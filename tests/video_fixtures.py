"""動画解析テスト用の合成動画(正解つき)を作る。GPU・外部素材不要。

宇宙系解説動画を模したショット(星空+惑星、テクスチャ画像のズーム/パン、
ディゾルブ、黒フェード、フラッシュ、テロップ)を numpy で描画し ffmpeg で符号化する。
"""

import math
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 640, 360, 30
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "C:/Windows/Fonts/meiryob.ttc", "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
]


def _font(size):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def texture(seed: int, w: int, h: int) -> np.ndarray:
    """多重スケールのカラーノイズ(特徴点が十分ある画)。"""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32)
    for octave, amp in ((8, 1.0), (24, 0.6), (64, 0.35), (160, 0.2)):
        small = rng.random((max(2, h * octave // w), octave, 3)).astype(np.float32)
        img += amp * cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    img = (img - img.min()) / (img.max() - img.min())
    tint = rng.random(3) * 0.6 + 0.4
    return (np.clip(img * tint, 0, 1) * 255).astype(np.uint8)


def space_frame(seed: int, planet_xy=(0.6, 0.5), radius=0.28, w=W, h=H) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32)
    n = 350
    xs, ys = rng.integers(0, w, n), rng.integers(0, h, n)
    br = rng.random(n) ** 3
    for x, y, b in zip(xs, ys, br):
        cv2.circle(img, (int(x), int(y)), 1 if b < 0.6 else 2, (0.5 + 0.5 * b,) * 3, -1)
    cx, cy, r = planet_xy[0] * w, planet_xy[1] * h, radius * h
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = (xx - cx) / r, (yy - cy) / r
    d2 = dx * dx + dy * dy
    inside = d2 < 1
    nz = np.sqrt(np.clip(1 - d2, 0, 1))
    light = np.clip(-0.6 * dx - 0.3 * dy + 0.75 * nz, 0, 1)
    tex = texture(seed + 99, w, h).astype(np.float32) / 255
    col = np.array([0.35, 0.55, 0.95], np.float32) if seed % 2 == 0 else \
        np.array([0.9, 0.5, 0.3], np.float32)
    surf = (0.55 * col + 0.45 * tex) * light[..., None]
    img[inside] = surf[inside]
    glow = np.exp(-np.clip(np.sqrt(d2) - 1, 0, None) * 12)[..., None] * col * 0.35
    img = np.where(inside[..., None], img, img + glow)
    return (np.clip(img, 0, 1) * 255).astype(np.uint8)


def warp(img: np.ndarray, zoom: float, cx_shift: float, cy_shift: float, w=W, h=H):
    """大きめの原画から、拡大率zoom・中心ずれ(画面比)で切り出す。"""
    ih, iw = img.shape[:2]
    scale = zoom * w / iw * 1.3
    m = np.array([[scale, 0, w / 2 - scale * (iw / 2 + cx_shift * iw)],
                  [0, scale, h / 2 - scale * (ih / 2 + cy_shift * ih)]], np.float32)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def draw_telop(frame: np.ndarray, text: str) -> np.ndarray:
    im = Image.fromarray(frame)
    d = ImageDraw.Draw(im)
    f = _font(int(H * 0.075))
    tw = d.textlength(text, font=f)
    d.text(((W - tw) / 2, H * 0.80), text, font=f, fill=(255, 255, 255),
           stroke_width=3, stroke_fill=(10, 10, 10))
    return np.asarray(im)


def build_fixture(path: Path) -> dict:
    """合成動画を書き出して正解データを返す。"""
    big_a = texture(1, 1280, 720)
    big_b = texture(2, 1280, 720)
    big_c = texture(3, 1280, 720)
    frames = []
    truth = {"cuts": [], "dissolves": [], "fades": [], "flashes": [], "shots": []}

    def add_shot(n, fn, camera, telop=False):
        start = len(frames)
        for i in range(n):
            fr = fn(i, n)
            if telop:
                fr = draw_telop(fr, "もしも地球が止まったら")
            frames.append(fr)
        truth["shots"].append({"start": start, "end": len(frames), "camera": camera,
                               "telop": telop})

    ease = lambda x: x  # noqa: E731
    # 0: 宇宙 静止 2秒
    add_shot(60, lambda i, n: space_frame(10), "static")
    truth["cuts"].append(len(frames))
    # 1: テクスチャ ズームイン 12% / 3秒 (線形)
    add_shot(90, lambda i, n: warp(big_a, 1.0 + 0.12 * ease(i / (n - 1)), 0, 0), "zoom_in", telop=True)
    truth["cuts"].append(len(frames))
    # 2: 右パン(内容は左へ流れる) 画面の8% / 3秒, ease-in-out
    add_shot(90, lambda i, n: warp(big_b, 1.0, 0.08 * 1.3 * (lambda x: x * x * (3 - 2 * x))(i / (n - 1)) / 1.3, 0),
             "pan_right")
    # 3: ディゾルブ 15フレームで big_b(最終位置) → big_c 静止
    last_b = frames[-1].astype(np.float32)
    c_img = warp(big_c, 1.0, 0, 0)
    d_start = len(frames)
    for i in range(15):
        a = (i + 1) / 16
        frames.append(((1 - a) * last_b + a * c_img.astype(np.float32)).astype(np.uint8))
    truth["dissolves"].append({"start": d_start, "end": len(frames)})
    add_shot(75, lambda i, n: c_img, "static", telop=True)
    # 4: 黒フェード(アウト8f + 黒6f + イン8f) → ズームアウト 宇宙
    last = frames[-1].astype(np.float32)
    f_start = len(frames)
    for i in range(8):
        frames.append((last * (1 - (i + 1) / 8)).astype(np.uint8))
    for i in range(6):
        frames.append(np.zeros((H, W, 3), np.uint8))
    sp = space_frame(11, (0.45, 0.5), 0.3)
    sp_big = cv2.resize(sp, (int(W * 1.6), int(H * 1.6)), interpolation=cv2.INTER_CUBIC)
    first = warp(sp_big, 1.15, 0, 0)
    for i in range(8):
        frames.append((first.astype(np.float32) * (i + 1) / 8).astype(np.uint8))
    truth["fades"].append({"start": f_start, "end": len(frames)})
    add_shot(90, lambda i, n: warp(sp_big, 1.15 - 0.12 * (i / (n - 1)), 0, 0), "zoom_out")
    truth["cuts"].append(len(frames))
    # 5: 静止テクスチャ + 途中で2フレームの白フラッシュ
    tex_d = warp(texture(4, 1280, 720), 1.0, 0, 0)
    flash_at = len(frames) + 30
    add_shot(70, lambda i, n: np.full_like(tex_d, 250) if 30 <= i < 32 else tex_d, "static")
    truth["flashes"].append(flash_at)
    truth["cuts"].append(len(frames))
    # 6: 似た宇宙の別カット(惑星の位置違い)
    add_shot(60, lambda i, n: space_frame(12, (0.3, 0.45), 0.25), "static")

    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-shortest", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(path)],
        stdin=subprocess.PIPE)
    for fr in frames:
        proc.stdin.write(np.ascontiguousarray(fr).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("fixture encode failed")
    truth["n_frames"] = len(frames)
    truth["fps"] = FPS
    return truth


def build_audio_fixture(path: Path, seconds: float = 12.0, sr: int = 48000) -> dict:
    """音声のみの正解つきフィクスチャ: 「発話」(変調トーン)区間と無音、既知のラウドネス。"""
    t = np.arange(int(seconds * sr)) / sr
    speech = np.zeros_like(t)
    segs = [(0.5, 3.0), (3.6, 6.2), (7.0, 10.5)]
    for s, e in segs:
        m = (t >= s) & (t < e)
        # 5Hzの音節状AM変調をかけた有声音もどき(基本周波数150Hz、500Hz/1500Hz付近にフォルマント)
        car = sum((1.0 / k) * (1 + 2.5 * math.exp(-((150 * k - 500) / 250) ** 2)
                               + 1.5 * math.exp(-((150 * k - 1500) / 400) ** 2))
                  * np.sin(2 * math.pi * 150 * k * t) for k in range(1, 20)) * 0.35
        am = 0.55 + 0.45 * np.sin(2 * math.pi * 5 * t)
        speech[m] = (0.25 * car * am)[m]
    bgm = 0.02 * np.sin(2 * math.pi * 110 * t)
    y = (speech + bgm).astype(np.float32)
    stereo = np.stack([y, y], axis=1)
    import soundfile as sf
    sf.write(str(path), stereo, sr)
    return {"speech_segments": segs, "duration": seconds}
