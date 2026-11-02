#!/usr/bin/env python3
"""重ねて使うエフェクト素材（透過付き動画）をその場で生成する。

外部素材を一切使わず、計算だけでキラキラと白い発光を作る。
出力は QuickTime Animation (qtrle) の .mov で、アルファチャンネル付き。

使い方:
    python3 make_assets.py -o assets --fps 29.97
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np


def _write_mov(frames, path: Path, fps: float, size: int) -> None:
    """RGBA のフレーム列を透過付き .mov に書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{size}x{size}",
           "-framerate", f"{fps}", "-i", "-",
           "-c:v", "qtrle", "-pix_fmt", "argb", str(path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(f.tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError(f"{path} の書き出しに失敗")


def _star(canvas: np.ndarray, cx: float, cy: float, r: float, a: float,
          rgb: tuple[int, int, int], yy: np.ndarray, xx: np.ndarray) -> None:
    """4方向に伸びる光の粒をアルファ加算で描く。"""
    if a <= 0.002 or r <= 0.3:
        return
    dx, dy = xx - cx, yy - cy
    core = np.exp(-(dx * dx + dy * dy) / (2.0 * (r * 0.33) ** 2))
    spike_h = np.exp(-(dy * dy) / (2.0 * (r * 0.14) ** 2) - np.abs(dx) / (r * 0.85))
    spike_v = np.exp(-(dx * dx) / (2.0 * (r * 0.14) ** 2) - np.abs(dy) / (r * 0.85))
    m = np.clip(core + 0.55 * (spike_h + spike_v), 0.0, 1.0) * a
    keep = m > 0.004
    if not keep.any():
        return
    old = canvas[..., 3][keep] / 255.0
    new = np.clip(old + m[keep], 0.0, 1.0)
    canvas[..., 3][keep] = (new * 255).astype(np.uint8)
    for ch, val in enumerate(rgb):
        prev = canvas[..., ch][keep].astype(np.float32)
        canvas[..., ch][keep] = np.clip(prev * (1 - m[keep]) + val * m[keep], 0, 255).astype(np.uint8)


def sparkle(size: int, n_frames: int, fps: float, seed: int = 7) -> list[np.ndarray]:
    """中心から外へ広がるピンク系の粒。立ち上がり速く、消えはゆっくり。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = size / 2.0
    n_p = 26
    ang = rng.uniform(0, 2 * np.pi, n_p)
    spd = rng.uniform(0.18, 0.62, n_p) * size / n_frames
    birth = rng.uniform(0.0, 0.45, n_p)
    life = rng.uniform(0.35, 0.8, n_p)
    base_r = rng.uniform(size * 0.035, size * 0.085, n_p)
    # ピンク〜マゼンタ〜白のあいだで散らす
    tints = [(255, 120, 205), (255, 165, 225), (255, 255, 255), (240, 95, 190)]
    tint = [tints[i % len(tints)] for i in rng.permutation(n_p)]

    out = []
    for f in range(n_frames):
        t = f / (n_frames - 1)
        cv = np.zeros((size, size, 4), dtype=np.uint8)
        for i in range(n_p):
            lt = (t - birth[i]) / life[i]
            if lt < 0 or lt > 1:
                continue
            a = np.sin(np.pi * min(lt, 1.0)) ** 1.4
            d = spd[i] * n_frames * lt
            _star(cv, c + np.cos(ang[i]) * d, c + np.sin(ang[i]) * d,
                  base_r[i] * (1.25 - 0.55 * lt), float(a) * 0.95, tint[i], yy, xx)
        out.append(cv)
    return out


def glow(size: int, n_frames: int, fps: float) -> list[np.ndarray]:
    """一瞬だけ白く弾ける光。カット頭に置く用。"""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = size / 2.0
    d2 = (xx - c) ** 2 + (yy - c) ** 2
    out = []
    for f in range(n_frames):
        t = f / (n_frames - 1)
        a = np.sin(np.pi * t) ** 2.0
        r = size * (0.10 + 0.34 * t)
        cv = np.zeros((size, size, 4), dtype=np.uint8)
        m = np.exp(-d2 / (2.0 * (r * 0.45) ** 2)) * a
        cv[..., 0:3] = 255
        cv[..., 3] = (np.clip(m, 0, 1) * 255).astype(np.uint8)
        # 中心に十字の光条を足す
        spike = (np.exp(-((yy - c) ** 2) / (2 * (size * 0.012) ** 2) - np.abs(xx - c) / (r * 1.1))
                 + np.exp(-((xx - c) ** 2) / (2 * (size * 0.012) ** 2) - np.abs(yy - c) / (r * 1.1)))
        cv[..., 3] = np.clip(cv[..., 3].astype(np.float32)
                             + np.clip(spike, 0, 1) * a * 220, 0, 255).astype(np.uint8)
        out.append(cv)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="透過エフェクト素材を生成する")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("assets"))
    ap.add_argument("--fps", type=float, default=29.97)
    ap.add_argument("--sparkle-sec", type=float, default=1.6)
    ap.add_argument("--glow-sec", type=float, default=0.35)
    ap.add_argument("--size", type=int, default=512)
    args = ap.parse_args(argv)

    args.outdir.mkdir(parents=True, exist_ok=True)
    ns = max(6, int(round(args.sparkle_sec * args.fps)))
    ng = max(4, int(round(args.glow_sec * args.fps)))

    sp = args.outdir / "sparkle_pink.mov"
    gl = args.outdir / "glow_white.mov"
    _write_mov(sparkle(args.size, ns, args.fps), sp, args.fps, args.size)
    _write_mov(glow(args.size, ng, args.fps), gl, args.fps, args.size)
    print(f"生成: {sp} ({ns}フレーム / {ns / args.fps:.2f}秒)")
    print(f"生成: {gl} ({ng}フレーム / {ng / args.fps:.2f}秒)")


if __name__ == "__main__":
    main()
