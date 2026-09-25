#!/usr/bin/env python3
"""正解付きの合成テスト動画を作る (tests/run_video_selftest.py から使う)。

解説動画でよく使われる編集を、フレーム番号の正解つきで一通り入れてある:
  ハードカット / ズームカット(1.3倍) / ケンバーンズ(1.0→1.2倍) / ディゾルブ / 黒フェード /
  白フラッシュ / ジャンプカット(同一構図で人物だけ飛ぶ) / シェイク /
  テロップ(即出し・差し替え・フェードイン・ポップ) / 常駐ロゴ /
  話し声風の音(既知の無音区間つき) + BGM + テロップ等に合わせた効果音
絵柄はすべてこのスクリプトで描く図形とASCII文字のみ。

usage (単体で動画だけ作る): python tests/synth_video.py out.mp4
"""

import subprocess
import sys
import wave
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from video_io import ffmpeg_exe

W, H, FPS, N = 1280, 720, 30, 900
SR = 48000

LOGO = (24, 20, 150, 56)  # x, y, w, h (px)
BGM_DBFS = -38.0
SPEECH_DBFS = -18.0

# (開始, 終了(含まない), 文字, 位置, 入り方, 塗り, 縁)
TELOPS = [
    (15, 75, "HELLO WORLD", "bottom", "cut", (255, 255, 255), (0, 0, 0)),
    (75, 140, "SECOND LINE", "bottom", "cut", (255, 255, 255), (0, 0, 0)),
    (150, 230, "THIRD CAPTION", "bottom", "fade", (255, 255, 255), (0, 0, 0)),
    (520, 600, "BIG!", "center", "pop", (255, 230, 0), (220, 0, 0)),
    (700, 790, "SHAKE TEST", "bottom", "cut", (255, 255, 255), (0, 0, 0)),
]
POP_SCALES = [0.4, 0.7, 1.0, 1.15, 1.05]  # 520..524 (525以降は1.0)
FADE_IN = 6

GROUND_TRUTH = {
    "hard_cuts": [90, 240, 810],
    "zoom_cut": {"frame": 180, "scale": 1.3},
    "jump_cut": 585,
    "flash": 675,
    "dissolve": (360, 374),
    "fade": (465, 494),
    "ken_burns": {"start": 270, "end": 330, "scale": 1.2},
    "shake": (720, 736),
    "telops": TELOPS,
    "logo": LOGO,
    "bgm_dbfs": BGM_DBFS,
}


def _rng(seed):
    return np.random.default_rng(seed)


WORDS = ["menu", "settings", "search", "account", "new chat", "upload", "share", "history",
         "model", "pricing", "docs", "export", "slides", "sheets", "agent", "prompt"]


def scene_a_bg():
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    img = np.stack([40 + 60 * x / W, 70 + 40 * y / H, 140 + 60 * (1 - y / H)], -1)
    rng = _rng(1)
    for _ in range(70):
        c = (int(rng.integers(0, W)), int(rng.integers(0, H)))
        cv2.circle(img, c, int(rng.integers(6, 30)), rng.integers(0, 255, 3).tolist(), -1)
    return img.clip(0, 255).astype(np.uint8)


def draw_person(img, f, cx_norm):
    sway = 6 * np.sin(2 * np.pi * 0.5 * f / FPS)
    nod = 3 * np.sin(2 * np.pi * 1.3 * f / FPS)
    cx, hy = int(cx_norm * W + sway), int(330 + nod)
    cv2.ellipse(img, (cx, 720), (210, 260), 0, 180, 360, (45, 45, 70), -1)
    for k in range(-3, 4):  # 服の柄(特徴点用)
        cv2.line(img, (cx + 40 * k, 500), (cx + 40 * k + 10, 720), (80, 80, 120), 6)
    cv2.circle(img, (cx, hy), 95, (230, 190, 160), -1)
    cv2.ellipse(img, (cx, hy - 40), (100, 60), 0, 180, 360, (60, 35, 20), -1)
    for dx in (-35, 35):
        cv2.circle(img, (cx + dx, hy - 10), 10, (30, 30, 30), -1)
    mouth = 4 + int(10 * abs(np.sin(2 * np.pi * 3 * f / FPS)))
    cv2.ellipse(img, (cx, hy + 45), (28, mouth), 0, 0, 360, (120, 30, 40), -1)


def scene_b():
    img = np.full((H, W, 3), 250, np.uint8)
    cv2.rectangle(img, (0, 0), (W, 60), (225, 228, 235), -1)
    cv2.rectangle(img, (0, 60), (220, H), (240, 242, 246), -1)
    rng = _rng(2)
    for i in range(10):
        cv2.putText(img, WORDS[i], (24, 110 + 40 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (70, 70, 80), 1, cv2.LINE_AA)
    y = 110
    while y < H - 30:
        words = " ".join(rng.choice(WORDS, int(rng.integers(3, 9))))
        cv2.putText(img, words, (260, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (60, 60, 70), 1, cv2.LINE_AA)
        y += int(rng.choice([26, 26, 26, 52]))
    cv2.rectangle(img, (1000, 80), (1180, 130), (40, 120, 230), -1)
    cv2.putText(img, "Upgrade", (1040, 113), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return img


def draw_cursor(img, f):
    x = int(640 + 220 * np.sin(f / 40))
    y = int(330 + 120 * np.cos(f / 55))
    pts = np.array([[x, y], [x, y + 22], [x + 6, y + 16], [x + 14, y + 20]], np.int32)
    cv2.fillPoly(img, [pts], (10, 10, 10))


def scene_c_base():
    rng = _rng(3)
    img = cv2.resize(rng.random((45, 80, 3)).astype(np.float32), (2 * W, 2 * H), interpolation=cv2.INTER_CUBIC)
    img = img * 180 + 40
    for _ in range(400):
        c = (int(rng.integers(0, 2 * W)), int(rng.integers(0, 2 * H)))
        col = rng.integers(0, 255, 3).tolist()
        if rng.random() < 0.5:
            cv2.circle(img, c, int(rng.integers(4, 26)), col, -1)
        else:
            cv2.rectangle(img, c, (c[0] + int(rng.integers(8, 50)), c[1] + int(rng.integers(8, 50))), col, -1)
    return img.clip(0, 255).astype(np.uint8)


def scene_d():
    img = np.full((H, W, 3), (235, 225, 200), np.uint8)
    rng = _rng(4)
    for _ in range(60):
        c = (int(rng.integers(0, W)), int(rng.integers(0, H)))
        col = rng.integers(0, 255, 3).tolist()
        if rng.random() < 0.5:
            cv2.circle(img, c, int(rng.integers(10, 60)), col, -1)
        else:
            cv2.rectangle(img, c, (c[0] + int(rng.integers(20, 120)), c[1] + int(rng.integers(20, 120))), col, -1)
    return img


def scene_e():
    y, x = np.mgrid[0:H, 0:W]
    img = np.zeros((H, W, 3), np.uint8)
    img[..., 1] = (120 + 60 * np.sin(x / 23.0)).astype(np.uint8)
    img[..., 0] = (60 + 40 * np.sin(y / 31.0)).astype(np.uint8)
    img[..., 2] = 80
    rng = _rng(5)
    for _ in range(80):
        c = (int(rng.integers(0, W)), int(rng.integers(0, H)))
        cv2.circle(img, c, int(rng.integers(5, 25)), rng.integers(0, 255, 3).tolist(), -1)
    return img


def zoom_about(img, scale, cx, cy):
    """画像を点(cx, cy)[正規化座標]を中心にscale倍する(ズームカット用)"""
    px, py = cx * img.shape[1], cy * img.shape[0]
    m = np.float32([[scale, 0, (1 - scale) * px], [0, scale, (1 - scale) * py]])
    return cv2.warpAffine(img, m, (img.shape[1], img.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def ken_burns(base2x, scale):
    """2倍解像度の素材から中心固定でscale倍に切り出す"""
    s = 0.5 * scale
    m = np.float32([[s, 0, W / 2 - s * W], [0, s, H / 2 - s * H]])
    return cv2.warpAffine(base2x, m, (W, H), flags=cv2.INTER_AREA)


def kb_scale(f):
    ks = GROUND_TRUTH["ken_burns"]
    if f < ks["start"]:
        return 1.0
    if f >= ks["end"]:
        return ks["scale"]
    return 1.0 + (ks["scale"] - 1.0) * (f - ks["start"]) / (ks["end"] - ks["start"])


def text_sprite(text, fill, outline, font_scale, bold, outline_px):
    """縁取り文字のRGB画像とアルファ。OpenCV 5のputTextは太さ指定が効かないため膨張で太らせる"""
    font = cv2.FONT_HERSHEY_DUPLEX
    (tw, th), base = cv2.getTextSize(text, font, font_scale, 2)
    pad = outline_px + bold + 4
    mask = np.zeros((th + base + 2 * pad, tw + 2 * pad), np.uint8)
    cv2.putText(mask, text, (pad, pad + th), font, font_scale, 255, 2, cv2.LINE_AA)
    disk = lambda r: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    fill_a = cv2.dilate(mask, disk(bold)) if bold else mask
    out_a = cv2.dilate(fill_a, disk(outline_px))
    canvas = np.zeros(mask.shape + (3,), np.uint8)
    canvas[:] = outline
    f = fill_a.astype(np.float32)[..., None] / 255
    canvas = (canvas * (1 - f) + np.array(fill, np.float32) * f).astype(np.uint8)
    return canvas, out_a


def paste(img, sprite, alpha, cx, cy, scale=1.0, opacity=1.0):
    if scale != 1.0:
        sprite = cv2.resize(sprite, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        alpha = cv2.resize(alpha, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    h, w = alpha.shape
    x0, y0 = int(cx - w / 2), int(cy - h / 2)
    xs, ys = max(0, x0), max(0, y0)
    xe, ye = min(img.shape[1], x0 + w), min(img.shape[0], y0 + h)
    a = (alpha[ys - y0:ye - y0, xs - x0:xe - x0].astype(np.float32) / 255 * opacity)[..., None]
    roi = img[ys:ye, xs:xe].astype(np.float32)
    img[ys:ye, xs:xe] = (roi * (1 - a) + sprite[ys - y0:ye - y0, xs - x0:xe - x0] * a).astype(np.uint8)


class Renderer:
    def __init__(self):
        self.a_bg = scene_a_bg()
        self.b = scene_b()
        self.c2x = scene_c_base()
        self.d = scene_d()
        self.e = scene_e()
        self.sprites = {}
        for t in TELOPS:
            big = t[3] == "center"
            self.sprites[t[2]] = text_sprite(t[2], t[5], t[6], 3.2 if big else 1.6, 3 if big else 1,
                                             9 if big else 5)
        self.logo = np.zeros((LOGO[3], LOGO[2], 3), np.uint8)
        self.logo[:] = (60, 20, 90)
        cv2.putText(self.logo, "LOGO", (22, 40), cv2.FONT_HERSHEY_DUPLEX, 1.2, (255, 255, 255), 2, cv2.LINE_AA)
        rng = _rng(6)
        self.shake = {f: (int(rng.integers(-10, 11)), int(rng.integers(-10, 11)))
                      for f in range(*GROUND_TRUTH["shake"])}

    def scene_a(self, f, cx):
        img = self.a_bg.copy()
        draw_person(img, f, cx)
        return img

    def base(self, f):
        if f < 90:
            return self.scene_a(f, 0.5)
        if f < 180:
            img = self.b.copy()
            draw_cursor(img, f)
            return img
        if f < 240:
            return zoom_about(self.b, 1.3, 0.6, 0.4)
        if f < 360:
            return ken_burns(self.c2x, kb_scale(f))
        if f < 375:  # ディゾルブ C→D
            a = (f - 359) / 16
            return (ken_burns(self.c2x, 1.2) * (1 - a) + self.d * a).astype(np.uint8)
        if f < 465:
            return self.d.copy()
        if f < 480:  # 黒へフェードアウト
            return (self.d * (1 - (f - 464) / 16)).astype(np.uint8)
        if f < 495:  # 黒からフェードイン
            return (self.scene_a(f, 0.5) * ((f - 479) / 16)).astype(np.uint8)
        if f < 585:
            return self.scene_a(f, 0.5)
        if f < 675:
            return self.scene_a(f, 0.43)  # ジャンプカット: 同じ背景で人物だけ位置が飛ぶ
        if f < 810:
            img = self.e.copy()
            if f in self.shake:
                dx, dy = self.shake[f]
                img = cv2.warpAffine(img, np.float32([[1, 0, dx], [0, 1, dy]]), (W, H), borderMode=cv2.BORDER_REFLECT)
            white = {675: 1.0, 676: 1.0, 677: 0.6, 678: 0.3}.get(f, 0.0)  # 白フラッシュ
            if white:
                img = (img * (1 - white) + 255 * white).astype(np.uint8)
            return img
        img = self.b.copy()
        draw_cursor(img, f)
        return img

    def frame(self, f):
        img = self.base(f)
        for start, end, text, pos, anim, _, _ in TELOPS:
            if not start <= f < end:
                continue
            spr, alpha = self.sprites[text]
            cy = 0.88 * H if pos == "bottom" else 0.45 * H
            scale, opacity = 1.0, 1.0
            if anim == "fade" and f - start < FADE_IN:
                opacity = (f - start + 1) / FADE_IN
            if anim == "pop" and f - start < len(POP_SCALES):
                scale = POP_SCALES[f - start]
            paste(img, spr, alpha, W / 2, cy, scale, opacity)
        x, y, w, h = LOGO
        img[y:y + h, x:x + w] = self.logo
        return img


def speech_segments():
    """(開始秒, 終了秒)の発話区間。区間の間が正解の無音(ジェットカットの間)"""
    durs = [1.6, 0.9, 2.2, 1.2, 1.8, 0.7, 2.5, 1.1, 1.4, 2.0, 0.8, 1.7]
    gaps = [0.25, 0.4, 0.15, 0.6, 0.3, 0.2, 0.5, 0.35, 0.25, 0.45, 0.3, 0.2]
    t, segs, k = 0.3, [], 0
    while True:
        d = durs[k % len(durs)]
        if t + d > N / FPS - 0.4:
            break
        segs.append((round(t, 3), round(t + d, 3)))
        t += d + gaps[k % len(gaps)]
        k += 1
    return segs


def se_times():
    """効果音を入れる時刻(秒): テロップの出現・ズームカット・フラッシュ"""
    frames = [15, 150, 180, 520, 675, 700]
    return [f / FPS for f in frames]


def make_audio():
    n = int(N / FPS * SR)
    t = np.arange(n) / SR
    amp = 10 ** (BGM_DBFS / 20) / np.sqrt(1.5)
    bgm = amp * (np.sin(2 * np.pi * 220 * t) + np.sin(2 * np.pi * 277.18 * t) + np.sin(2 * np.pi * 329.63 * t))

    rng = _rng(7)
    noise = rng.standard_normal(n)
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, 1 / SR)
    spec[(freqs < 300) | (freqs > 3400)] = 0
    voice = np.fft.irfft(spec, n)
    voice /= np.sqrt(np.mean(voice ** 2))
    env = np.zeros(n)
    for a, b in speech_segments():
        i0, i1 = int(a * SR), int(b * SR)
        seg_t = t[i0:i1] - a
        e = 0.55 + 0.45 * np.abs(np.sin(np.pi * 4.5 * seg_t))
        ramp = np.minimum(1, np.minimum(seg_t, (b - a) - seg_t) / 0.01)  # 10msの立ち上がり
        env[i0:i1] = e * ramp
    voice = voice * env * 10 ** (SPEECH_DBFS / 20) / 0.8

    se = np.zeros(n)
    for ts in se_times():
        i0 = int(ts * SR)
        k = np.arange(int(0.06 * SR))
        burst = np.exp(-k / (0.012 * SR)) * (np.sin(2 * np.pi * 1800 * k / SR) + 0.5 * rng.standard_normal(len(k)))
        se[i0:i0 + len(k)] += 0.45 * burst[: max(0, min(len(k), n - i0))]
    return (bgm + voice + se).clip(-1, 1).astype(np.float32)


def write_wav(path, audio):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes((audio * 32767).astype("<i2").tobytes())


def make_video(out_path: Path):
    out_path = Path(out_path)
    wav = out_path.with_suffix(".wav")
    write_wav(wav, make_audio())
    cmd = [ffmpeg_exe(), "-hide_banner", "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(wav), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    r = Renderer()
    for f in range(N):
        proc.stdin.write(np.ascontiguousarray(r.frame(f)).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("合成動画のエンコードに失敗しました")
    wav.unlink()
    return out_path


if __name__ == "__main__":
    make_video(Path(sys.argv[1] if len(sys.argv) > 1 else "synth.mp4"))
