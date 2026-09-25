#!/usr/bin/env python3
"""YouTube動画(またはローカル動画ファイル)をフレーム単位で分析する。

「このチャンネルと同じクオリティの動画を作る」ための下調べ用。全フレームの差分から
画面転換・テロップ更新・口パク・ポップインなどが「いつ・画面のどこで・何フレームかけて」
起きているかを数値化し、目視分析用の画像(キーフレーム一覧・連番フレーム・
変化ヒートマップ・タイムライン)を書き出す。GPU不要。ffmpegはimageio-ffmpeg同梱のものを使う。

usage:
  python scripts/analyze_video.py video https://www.youtube.com/watch?v=XXXXXXXXXXX
  python scripts/analyze_video.py video 録画.mp4 -o analysis/my_rec
  python scripts/analyze_video.py channel https://www.youtube.com/@name

出力(既定 analysis/<動画ID>/ 。analysis/ はgit管理外 — 他人の動画のフレームを含むため):
  report.md           要約レポート(数値+画像リンク)
  summary.json        全数値(機械可読)
  events.csv          変化イベント一覧(開始/終了フレーム・種類・範囲)
  regions.csv         よく変化する画面領域(字幕帯・口パク等の候補)と更新間隔
  frame_metrics.csv   全フレームの差分量
  images/             レイアウト中央値・変化ヒートマップ・タイムライン・一覧シート・連番ストリップ
  frames/             キーフレーム単体(横1280px)
"""

import argparse
import csv
import html
import json
import math
import os
import re
import subprocess
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "analysis"

# --- 解析パラメータ(px値は解析解像度 ANALYSIS_W 基準) ---
ANALYSIS_W = 480       # 差分解析の横幅。1080p素材の口パクや字幕の文字替わりまで拾える大きさ
CELL = 12              # 領域解析のセル一辺
PIX_T = 20             # RGBいずれかの差がこれを超えた画素を「変化した」とみなす(0-255)。
                       # 10〜16では圧縮ノイズ(キーフレーム前後の輪郭のちらつき)を拾う(合成動画で検証)
ACTIVE_RATIO = 0.002   # 画面のこの割合以上が変化したフレームは「動きあり」(口パク・まばたき程度は含めず、領域解析で拾う)
ACTIVE_MAD = 1.2       # 画素差の平均がこれ以上でも「動きあり」(1フレームごとの差が小さいフェードの検出用)
GAP_TOL = 1            # 動きの途切れがこのフレーム数以下なら同じイベントとみなす
CELL_ACTIVE = 0.05     # セル内でこの割合以上が変化したらそのセルは「動いた」
REGION_MIN_RATE = 1.0  # 1分あたりこの回数以上動くセルを「よく変化する領域」の候補にする
MAJOR_NET = 0.15       # イベント前後で画面のこの割合以上が変わったら「場面転換」
FULL_NET = 0.5         # 同・半分以上なら全面カット/全面トランジション
EXCLUDE_RATIO = 0.3    # 1フレームでこれ以上変わるフレームは領域解析から外す
BURST_GAP_S = 0.4      # 領域の更新がこの秒数未満の間隔で続く場合は1まとまり(バースト)とみなす
KEY_W = 1280           # 保存するキーフレームの横幅
TILE_W = 480           # 一覧シート1コマの横幅
MAX_STRIP_FRAMES = 24  # 連番ストリップ1枚あたりの最大フレーム数

TYPE_JA = {
    "cut": "全面カット",
    "transition": "全面トランジション",
    "major": "大きな部分切替",
    "minor": "局所的な変化",
    "continuous": "連続的な動き",
}


# ---------------------------------------------------------------- ffmpeg / 動画情報

def ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


NTSC_RATES = [(24000, 1001), (30000, 1001), (48000, 1001), (60000, 1001), (120000, 1001)]


def snap_fps(fps: float) -> float:
    """ffmpegの表示(23.98 fps等)は丸められているので、NTSC系/整数の正確な値に寄せる。
    長尺では丸め誤差が数フレームのズレになるため。"""
    for num, den in NTSC_RATES:
        if abs(fps - num / den) < 0.006:
            return num / den
    if abs(fps - round(fps)) < 0.006:
        return float(round(fps))
    return fps


def probe(path: Path) -> dict:
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True)
    text = r.stderr.decode("utf-8", "replace")
    lines = text.splitlines()
    vi = next((i for i, l in enumerate(lines)
               if re.search(r"Stream #\d+:\d+.*: Video: ", l) and "attached pic" not in l), None)
    if vi is None:
        raise SystemExit(f"映像ストリームが見つかりません: {path}\n{text[-800:]}")
    vline = lines[vi]
    size = re.search(r", (\d{2,5})x(\d{2,5})", vline)
    if not size:
        raise SystemExit(f"解像度を読み取れません: {vline}")
    w, h = int(size.group(1)), int(size.group(2))
    fps_m = re.search(r"([\d.]+) fps", vline) or re.search(r"([\d.]+) tbr", vline)
    fps = snap_fps(float(fps_m.group(1))) if fps_m else 30.0
    # 縦持ちスマホ動画などの回転メタデータ(ffmpegは自動回転して出力する)
    for l in lines[vi + 1: vi + 8]:
        rot = re.search(r"rotation of (-?[\d.]+) degrees", l)
        if rot and round(abs(float(rot.group(1)))) % 180 == 90:
            w, h = h, w
    dur = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else None
    bitrate = re.search(r"bitrate: (\d+) kb/s", text)
    audio = None
    aline = next((l for l in lines if re.search(r"Stream #\d+:\d+.*: Audio: ", l)), None)
    if aline:
        ac = re.search(r"Audio: (\w+)", aline)
        hz = re.search(r"(\d+) Hz, ([^,]+)", aline)
        audio = {
            "codec": ac.group(1) if ac else None,
            "sample_rate": int(hz.group(1)) if hz else None,
            "channels": hz.group(2).strip() if hz else None,
        }
    return {
        "width": w, "height": h, "fps": fps,
        "fps_display": fps_m.group(1) if fps_m else None,
        "duration": duration,
        "vcodec": re.search(r"Video: (\w+)", vline).group(1),
        "bitrate_kbps": int(bitrate.group(1)) if bitrate else None,
        "audio": audio,
    }


def even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def scaled_size(src_w: int, src_h: int, width: int) -> tuple[int, int]:
    width = min(width, src_w)
    width -= width % 2
    return width, even(width * src_h / src_w)


# ---------------------------------------------------------------- 全フレーム差分パス

SHOWINFO_RE = re.compile(rb"\bn:\s*(\d+)\s+pts:\s*(\S+)\s+pts_time:(\S+)")


def to_gray(rgb: np.ndarray) -> np.ndarray:
    r, g, b = (rgb[..., i].astype(np.uint16) for i in range(3))
    return ((r * 77 + g * 150 + b * 29) >> 8).astype(np.int16)


def frame_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """RGB(int16)の画素ごとの最大チャンネル差。輝度差だと明るさが同じ色同士のフェードを見逃す"""
    return np.abs(a - b).max(axis=2)


def cell_counts(mask: np.ndarray, hc: int, wc: int) -> np.ndarray:
    return mask[:hc * CELL, :wc * CELL].reshape(hc, CELL, wc, CELL).sum(axis=(1, 3), dtype=np.uint16)


def bbox_of(mask: np.ndarray) -> list | None:
    """変化マスクの外接矩形を画面比(0-1)の [x0, y0, x1, y1] で返す"""
    ys = np.flatnonzero(mask.any(1))
    xs = np.flatnonzero(mask.any(0))
    if not len(ys):
        return None
    h, w = mask.shape
    return [round(float(v), 3) for v in (xs[0] / w, ys[0] / h, (xs[-1] + 1) / w, (ys[-1] + 1) / h)]


def main_bbox(mask: np.ndarray) -> list | None:
    """同時に起きた小さな変化(口パク等)を除き、いちばん大きな変化のかたまりの外接矩形を返す"""
    h, w = mask.shape
    hc, wc = h // CELL, w // CELL
    cnt = cell_counts(mask, hc, wc)
    comps = components(cnt >= 4)
    if not comps:
        return bbox_of(mask)
    best = max(comps, key=lambda c: sum(int(cnt[y, x]) for y, x in c))
    keep = np.zeros_like(mask)
    for y, x in best:
        keep[y * CELL:(y + 1) * CELL, x * CELL:(x + 1) * CELL] = True
    return bbox_of(mask & keep)


def classify(dur: int, net_ratio: float, fps: float) -> str:
    if dur > 3 * fps:
        return "continuous"
    if net_ratio >= FULL_NET:
        return "cut" if dur <= 2 else "transition"
    if net_ratio >= MAJOR_NET:
        return "major"
    return "minor"


@dataclass
class _OpenSegment:
    start: int
    end: int
    before: np.ndarray
    union: np.ndarray   # 区間中に一度でも変化した画素
    late: np.ndarray    # 2フレーム目以降に変化した画素(開始と同時の字幕差し替え等を除くため)
    peak: float


def run_pass(path: Path, meta: dict, aw: int, ah: int, max_seconds: float | None) -> dict:
    """全フレームを解析解像度でデコードし、フレーム間差分・イベント・セル別変化を集計する"""
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-loglevel", "info"]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-i", str(path), "-map", "0:v:0", "-an",
            "-vf", f"scale={aw}:{ah}:flags=area,showinfo",
            "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pts: list[float] = []
    err_tail: list[bytes] = []

    def pump_stderr():
        for line in proc.stderr:
            m = SHOWINFO_RE.search(line)
            if m:
                try:
                    pts.append(float(m.group(3)))
                except ValueError:  # NOPTS
                    pts.append(float("nan"))
            else:
                err_tail.append(line)
                del err_tail[:-40]

    reader = threading.Thread(target=pump_stderr, daemon=True)
    reader.start()

    fps = meta["fps"]
    est_total = int((min(meta["duration"] or 0, max_seconds or 1e12)) * fps) or None
    sample_every = max(1, (est_total or 3000) // 240)
    hc, wc = ah // CELL, aw // CELL
    frame_bytes = aw * ah * 3

    mads, ratios, cells, samples = [], [], [], []
    pix_count = np.zeros((ah, aw), np.uint32)
    segments: list[dict] = []
    seg: _OpenSegment | None = None
    quiet = 0
    prev = prev2 = pending = None
    cell_thr = math.ceil(CELL_ACTIVE * CELL * CELL)

    def close(s: _OpenSegment, after: np.ndarray):
        dur = s.end - s.start + 1
        net = frame_diff(after, s.before) > PIX_T
        net_ratio = float(net.mean())
        if s.peak < ACTIVE_RATIO and net_ratio < ACTIVE_RATIO * 10:
            return  # どのフレームでも目に見える変化がなく、前後もほぼ同じ = 圧縮ノイズの揺れ
        typ = classify(dur, net_ratio, fps)
        segments.append({
            "start": s.start, "end": s.end, "dur": dur, "type": typ, "anim": dur >= 3,
            "peak_ratio": round(s.peak, 4), "net_ratio": round(net_ratio, 4),
            "union_ratio": round(float(s.union.mean()), 4),
            "net_bbox": bbox_of(net), "union_bbox": bbox_of(s.union),
            # 全面の切替は前後差分の範囲、それ以外は同時に起きた小さな変化を除いた主な範囲
            "main_bbox": bbox_of(net) if typ in ("cut", "transition") else
            main_bbox(s.late if dur >= 3 and s.late.any() else s.union),
        })

    i = -1
    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        i += 1
        rgb = np.frombuffer(buf, np.uint8).reshape(ah, aw, 3)
        cur = rgb.astype(np.int16)
        if i % sample_every == 0:
            samples.append(rgb.copy())
            if len(samples) >= 480:
                samples = samples[::2]
                sample_every *= 2
        if prev is None:
            mads.append(0.0)
            ratios.append(0.0)
            cells.append(np.zeros(hc * wc, np.uint8))
            prev = cur
            continue
        diff = frame_diff(cur, prev)
        changed = diff > PIX_T
        ratio = float(changed.mean())
        mad = float(diff.mean())
        mads.append(mad)
        ratios.append(ratio)
        # セル別の変化: 次のフレームでもまだ変化が残っている時だけ数える(1フレームだけの
        # 圧縮ちらつきを除く)。判定に次フレームが要るので1フレーム遅れで確定させる
        c1 = cell_counts(changed, hc, wc)
        if pending is not None:
            if (pending >= cell_thr).any():
                pending = np.minimum(pending, cell_counts(frame_diff(cur, prev2) > PIX_T, hc, wc))
            cells.append(pending.astype(np.uint8).ravel())
        pending = c1
        if ratio < EXCLUDE_RATIO:
            pix_count += changed
        if ratio >= ACTIVE_RATIO or mad >= ACTIVE_MAD:
            if seg is None:
                seg = _OpenSegment(start=i, end=i, before=prev, union=changed.copy(),
                                   late=np.zeros_like(changed), peak=ratio)
            else:
                seg.union |= changed
                seg.late |= changed
                seg.end = i
                seg.peak = max(seg.peak, ratio)
            quiet = 0
        elif seg is not None:
            quiet += 1
            if quiet > GAP_TOL:
                close(seg, cur)
                seg = None
        prev2, prev = prev, cur
        if i % 600 == 0:
            total = f"/{est_total}" if est_total else ""
            print(f"\r  全フレーム解析中 {i}{total} フレーム", end="", file=sys.stderr, flush=True)
    if pending is not None:
        cells.append(pending.astype(np.uint8).ravel())
    if seg is not None and prev is not None:
        close(seg, prev)
    proc.stdout.close()
    proc.wait()
    reader.join(timeout=10)
    n = i + 1
    print(f"\r  全フレーム解析 完了: {n} フレーム            ", file=sys.stderr)
    if n == 0:
        tail = b"".join(err_tail).decode("utf-8", "replace")
        raise SystemExit(f"フレームを1枚も読めませんでした:\n{tail}")

    if len(pts) >= n:
        pts_arr = np.array(pts[:n], np.float64)
        bad = np.isnan(pts_arr)
        if bad.any():
            good = np.flatnonzero(~bad)
            pts_arr[bad] = np.interp(np.flatnonzero(bad), good, pts_arr[good]) if len(good) else \
                np.flatnonzero(bad) / fps
    else:  # showinfoの行を取りこぼした場合は固定fpsで近似
        t0 = pts[0] if pts and not math.isnan(pts[0]) else 0.0
        pts_arr = t0 + np.arange(n) / fps

    return {
        "n": n, "aw": aw, "ah": ah, "hc": hc, "wc": wc,
        "mad": np.array(mads, np.float32), "ratio": np.array(ratios, np.float32),
        "cells": np.stack(cells), "pix_count": pix_count,
        "samples": samples, "segments": segments, "pts": pts_arr,
    }


# ---------------------------------------------------------------- 領域(字幕帯・口パク等)の解析

def runs_of(b: np.ndarray) -> list[tuple[int, int]]:
    """True連続区間を (開始, 終了) の包含区間で返す"""
    d = np.diff(np.concatenate([[0], b.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1).tolist(), (np.flatnonzero(d == -1) - 1).tolist()))


def merge_runs(runs: list[tuple[int, int]], max_gap: int) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s, e in runs:
        if out and s - out[-1][1] - 1 <= max_gap:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """8近傍の連結成分(セル単位なので素朴なBFSで十分)"""
    h, w = mask.shape
    seen = np.zeros_like(mask, bool)
    comps = []
    for y in range(h):
        for x in range(w):
            if not mask[y, x] or seen[y, x]:
                continue
            stack, comp = [(y, x)], []
            seen[y, x] = True
            while stack:
                cy, cx = stack.pop()
                comp.append((cy, cx))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
            comps.append(comp)
    return comps


def dilate(mask: np.ndarray) -> np.ndarray:
    out = mask.copy()
    p = np.pad(mask, 1)
    h, w = mask.shape
    for dy in range(3):
        for dx in range(3):
            out |= p[dy:dy + h, dx:dx + w]
    return out


def position_ja(cx: float, cy: float) -> str:
    v = "上" if cy < 1 / 3 else ("中" if cy < 2 / 3 else "下")
    hz = "左" if cx < 1 / 3 else ("中央" if cx < 2 / 3 else "右")
    return f"{v}・{hz}"


def guess_region(r: dict, aspect: float) -> str:
    """数値からの当て推量。必ず連番ストリップ等で目視確認すること"""
    x0, y0, x1, y1 = r["bbox"]
    w, h = x1 - x0, (y1 - y0) / aspect  # 縦横比を画素比に揃える
    area = (x1 - x0) * (y1 - y0)
    cy = (y0 + y1) / 2
    bi = r["burst_interval_median_s"]
    bd = r["burst_dur_median_s"]
    if w / max(h, 1e-6) >= 2.5 and cy >= 0.55 and bi and 0.8 <= bi <= 15:
        return "字幕(テロップ)帯?"
    if area <= 0.03 and r["events_per_min"] >= 60:
        return "口パク?"
    if area <= 0.03 and bi and 1.5 <= bi <= 12 and bd is not None and bd <= 0.4:
        return "まばたき?"
    if area >= 0.08 and bi and bi >= 3:
        return "図解・画像の差し替え?"
    if r["active_pct"] >= 50:
        return "常時動いている(背景アニメ等)?"
    return "その他の動き"


def find_regions(A: dict, exclude: np.ndarray, fps: float) -> list[dict]:
    thr = math.ceil(CELL_ACTIVE * CELL * CELL)
    act = A["cells"] >= thr
    act[exclude] = False
    minutes = A["n"] / fps / 60
    rises = (act[1:] & ~act[:-1]).sum(0) + act[0]
    rate = rises / max(minutes, 1e-6)
    grid = ((rate >= REGION_MIN_RATE) & (rises >= 2)).reshape(A["hc"], A["wc"])
    regions = []
    for comp in components(dilate(grid)):
        cells = [(y, x) for y, x in comp if grid[y, x]]
        if not cells:
            continue
        idx = [y * A["wc"] + x for y, x in cells]
        ys = [y for y, _ in cells]
        xs = [x for _, x in cells]
        k = max(1, int(round(0.05 * len(idx))))
        ract = act[:, idx].sum(1) >= k
        events = merge_runs(runs_of(ract), GAP_TOL)
        if len(events) < 2:
            continue
        bursts = merge_runs(events, int(BURST_GAP_S * fps))
        starts = np.array([s for s, _ in bursts])
        intervals = np.diff(starts) / fps
        bdur = np.array([e - s + 1 for s, e in bursts]) / fps
        edur = np.array([e - s + 1 for s, e in events])
        regions.append({
            "cells": len(cells),
            "bbox": [round(min(xs) * CELL / A["aw"], 3), round(min(ys) * CELL / A["ah"], 3),
                     round((max(xs) + 1) * CELL / A["aw"], 3), round((max(ys) + 1) * CELL / A["ah"], 3)],
            "grid_bbox": [min(xs), min(ys), max(xs) + 1, max(ys) + 1],
            "events": len(events),
            "events_per_min": round(len(events) / minutes, 1),
            "event_dur_median_frames": float(np.median(edur)),
            "bursts": len(bursts),
            "bursts_per_min": round(len(bursts) / minutes, 1),
            "burst_interval_median_s": round(float(np.median(intervals)), 2) if len(intervals) else None,
            "burst_interval_p25_s": round(float(np.percentile(intervals, 25)), 2) if len(intervals) else None,
            "burst_interval_p75_s": round(float(np.percentile(intervals, 75)), 2) if len(intervals) else None,
            "burst_dur_median_s": round(float(np.median(bdur)), 2),
            "active_pct": round(float(ract.mean() * 100), 1),
            "_activity": ract,
        })
    regions = [r for r in regions if r["cells"] >= 2 or r["events_per_min"] >= 10]
    regions.sort(key=lambda r: r["events_per_min"] * math.sqrt(r["cells"]), reverse=True)
    aspect = A["aw"] / A["ah"]
    for n, r in enumerate(regions, 1):
        x0, y0, x1, y1 = r["bbox"]
        r["id"] = f"R{n}"
        r["position"] = position_ja((x0 + x1) / 2, (y0 + y1) / 2)
        r["guess"] = guess_region(r, aspect)
    return regions


# ---------------------------------------------------------------- フレーム切り出し

def grab_frames(path: Path, pts: np.ndarray, fps: float, idx: int, count: int,
                src_w: int, src_h: int, width: int) -> np.ndarray:
    """idx番目から連続count枚を横width pxで切り出す(-ssの高精度シーク)"""
    w, h = scaled_size(src_w, src_h, width)
    t = max(0.0, float(pts[idx]) - 0.25 / fps)
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.6f}", "-i", str(path),
           "-map", "0:v:0", "-an", "-frames:v", str(count), "-vf", f"scale={w}:{h}:flags=lanczos",
           "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True).stdout
    fb = w * h * 3
    k = len(out) // fb
    return np.frombuffer(out[:k * fb], np.uint8).reshape(k, h, w, 3)


def fmt_time(t: float) -> str:
    m, s = divmod(max(t, 0.0), 60)
    return f"{int(m)}:{s:05.2f}"


def load_font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def label_image(im: Image.Image, text: str, size: int = 18) -> Image.Image:
    d = ImageDraw.Draw(im)
    font = load_font(size)
    tw = d.textlength(text, font=font)
    d.rectangle([0, 0, tw + 10, size + 8], fill=(0, 0, 0))
    d.text((5, 3), text, fill=(255, 230, 0), font=font)
    return im


def make_sheet(items: list[tuple[np.ndarray, str]], out: Path, title: str, cols: int = 4,
               tile_w: int = TILE_W) -> None:
    tiles = []
    for arr, lab in items:
        im = Image.fromarray(arr)
        im = im.resize((tile_w, even(tile_w * im.height / im.width)), Image.LANCZOS)
        tiles.append(label_image(im, lab))
    th = max(t.height for t in tiles)
    rows = math.ceil(len(tiles) / cols)
    pad, title_h = 4, 34
    sheet = Image.new("RGB", (cols * tile_w + (cols + 1) * pad, title_h + rows * (th + pad) + pad), (28, 28, 28))
    ImageDraw.Draw(sheet).text((8, 7), title, fill=(255, 255, 255), font=load_font(20))
    for k, t in enumerate(tiles):
        r, c = divmod(k, cols)
        sheet.paste(t, (pad + c * (tile_w + pad), title_h + pad + r * (th + pad)))
    sheet.save(out, quality=88)


# ---------------------------------------------------------------- 可視化(ヒートマップ・タイムライン)

MAGMA = [(0.0, (0, 0, 4)), (0.25, (81, 18, 124)), (0.5, (183, 55, 121)),
         (0.75, (252, 137, 97)), (1.0, (252, 253, 191))]


def colormap(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0, 1)
    out = np.zeros(v.shape + (3,), np.float32)
    for (a, ca), (b, cb) in zip(MAGMA, MAGMA[1:]):
        m = (v >= a) & (v <= b)
        t = ((v - a) / (b - a))[m][:, None]
        out[m] = np.array(ca) * (1 - t) + np.array(cb) * t
    return out.astype(np.uint8)


def render_layout_images(A: dict, regions: list[dict], img_dir: Path) -> None:
    median = np.median(np.stack(A["samples"]), axis=0).astype(np.uint8)
    scale = 2
    size = (A["aw"] * scale, A["ah"] * scale)
    Image.fromarray(median).resize(size, Image.BICUBIC).save(img_dir / "median_layout.png")

    heat = np.log1p(A["pix_count"].astype(np.float32))
    heat /= max(float(heat.max()), 1e-6)
    base = (to_gray(median).astype(np.float32) * 0.45)[..., None].repeat(3, axis=2)
    col = colormap(heat).astype(np.float32)
    alpha = (heat ** 0.6 * 0.85)[..., None]
    over = (base * (1 - alpha) + col * alpha).astype(np.uint8)
    im = Image.fromarray(over).resize(size, Image.BICUBIC)
    d = ImageDraw.Draw(im)
    font = load_font(16)
    px = CELL * scale
    for r in regions[:12]:
        gx0, gy0, gx1, gy1 = r["grid_bbox"]
        d.rectangle([gx0 * px, gy0 * px, gx1 * px - 1, gy1 * px - 1], outline=(0, 255, 255), width=2)
        d.rectangle([gx0 * px, gy0 * px, gx0 * px + 34, gy0 * px + 20], fill=(0, 0, 0))
        d.text((gx0 * px + 3, gy0 * px + 2), r["id"], fill=(0, 255, 255), font=font)
    im.save(img_dir / "change_heatmap.png")


def render_timeline(A: dict, fps: float, regions: list[dict], audio_env: np.ndarray | None,
                    audio_hop: float, out: Path) -> None:
    n = A["n"]
    dur = n / fps
    left, width, lane_h, gap = 110, 1700, 34, 6
    lanes = ["cuts", "motion"] + (["audio"] if audio_env is not None else []) + [r["id"] for r in regions[:6]]
    H = 30 + len(lanes) * (lane_h + gap) + 24
    im = Image.new("RGB", (left + width + 20, H), (250, 250, 250))
    d = ImageDraw.Draw(im)
    font = load_font(14)

    def x_of(t):
        return left + int(t / max(dur, 1e-6) * width)

    step = 60 if dur > 180 else (10 if dur > 30 else 5)
    for t in np.arange(0, dur + 1e-6, step):
        d.line([x_of(t), 20, x_of(t), H - 20], fill=(215, 215, 215))
        d.text((x_of(t) + 2, H - 18), fmt_time(t)[:-3], fill=(90, 90, 90), font=font)
    per_sec = max(1, int(round(fps)))
    for li, name in enumerate(lanes):
        y0 = 24 + li * (lane_h + gap)
        y1 = y0 + lane_h
        d.rectangle([left, y0, left + width, y1], outline=(200, 200, 200))
        d.text((6, y0 + 9), name, fill=(40, 40, 40), font=font)
        if name == "cuts":
            for s in A["segments"]:
                if s["type"] in ("cut", "transition", "major"):
                    c = {"cut": (220, 40, 40), "transition": (140, 40, 200), "major": (240, 150, 0)}[s["type"]]
                    xa, xb = x_of(s["start"] / fps), x_of((s["end"] + 1) / fps)
                    d.rectangle([xa, y0 + 2, max(xb, xa + 1), y1 - 2], fill=c)
        elif name == "motion":
            r = A["ratio"]
            for k in range(0, n, per_sec):
                v = float(r[k:k + per_sec].max())
                hgt = int(min(1.0, v / 0.3) ** 0.5 * (lane_h - 4))
                xa = x_of(k / fps)
                d.rectangle([xa, y1 - 2 - hgt, max(x_of((k + per_sec) / fps) - 1, xa), y1 - 2], fill=(90, 90, 90))
        elif name == "audio":
            for k, v in enumerate(audio_env):
                hgt = int(np.clip((v + 60) / 60, 0, 1) * (lane_h - 4))
                xa = x_of(k * audio_hop)
                d.line([xa, y1 - 2 - hgt, xa, y1 - 2], fill=(40, 120, 200))
        else:
            act = next(r for r in regions if r["id"] == name)["_activity"]
            for k in range(0, n, per_sec):
                v = float(act[k:k + per_sec].mean())
                if v > 0:
                    shade = int(235 - 200 * min(1.0, v * 3))
                    d.rectangle([x_of(k / fps), y0 + 3, max(x_of((k + per_sec) / fps) - 1, x_of(k / fps)), y1 - 3],
                                fill=(shade, shade, shade))
    im.save(out)


# ---------------------------------------------------------------- 音声

def analyze_audio(path: Path, max_seconds: float | None) -> dict | None:
    ff = ffmpeg_exe()
    lim = ["-t", str(max_seconds)] if max_seconds else []
    r = subprocess.run([ff, "-hide_banner", "-nostats", *lim, "-i", str(path), "-map", "0:a:0", "-vn",
                        "-af", "ebur128=peak=true:framelog=verbose", "-f", "null", "-"], capture_output=True)
    text = r.stderr.decode("utf-8", "replace")
    if "Summary:" not in text:
        return None
    summ = text.split("Summary:")[-1]

    def grab(pat):
        m = re.search(pat, summ)
        return float(m.group(1)) if m and m.group(1) != "-inf" else None

    res = {
        "integrated_lufs": grab(r"I:\s+(-?[\d.]+) LUFS"),
        "lra_lu": grab(r"LRA:\s+(-?[\d.]+) LU"),
        "true_peak_dbfs": grab(r"Peak:\s+(-?[\d.]+|-inf) dBFS"),
    }
    raw = subprocess.run([ff, "-hide_banner", "-loglevel", "error", *lim, "-i", str(path), "-map", "0:a:0",
                          "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"], capture_output=True).stdout
    x = np.frombuffer(raw, np.float32)
    hop = 0.05
    win = int(16000 * hop)
    if len(x) < win * 10:
        return res
    frames = x[: len(x) // win * win].reshape(-1, win)
    env = 20 * np.log10(np.sqrt((frames.astype(np.float64) ** 2).mean(1)) + 1e-9)
    p10, p50, p90 = (float(np.percentile(env, q)) for q in (10, 50, 90))
    thr = (p10 + p90) / 2  # 発話と間(ま)の二峰の中間で切る
    loud = env > thr
    pauses = [(s, e) for s, e in runs_of(~loud) if (e - s + 1) * hop >= 0.15]
    speech = [(s, e) for s, e in runs_of(loud) if (e - s + 1) * hop >= 0.1]
    minutes = len(env) * hop / 60
    pl = np.array([(e - s + 1) * hop for s, e in pauses])
    sl = np.array([(e - s + 1) * hop for s, e in speech])
    res.update({
        "level_p10_dbfs": round(p10, 1), "level_p50_dbfs": round(p50, 1), "level_p90_dbfs": round(p90, 1),
        "split_threshold_dbfs": round(thr, 1),
        "pauses": len(pauses), "pauses_per_min": round(len(pauses) / minutes, 1),
        "pause_median_s": round(float(np.median(pl)), 2) if len(pl) else None,
        "pause_p75_s": round(float(np.percentile(pl, 75)), 2) if len(pl) else None,
        "speech_run_median_s": round(float(np.median(sl)), 2) if len(sl) else None,
        "loud_ratio_pct": round(float(loud.mean() * 100), 1),
        "never_silent": bool(p10 > -50),  # 間でも-50dBFSを下回らない → BGM/環境音が常時鳴っている
        "_env": env, "_hop": hop,
    })
    return res


# ---------------------------------------------------------------- 字幕(話速)

VTT_TIME = re.compile(r"(\d+:)?(\d+):(\d+)[.,](\d+)\s+-->\s+(\d+:)?(\d+):(\d+)[.,](\d+)")


def _secs(h, m, s, ms):
    return (int(h[:-1]) if h else 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 10 ** len(ms)


def parse_subtitles(path: Path) -> list[tuple[float, float, str]]:
    """VTT/SRTを読み、YouTube自動字幕の「前の行を繰り返すロールアップ表示」を重複排除して行単位で返す"""
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[tuple[float, float, str]] = []
    recent: list[str] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip().split("\n")
        ti = next((k for k, l in enumerate(lines) if VTT_TIME.search(l)), None)
        if ti is None:
            continue
        m = VTT_TIME.search(lines[ti])
        start, end = _secs(*m.group(1, 2, 3, 4)), _secs(*m.group(5, 6, 7, 8))
        for raw in lines[ti + 1:]:
            line = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
            if not line or line in recent:
                continue
            out.append((start, end, line))
            recent = (recent + [line])[-4:]
    return out


def subtitle_stats(cues: list[tuple[float, float, str]], duration: float, speech_ratio: float | None) -> dict:
    chars = sum(len(re.sub(r"\s", "", t)) for _, _, t in cues)
    minutes = duration / 60
    res = {"lines": len(cues), "chars": chars, "chars_per_min": round(chars / max(minutes, 1e-6), 1)}
    if speech_ratio:
        res["chars_per_speech_min"] = round(chars / max(minutes * speech_ratio, 1e-6), 1)
    return res


# ---------------------------------------------------------------- yt-dlp

def ydl_base_opts(args) -> dict:
    opts = {
        "js_runtimes": {"deno": {}, "node": {}},  # YouTubeの署名解読にJSランタイムが要る(どちらか入っていればよい)
        "retries": 3,
    }
    if os.environ.get("SSL_CERT_FILE"):
        opts["compat_opts"] = {"no-certifi"}  # 独自CAのプロキシ環境ではOSの証明書設定に従う
    if getattr(args, "cookies_from_browser", None):
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    return opts


def download(url: str, out_dir: Path | None, args) -> tuple[Path, dict, dict[str, Path]]:
    import yt_dlp
    target = str((out_dir or DEFAULT_OUT / "%(id)s") / "source.%(ext)s")
    mh = args.max_height
    opts = ydl_base_opts(args) | {
        "format": f"bv*[height<={mh}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={mh}]+ba/b[height<={mh}]/b",
        "outtmpl": {"default": target},
        "ffmpeg_location": ffmpeg_exe(),
        "writeinfojson": True,
        "writethumbnail": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["ja", "ja-orig"],
        "subtitlesformat": "vtt/best",
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        if "subtitle" not in str(e).lower():
            raise
        print("  字幕の取得に失敗したので字幕なしで再取得します", file=sys.stderr)
        opts.update(writesubtitles=False, writeautomaticsub=False)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    path = Path(info["requested_downloads"][0]["filepath"])
    subs = {lang: Path(d["filepath"]) for lang, d in (info.get("requested_subtitles") or {}).items()
            if d.get("filepath") and Path(d["filepath"]).exists()}
    return path, info, subs


# ---------------------------------------------------------------- videoコマンド

def pick_strips(segments: list[dict], regions: list[dict], fps: float, limit: int) -> list[dict]:
    """フレーム単位で見るべきイベントを選ぶ: 転換 > アニメーション付き変化 > 主要領域の更新"""
    chosen: list[dict] = []

    def far_enough(s):  # すでに選んだイベントと区間が重ならない
        return all(s["start"] > c["end"] + 10 or s["end"] + 10 < c["start"] for c in chosen)

    for typ, k in (("transition", 2), ("cut", 1)):
        for s in sorted((s for s in segments if s["type"] == typ), key=lambda s: -s["dur"])[:k]:
            chosen.append(dict(s, why=TYPE_JA[typ]))
    anim = [s for s in segments if s["anim"] and s["type"] in ("major", "minor")]
    anim.sort(key=lambda s: -(s["union_ratio"] * s["dur"]))
    for s in anim:
        if len([c for c in chosen if c["why"] == "アニメーション"]) >= max(2, limit // 2):
            break
        if far_enough(s):
            chosen.append(dict(s, why="アニメーション"))
    for r in regions[:4]:
        if len(chosen) >= limit:
            break
        bursts = merge_runs(merge_runs(runs_of(r["_activity"]), GAP_TOL), int(BURST_GAP_S * fps))
        if bursts:
            s, e = bursts[len(bursts) // 2]
            chosen.append({"start": s, "end": e, "dur": e - s + 1, "type": "region",
                           "main_bbox": r["bbox"], "why": f"{r['id']} {r['guess']}"})
    chosen = chosen[:limit]
    chosen.sort(key=lambda c: c["start"])
    return chosen


def cmd_video(args) -> None:
    src = args.source
    out_dir = Path(args.out).resolve() if args.out else None
    info, subs = None, {}
    if re.match(r"https?://", src):
        print(f"[1/6] ダウンロード: {src}", file=sys.stderr)
        path, info, subs = download(src, out_dir, args)
        out_dir = out_dir or path.parent
    else:
        path = Path(src).resolve()
        if not path.exists():
            raise SystemExit(f"ファイルがありません: {path}")
        out_dir = out_dir or DEFAULT_OUT / path.stem
        if args.subs:
            subs = {"file": Path(args.subs)}
    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir, frame_dir = out_dir / "images", out_dir / "frames"
    img_dir.mkdir(exist_ok=True)
    frame_dir.mkdir(exist_ok=True)

    meta = probe(path)
    fps = meta["fps"]
    aw, ah = scaled_size(meta["width"], meta["height"], ANALYSIS_W)
    print(f"[2/6] 動画情報: {meta['width']}x{meta['height']} {fps:.3f}fps "
          f"{meta['duration'] or 0:.1f}秒 {meta['vcodec']}", file=sys.stderr)

    A = run_pass(path, meta, aw, ah, args.max_minutes * 60 if args.max_minutes else None)
    n, pts, segs = A["n"], A["pts"], A["segments"]
    duration = n / fps
    for s in segs:
        s["start_time"] = round(float(pts[s["start"]]), 3)
        s["dur_s"] = round(s["dur"] / fps, 3)

    exclude = A["ratio"] >= EXCLUDE_RATIO
    for s in segs:
        if s["type"] in ("cut", "transition"):
            exclude[s["start"]: s["end"] + 1] = True
    print("[3/6] 画面領域の解析", file=sys.stderr)
    regions = find_regions(A, exclude, fps)

    print("[4/6] 音声・字幕の解析", file=sys.stderr)
    audio = analyze_audio(path, args.max_minutes * 60 if args.max_minutes else None) if meta["audio"] else None
    sub_stats, sub_file = None, None
    pref = [k for k in ("file", "ja", "ja-orig") if k in subs] + [k for k in subs if k not in ("file", "ja", "ja-orig")]
    if pref:
        sub_file = subs[pref[0]]
        cues = parse_subtitles(sub_file)
        speech_ratio = audio["loud_ratio_pct"] / 100 if audio and audio.get("loud_ratio_pct") else None
        sub_stats = subtitle_stats(cues, duration, speech_ratio)
        sub_stats["source"] = sub_file.name
        (out_dir / "transcript.txt").write_text(
            "\n".join(f"[{fmt_time(s)}] {t}" for s, _, t in cues), encoding="utf-8")

    print("[5/6] キーフレーム・連番ストリップの書き出し", file=sys.stderr)
    W, H = meta["width"], meta["height"]

    def grab(idx, count=1, width=KEY_W):
        return grab_frames(path, pts, fps, idx, count, W, H, width)

    # キーフレーム = 場面転換の直前(その場面が出来上がった状態)
    majors = sorted((s for s in segs if s["type"] in ("cut", "transition", "major")), key=lambda s: s["start"])
    bounds, cur = [], 0
    for s in majors:
        if s["start"] - 1 >= cur:
            bounds.append((cur, s["start"] - 1))
        cur = s["end"] + 1
    if cur <= n - 1:
        bounds.append((cur, n - 1))
    stable = [b for b in bounds if (b[1] - b[0] + 1) >= 0.3 * fps] or bounds
    key_idx = [e for _, e in stable]
    if len(key_idx) > args.max_keyframes:
        key_idx = [key_idx[int(k)] for k in np.linspace(0, len(key_idx) - 1, args.max_keyframes)]
    interval_idx = sorted({int(np.searchsorted(pts, t)) for t in np.arange(0, duration, args.interval)})
    interval_idx = [min(k, n - 1) for k in interval_idx]
    with ThreadPoolExecutor(max_workers=4) as pool:
        key_frames = list(pool.map(grab, key_idx))
        int_frames = list(pool.map(grab, interval_idx))
    key_items = []
    for idx, fr in zip(key_idx, key_frames):
        if len(fr):
            Image.fromarray(fr[0]).save(frame_dir / f"key_{idx:06d}.jpg", quality=92)
            key_items.append((fr[0], f"{fmt_time(pts[idx])}  f{idx}"))
    int_items = [(fr[0], f"{fmt_time(pts[idx])}  f{idx}") for idx, fr in zip(interval_idx, int_frames) if len(fr)]
    sheets = {"keyframes": [], "interval": []}
    # 画像内の文字は英数字のみ(Pillow既定フォントは日本語を描けない)
    for name, items, title in (("keyframes", key_items, "keyframes (last frame of each scene)"),
                               ("interval", int_items, f"every {args.interval:g}s")):
        for k in range(0, len(items), 20):
            p = img_dir / f"{name}_{k // 20 + 1:02d}.jpg"
            make_sheet(items[k:k + 20], p, f"{title}  {k // 20 + 1}/{math.ceil(len(items) / 20)}")
            sheets[name].append(p.name)

    strips = []
    for k, s in enumerate(pick_strips(segs, regions, fps, args.strips), 1):
        first = max(0, s["start"] - 2)
        count = min(MAX_STRIP_FRAMES, s["end"] + 4 - first)
        frames = grab(first, count, width=KEY_W)
        if not len(frames):
            continue
        items = [(f, f"f{j - (s['start'] - first):+d}  #{first + j}") for j, f in enumerate(frames)]
        tag = {"cut": "cut", "transition": "transition", "region": "region"}.get(s["type"], "anim")
        name = f"strip_{k:02d}_{tag}_{fmt_time(pts[s['start']]).replace(':', 'm').replace('.', 's')}.jpg"
        title = f"#{s['start']} {fmt_time(pts[s['start']])}  dur {s['dur']}f  ({tag})"
        make_sheet(items, img_dir / name, title)
        crop_name = None
        bb = s.get("main_bbox")
        if bb and s["type"] not in ("cut", "transition") and (bb[2] - bb[0]) * (bb[3] - bb[1]) < 0.2:
            fh, fw = frames.shape[1:3]
            cx, cy = (bb[0] + bb[2]) / 2 * fw, (bb[1] + bb[3]) / 2 * fh
            bw = max((bb[2] - bb[0]) * fw * 1.6, fw * 0.15)
            bh = max((bb[3] - bb[1]) * fh * 1.6, fh * 0.15)
            x0, x1 = int(max(0, cx - bw / 2)), int(min(fw, cx + bw / 2))
            y0, y1 = int(max(0, cy - bh / 2)), int(min(fh, cy + bh / 2))
            crop_name = name.replace(".jpg", "_zoom.jpg")
            make_sheet([(np.ascontiguousarray(f[y0:y1, x0:x1]), lab) for f, lab in items], img_dir / crop_name,
                       title + "  zoom")
        strips.append({"file": name, "zoom": crop_name, "start": s["start"], "end": s["end"],
                       "time": round(float(pts[s["start"]]), 3), "dur_frames": s["dur"], "why": s["why"]})

    print("[6/6] レポート作成", file=sys.stderr)
    render_layout_images(A, regions, img_dir)
    render_timeline(A, fps, regions, audio.get("_env") if audio else None, audio.get("_hop", 0.05) if audio else 0.05,
                    img_dir / "timeline.png")
    thumb = None
    for ext in ("webp", "jpg", "png"):
        p = path.with_suffix(f".{ext}")
        if p.exists() and p != path:
            Image.open(p).convert("RGB").save(img_dir / "thumbnail.jpg", quality=92)
            thumb = "thumbnail.jpg"
            break

    summary = build_summary(meta, A, fps, duration, segs, regions, audio, sub_stats, info, args)
    summary.update({"sheets": sheets, "strips": strips, "thumbnail": thumb,
                    "source_file": path.name, "keyframes": len(key_items)})
    write_outputs(out_dir, A, fps, segs, regions, summary, exclude)
    write_report(out_dir, summary, regions, strips, sheets)
    print(f"完了: {out_dir / 'report.md'}", file=sys.stderr)


def pct(a, q):
    return round(float(np.percentile(a, q)), 2) if len(a) else None


def build_summary(meta, A, fps, duration, segs, regions, audio, sub_stats, info, args) -> dict:
    minutes = duration / 60
    scene = [s for s in segs if s["type"] in ("cut", "transition", "major")]
    scene_starts = np.array([s["start"] for s in scene]) / fps
    scene_iv = np.diff(np.concatenate([[0.0], scene_starts, [duration]]))
    anim = [s for s in segs if s["anim"] and s["type"] != "continuous"]
    anim_d = np.array([s["dur"] for s in anim])
    instant = [s for s in segs if not s["anim"] and s["type"] in ("minor", "major")]
    active = np.zeros(A["n"], bool)
    for s in segs:
        active[s["start"]: s["end"] + 1] = True
    counts = {t: sum(1 for s in segs if s["type"] == t) for t in TYPE_JA}
    yt = None
    if info:
        yt = {k: info.get(k) for k in ("id", "title", "channel", "channel_follower_count", "upload_date",
                                       "duration", "view_count", "like_count", "comment_count", "webpage_url")}
        yt["chapters"] = [{"start": c.get("start_time"), "title": c.get("title")} for c in info.get("chapters") or []]
        yt["tags"] = info.get("tags") or []
        yt["description_chars"] = len(info.get("description") or "")
    return {
        "video": {**{k: meta[k] for k in ("width", "height", "fps", "fps_display", "vcodec", "bitrate_kbps", "audio")},
                  "frames_analyzed": A["n"], "duration_analyzed_s": round(duration, 2),
                  "analysis_resolution": [A["aw"], A["ah"]]},
        "tempo": {
            "scene_changes": len(scene),
            "scene_changes_per_min": round(len(scene) / minutes, 2),
            "scene_interval_median_s": pct(scene_iv, 50),
            "scene_interval_p25_s": pct(scene_iv, 25),
            "scene_interval_p75_s": pct(scene_iv, 75),
            "counts_by_type": counts,
            "animated_events": len(anim),
            "animated_per_min": round(len(anim) / minutes, 2),
            "anim_dur_median_frames": pct(anim_d, 50),
            "anim_dur_p25_frames": pct(anim_d, 25),
            "anim_dur_p75_frames": pct(anim_d, 75),
            "instant_events_per_min": round(len(instant) / minutes, 2),
            "still_ratio_pct": round(float((~active).mean() * 100), 1),
        },
        "regions": [{k: v for k, v in r.items() if not k.startswith("_") and k != "grid_bbox"} for r in regions],
        "audio": {k: v for k, v in audio.items() if not k.startswith("_")} if audio else None,
        "subtitles": sub_stats,
        "youtube": yt,
        "params": {"ANALYSIS_W": ANALYSIS_W, "CELL": CELL, "PIX_T": PIX_T, "MAJOR_NET": MAJOR_NET,
                   "FULL_NET": FULL_NET, "interval_s": args.interval},
    }


def write_outputs(out_dir, A, fps, segs, regions, summary, exclude) -> None:
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out_dir / "events.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["no", "type", "type_ja", "animated", "start_frame", "end_frame", "start_time", "dur_frames",
                    "dur_s", "peak_ratio", "net_ratio", "union_ratio", "net_bbox", "union_bbox", "main_bbox"])
        for k, s in enumerate(segs, 1):
            w.writerow([k, s["type"], TYPE_JA[s["type"]], int(s["anim"]), s["start"], s["end"], s["start_time"],
                        s["dur"], s["dur_s"], s["peak_ratio"], s["net_ratio"], s["union_ratio"],
                        json.dumps(s["net_bbox"]), json.dumps(s["union_bbox"]), json.dumps(s["main_bbox"])])
    with open(out_dir / "regions.csv", "w", newline="", encoding="utf-8-sig") as f:
        cols = ["id", "guess", "position", "bbox", "cells", "events", "events_per_min", "event_dur_median_frames",
                "bursts", "bursts_per_min", "burst_interval_median_s", "burst_interval_p25_s",
                "burst_interval_p75_s", "burst_dur_median_s", "active_pct"]
        w = csv.writer(f)
        w.writerow(cols)
        for r in regions:
            w.writerow([json.dumps(r[c]) if c == "bbox" else r[c] for c in cols])
    with open(out_dir / "frame_metrics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["frame", "time", "mad", "changed_ratio", "excluded_from_regions"])
        for k in range(A["n"]):
            w.writerow([k, f"{A['pts'][k]:.4f}", f"{A['mad'][k]:.3f}", f"{A['ratio'][k]:.5f}", int(exclude[k])])


def write_report(out_dir: Path, S: dict, regions: list[dict], strips: list[dict], sheets: dict) -> None:
    v, t, a, sub, yt = S["video"], S["tempo"], S["audio"], S["subtitles"], S["youtube"]
    L = [f"# フレーム分析レポート: {yt['title'] if yt else S['source_file']}", ""]
    if yt:
        L += [f"- URL: {yt['webpage_url']}", f"- チャンネル: {yt['channel']}（登録者 {yt['channel_follower_count']}）",
              f"- 投稿日: {yt['upload_date']} / 再生 {yt['view_count']} / 高評価 {yt['like_count']} / "
              f"コメント {yt['comment_count']}",
              f"- チャプター: {len(yt['chapters'])}個 / タグ: {len(yt['tags'])}個 / 説明文: {yt['description_chars']}文字", ""]
        if S.get("thumbnail"):
            L += ["![サムネイル](images/thumbnail.jpg)", ""]
    au = v["audio"] or {}
    L += ["## 1. 基本スペック", "",
          "| 項目 | 値 |", "|---|---|",
          f"| 解像度 | {v['width']}x{v['height']} |",
          f"| フレームレート | {v['fps']:.3f} fps（表示値 {v['fps_display']}） |",
          f"| 長さ(解析範囲) | {fmt_time(v['duration_analyzed_s'])}（{v['frames_analyzed']}フレーム） |",
          f"| 映像コーデック / ビットレート | {v['vcodec']} / {v['bitrate_kbps']} kb/s |",
          f"| 音声 | {au.get('codec')} {au.get('sample_rate')} Hz {au.get('channels')} |", ""]
    L += ["## 2. テンポ（画面がどれだけ・どう変わるか）", "",
          "| 指標 | 値 | 意味 |", "|---|---|---|",
          f"| 場面転換 | {t['scene_changes']}回（{t['scene_changes_per_min']}回/分） | 画面の15%以上が入れ替わる変化 |",
          f"| 場面転換の間隔 | 中央値 {t['scene_interval_median_s']}秒（25〜75%: {t['scene_interval_p25_s']}〜{t['scene_interval_p75_s']}秒） | 1場面の長さ |",
          f"| うち全面カット / 全面トランジション | {t['counts_by_type']['cut']} / {t['counts_by_type']['transition']} | 画面の半分以上が一瞬で / 数フレームかけて変わる |",
          f"| アニメーション付きの変化 | {t['animated_events']}回（{t['animated_per_min']}回/分） | 3フレーム以上かけて動く出現・移動・拡大など |",
          f"| アニメーションの長さ | 中央値 {t['anim_dur_median_frames']}フレーム（25〜75%: {t['anim_dur_p25_frames']}〜{t['anim_dur_p75_frames']}） | 動きのキビキビ感 |",
          f"| 一瞬で切り替わる変化 | {t['instant_events_per_min']}回/分 | 字幕の差し替え等（1〜2フレーム） |",
          f"| 静止フレームの割合 | {t['still_ratio_pct']}% | 何も動いていない時間 |", ""]
    L += ["## 3. よく変化する画面領域", "",
          "![変化ヒートマップ](images/change_heatmap.png)", "",
          "明るいほど頻繁に変化する場所。枠は自動検出した領域（推定は当て推量なので連番ストリップで目視確認）。", "",
          "| ID | 推定 | 位置 | 範囲(x0,y0,x1,y1) | 更新/分 | まとまり間隔(中央値) | まとまりの長さ | 動いている時間 |",
          "|---|---|---|---|---|---|---|---|"]
    for r in regions[:12]:
        L.append(f"| {r['id']} | {r['guess']} | {r['position']} | {r['bbox']} | {r['events_per_min']} | "
                 f"{r['burst_interval_median_s']}秒 | {r['burst_dur_median_s']}秒 | {r['active_pct']}% |")
    L += ["", "![レイアウト(中央値合成)](images/median_layout.png)", "",
          "中央値合成 = 動画全体を通して一番よく映っている状態。常設のレイアウト（キャラ配置・字幕枠・背景）が分かる。", ""]
    L += ["## 4. タイムライン", "", "![タイムライン](images/timeline.png)", "",
          "cuts=場面転換（赤:全面カット 紫:全面トランジション 橙:部分切替） / motion=1秒ごとの変化量 / "
          "audio=音量 / R*=各領域が動いている時間", ""]
    L += ["## 5. 音声", ""]
    if a:
        L += ["| 指標 | 値 | 目安 |", "|---|---|---|",
              f"| 統合ラウドネス | {a['integrated_lufs']} LUFS | YouTubeは-14 LUFS基準で音量を揃える |",
              f"| ラウドネスレンジ | {a['lra_lu']} LU | 小さいほど音量が一定 |",
              f"| トゥルーピーク | {a['true_peak_dbfs']} dBFS | -1付近なら音割れ対策済み |"]
        if "pauses" in a:
            L += [f"| 間（ま）の回数 | {a['pauses']}回（{a['pauses_per_min']}回/分） | 0.15秒以上の音量の落ち込み |",
                  f"| 間の長さ | 中央値 {a['pause_median_s']}秒（75%: {a['pause_p75_s']}秒） | 台詞と台詞の間 |",
                  f"| 発話の連続長 | 中央値 {a['speech_run_median_s']}秒 | |",
                  f"| 音量の分布 | 下位10% {a['level_p10_dbfs']} / 中央 {a['level_p50_dbfs']} / 上位10% {a['level_p90_dbfs']} dBFS | "
                  f"{'間でも無音にならない → BGM/環境音が常時' if a['never_silent'] else '間で無音近くまで落ちる'} |"]
        L.append("")
    else:
        L += ["音声なし", ""]
    L += ["## 6. 話速（字幕から）", ""]
    if sub:
        L += [f"- 字幕ファイル: {sub['source']}（本文は transcript.txt。第三者の著作物なので共有しないこと）",
              f"- 行数 {sub['lines']} / 文字数 {sub['chars']}",
              f"- **{sub['chars_per_min']}文字/分**（動画全体）" +
              (f" / 発話中だけで {sub['chars_per_speech_min']}文字/分" if sub.get("chars_per_speech_min") else ""),
              "- 目安: 一般的なナレーション300文字/分前後。自動字幕は誤認識を含むので概算値", ""]
    else:
        L += ["字幕なし（ローカルファイルは --subs で字幕ファイルを指定できる）", ""]
    if yt and yt["chapters"]:
        L += ["## 7. チャプター構成", ""] + [f"- {fmt_time(c['start'] or 0)} {c['title']}" for c in yt["chapters"]] + [""]
    L += ["## 8. 目視用の画像", "", "### フレーム単位の連番ストリップ", "",
          "転換・アニメーション・主要領域の更新を、前後のフレームを含めて1フレームずつ並べたもの。"
          "`f+0` がイベント開始フレーム。_zoom は変化した範囲の拡大。", ""]
    for s in strips:
        z = f" / [拡大]({'images/' + s['zoom']})" if s["zoom"] else ""
        L.append(f"- {fmt_time(s['time'])}（#{s['start']}, {s['dur_frames']}フレーム）{s['why']}: "
                 f"[images/{s['file']}](images/{s['file']}){z}")
    L += ["", "### 一覧シート", ""]
    L += [f"- キーフレーム: " + ", ".join(f"[{p}](images/{p})" for p in sheets["keyframes"])]
    L += [f"- 定間隔: " + ", ".join(f"[{p}](images/{p})" for p in sheets["interval"])]
    L += ["", "---", "", "数値の意味と、画像を見て確認するチェックリストは docs/06_video_frame_analysis.md を参照。", ""]
    (out_dir / "report.md").write_text("\n".join(L), encoding="utf-8")


# ---------------------------------------------------------------- channelコマンド

def normalize_channel_url(url: str) -> str:
    url = url.split("?")[0].rstrip("/")
    url = re.sub(r"/(videos|shorts|streams|featured|about|playlists)$", "", url)
    return re.sub(r"://(m\.)?youtube\.com", "://www.youtube.com", url)


def fetch_thumb(entry: dict, dest: Path) -> np.ndarray | None:
    if not dest.exists():
        thumbs = sorted((t for t in entry.get("thumbnails") or [] if t.get("url")),
                        key=lambda t: (t.get("width") or 0), reverse=True)
        urls = [t["url"] for t in thumbs] + [f"https://i.ytimg.com/vi/{entry['id']}/hqdefault.jpg"]
        for u in urls:
            try:
                with urllib.request.urlopen(u, timeout=20) as r:
                    dest.write_bytes(r.read())
                break
            except Exception:
                continue
    try:
        return np.asarray(Image.open(dest).convert("RGB"))
    except Exception:
        return None


def cmd_channel(args) -> None:
    import yt_dlp
    base = normalize_channel_url(args.url)
    out_dir = Path(args.out).resolve() if args.out else DEFAULT_OUT / ("channel_" + re.sub(r"\W", "", base.rsplit("/", 1)[-1]))
    (out_dir / "thumbs").mkdir(parents=True, exist_ok=True)
    img_dir = out_dir / "images"
    img_dir.mkdir(exist_ok=True)
    rows = []
    opts = ydl_base_opts(args) | {"extract_flat": "in_playlist", "skip_download": True,
                                  "playlistend": args.limit, "quiet": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for tab in ("videos", "shorts"):
            try:
                info = ydl.extract_info(f"{base}/{tab}", download=False)
            except yt_dlp.utils.DownloadError as e:
                print(f"  {tab} タブを取得できませんでした: {e}", file=sys.stderr)
                continue
            for order, e in enumerate(info.get("entries") or []):
                if e and e.get("id"):
                    rows.append({"tab": tab, "order": order, "id": e["id"], "title": e.get("title") or "",
                                 "duration": e.get("duration"), "view_count": e.get("view_count"),
                                 "url": f"https://www.youtube.com/watch?v={e['id']}", "_entry": e})
    if not rows:
        raise SystemExit("動画一覧を取得できませんでした")
    with open(out_dir / "channel_videos.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["tab", "order(0=最新)", "id", "title", "duration_s", "view_count", "url"])
        for r in rows:
            w.writerow([r["tab"], r["order"], r["id"], r["title"], r["duration"], r["view_count"], r["url"]])

    vids = [r for r in rows if r["tab"] == "videos"]
    shorts = [r for r in rows if r["tab"] == "shorts"]
    durs = np.array([r["duration"] for r in vids if r["duration"]], np.float64)
    L = [f"# チャンネル分析: {base}", "",
         f"- 通常動画 {len(vids)}本 / ショート {len(shorts)}本（取得上限 {args.limit}本/タブ）", ""]
    if len(durs):
        buckets = [(0, 300, "〜5分"), (300, 600, "5〜10分"), (600, 900, "10〜15分"), (900, 1200, "15〜20分"),
                   (1200, 1800, "20〜30分"), (1800, 1e9, "30分〜")]
        L += ["## 動画の長さ", "",
              f"- 中央値 {fmt_time(float(np.median(durs)))[:-3]} / 平均 {fmt_time(float(durs.mean()))[:-3]} / "
              f"最短 {fmt_time(float(durs.min()))[:-3]} / 最長 {fmt_time(float(durs.max()))[:-3]}", "",
              "| 長さ | 本数 |", "|---|---|"]
        L += [f"| {lab} | {int(((durs >= a) & (durs < b)).sum())} |" for a, b, lab in buckets] + [""]
    ranked = sorted((r for r in vids if r["view_count"]), key=lambda r: -r["view_count"])
    if ranked:
        L += ["## 再生数トップ10（通常動画）", "", "| # | タイトル | 長さ | 再生数 |", "|---|---|---|---|"]
        L += [f"| {k} | [{r['title']}]({r['url']}) | {fmt_time(r['duration'] or 0)[:-3]} | {r['view_count']:,} |"
              for k, r in enumerate(ranked[:10], 1)] + [""]
    titles = [r["title"] for r in vids]
    if titles:
        brackets = {}
        for tt in titles:
            for b in re.findall(r"【([^】]+)】", tt):
                brackets[b] = brackets.get(b, 0) + 1
        top_b = sorted(brackets.items(), key=lambda kv: -kv[1])[:15]
        rate = lambda pat: round(sum(1 for tt in titles if re.search(pat, tt)) / len(titles) * 100)
        L += ["## タイトルの傾向", "",
              f"- 文字数 中央値 {int(np.median([len(tt) for tt in titles]))}文字",
              f"- 【】を使う {rate('【')}% / ？を使う {rate('[?？]')}% / ！を使う {rate('[!！]')}% / "
              f"数字を含む {rate('[0-9０-９]')}%",
              "- よく使う【】: " + ("、".join(f"{b}({c})" for b, c in top_b) or "なし"), ""]
    picks = (ranked[:3] + [r for r in vids[:5] if r not in ranked[:3]])[:5]
    L += ["## 分析候補（再生数上位3本 + 最新）", ""]
    L += [f"- {r['title']}\n  `python scripts/analyze_video.py video {r['url']}`" for r in picks] + [""]

    for name, sel, title in (("thumbs_top", ranked[:args.thumbs], "top views"),
                             ("thumbs_recent", vids[:args.thumbs], "most recent")):
        items = []
        for r in sel:
            arr = fetch_thumb(r["_entry"], out_dir / "thumbs" / f"{r['id']}.jpg")
            if arr is not None:
                items.append((arr, f"{(r['view_count'] or 0):,} views"))
        if items:
            make_sheet(items, img_dir / f"{name}.jpg", f"thumbnails: {title}", cols=5, tile_w=384)
            L += [f"![{title}](images/{name}.jpg)", ""]
    (out_dir / "channel_report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"完了: {out_dir / 'channel_report.md'}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("video", help="動画1本をフレーム単位で分析")
    v.add_argument("source", help="YouTubeのURL または 動画ファイルのパス")
    v.add_argument("-o", "--out", help="出力フォルダ(既定: analysis/<動画ID>)")
    v.add_argument("--max-height", type=int, default=1080, help="ダウンロードする最大の高さ(既定1080)")
    v.add_argument("--interval", type=float, default=10.0, help="定間隔フレームの間隔(秒, 既定10)")
    v.add_argument("--max-keyframes", type=int, default=120, help="キーフレームの最大枚数(既定120)")
    v.add_argument("--strips", type=int, default=12, help="連番ストリップの枚数(既定12)")
    v.add_argument("--max-minutes", type=float, help="先頭から何分だけ解析するか(長尺向け)")
    v.add_argument("--subs", help="ローカル動画用の字幕ファイル(.vtt/.srt)")
    v.add_argument("--cookies-from-browser", help="ボット確認で止まる時に使うブラウザ名(chrome/edge/firefox)")
    c = sub.add_parser("channel", help="チャンネルの動画一覧を集計(長さ・再生数・タイトル・サムネ)")
    c.add_argument("url", help="チャンネルURL(例 https://www.youtube.com/@name)")
    c.add_argument("-o", "--out", help="出力フォルダ(既定: analysis/channel_<名前>)")
    c.add_argument("--limit", type=int, default=300, help="タブごとの最大取得本数(既定300)")
    c.add_argument("--thumbs", type=int, default=20, help="サムネ一覧に並べる本数(既定20)")
    c.add_argument("--cookies-from-browser", help="ボット確認で止まる時に使うブラウザ名(chrome/edge/firefox)")
    args = ap.parse_args()
    {"video": cmd_video, "channel": cmd_channel}[args.cmd](args)


if __name__ == "__main__":
    main()
