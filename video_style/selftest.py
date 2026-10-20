#!/usr/bin/env python3
"""analyze_video.py の検証。

正解が分かっている合成動画（カット・ズーム・パン・白フラッシュ・暗転・クロスフェード・
パンチイン・テロップ・無音・クリック音）を作り、解析結果が正解と一致するかを確かめる。
ネットワーク不要（顔検出はモデルが手元にある場合のみ動かす）。

usage: python video_style/selftest.py
"""

import csv
import json
import math
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyze_video as av  # noqa: E402

W, H, FPS = 640, 360, 30
N = 420
rng = np.random.default_rng(7)


def texture(hue_bgr, size=(int(W * 1.6), int(H * 1.6))):
    """特徴点が取れる抽象模様（円と四角の重なり）"""
    w, h = size
    img = Image.new("RGB", (w, h), tuple(int(c) for c in hue_bgr[::-1]))
    d = ImageDraw.Draw(img)
    for _ in range(260):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.integers(6, 40))
        col = tuple(int(np.clip(c + rng.integers(-90, 90), 0, 255)) for c in hue_bgr[::-1])
        if rng.random() < 0.5:
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
        else:
            d.rectangle([x - r, y - r // 2, x + r, y + r // 2], fill=col)
    return np.array(img)[:, :, ::-1].copy()


def crop(img, cx, cy, zoom):
    """中心(cx,cy)・倍率zoomで W×H を切り出す（zoom=1 で画像の 1/1.6 幅）"""
    import cv2
    ih, iw = img.shape[:2]
    cw, ch = iw / 1.6 / zoom, ih / 1.6 / zoom
    M = np.float32([[W / cw, 0, -(cx - cw / 2) * W / cw], [0, H / ch, -(cy - ch / 2) * H / ch]])
    return cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR)


def build_frames():
    A, B, C, D, E, F = (texture(np.array(c)) for c in
                        [(200, 80, 40), (40, 160, 220), (60, 200, 60), (180, 40, 180), (30, 60, 200), (200, 200, 60)])
    cx0, cy0 = A.shape[1] / 2, A.shape[0] / 2
    font = ImageFont.truetype(next(p for p in av.FONT_CANDIDATES if Path(p).exists()), 30)
    frames = []
    for n in range(N):
        if n < 60:
            f = crop(A, cx0, cy0, 1.0)
            if 15 <= n < 46:
                im = Image.fromarray(f[:, :, ::-1].copy())
                ImageDraw.Draw(im).text((W // 2, 315), "テスト字幕その一", font=font, anchor="mm",
                                        fill=(255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0))
                f = np.array(im)[:, :, ::-1].copy()
        elif n < 90:
            f = crop(B, cx0, cy0, 1.0)
        elif n < 150:
            f = crop(C, cx0, cy0, 1.0 + 0.25 * (n - 90) / 59)
        elif n < 153:
            f = np.full((H, W, 3), 255, np.uint8)
        elif n < 223:
            k = min(n, 212) - 153
            f = crop(D, cx0 - 0.075 * A.shape[1] / 1.6 + 0.15 * A.shape[1] / 1.6 * k / 59, cy0, 1.0)
            if n >= 213:
                f = (f * (1 - (n - 212) / 10)).astype(np.uint8)
        elif n < 233:
            f = np.zeros((H, W, 3), np.uint8)
        elif n < 300:
            f = crop(E, cx0, cy0, 1.0)
            if n < 243:
                f = (f * ((n - 233) / 10)).astype(np.uint8)
        elif n < 315:
            a = (n - 300 + 1) / 16
            f = ((1 - a) * crop(E, cx0, cy0, 1.0) + a * crop(F, cx0, cy0, 1.0)).astype(np.uint8)
        elif n < 390:
            f = crop(F, cx0, cy0, 1.0)
        else:
            f = crop(F, cx0, cy0, 1.2)
        frames.append(f)
    return frames


def build_audio(path: Path):
    sr = 48000
    t = np.arange(int(N / FPS * sr)) / sr
    voice = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 8))
    env = 0.5 * (1 + np.sin(2 * np.pi * 5 * t)) ** 2
    x = 0.12 * voice * env
    x[(t >= 4.0) & (t < 6.0)] = 0.0  # 完全な無音
    for c in (2.0, 3.0):  # カットに合わせたクリック音
        i = int(c * sr)
        x[i:i + 480] += 0.8 * np.exp(-np.arange(480) / 60) * np.sign(np.sin(np.arange(480) * 0.9))
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def encode(frames, wav: Path, out: Path):
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                          "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(wav),
                          "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-b:a", "160k", "-shortest", str(out)], stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(f.tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError("ffmpeg encode failed")


FAIL = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'NG'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAIL.append(name)


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        vid, wav, out = td / "synthetic.mp4", td / "a.wav", td / "out"
        print("合成動画を作成中…")
        build_audio(wav)
        encode(build_frames(), wav, vid)
        face_step = "5" if av.DEFAULT_FACE_MODEL.exists() else "0"
        t0 = time.time()
        s = av.main([str(vid), "-o", str(out), "--face-step", face_step])
        print(f"解析時間: {time.time() - t0:.1f}秒\n検証:")

        ev = json.loads((out / "events.json").read_text(encoding="utf-8"))
        bnd = {b["frame"]: b["type"] for b in ev["boundaries"]}
        shots = list(csv.DictReader(open(out / "shots.csv", encoding="utf-8-sig")))
        telops = list(csv.DictReader(open(out / "telops.csv", encoding="utf-8-sig")))

        def near(frame, kinds, tol=1):
            return any(abs(f - frame) <= tol and k in kinds for f, k in bnd.items())

        check("カット@60", near(60, {"cut"}))
        check("カット@90", near(90, {"cut"}))
        check("パンチインのカット@390", near(390, {"cut"}))
        check("白フラッシュ 150-153", near(150, {"flash_start"}) and near(153, {"flash_end"}))
        blk = [sp for sp in ev["specials"] if sp["kind"] == "black"]
        check("暗転を1回検出", len(blk) == 1, str(blk))
        if blk:
            check("フェードアウト/インのコマ数", blk[0]["fade_out_frames"] >= 5 and blk[0]["fade_in_frames"] >= 5,
                  f"out={blk[0]['fade_out_frames']} in={blk[0]['fade_in_frames']}")
        diss = [g for g in ev["gradual"] if g["type"] == "dissolve"]
        check("クロスフェード（300-315）", any(298 <= g["center"] <= 318 for g in diss), str(ev["gradual"]))
        expected = {60, 90, 150, 153, 390}
        extra = [f for f, k in bnd.items() if k == "cut" and not any(abs(f - e) <= 1 for e in expected)]
        check("余計なカットが無い", not extra, f"extra={extra}")

        def shot_at(frame):
            return next(sh for sh in shots if int(sh["start"]) <= frame < int(sh["end"]))
        z = shot_at(120)
        check("ズームイン判定（+25%）", z["motion"] == "zoom_in" and 18 <= float(z["zoom_pct"]) <= 32,
              f"{z['motion']} {z['zoom_pct']}%")
        p = shot_at(180)
        check("右パン判定（+15%）", p["motion"] == "pan_right" and 10 <= float(p["pan_x_pct"]) <= 20,
              f"{p['motion']} {p['pan_x_pct']}%")
        st = shot_at(30)
        check("静止ショット判定", st["motion"] in ("still", "fixed"), st["motion"])
        pi = shot_at(400)
        check("パンチイン（同じ構図を約1.2倍）", pi.get("jump") == "punch_in" and abs(float(pi["jump_scale"]) - 1.2) < 0.05,
              f"{pi.get('jump')} {pi.get('jump_scale')}")
        e = shot_at(260)
        check("暗転明けはフェードイン", e["transition_in"] == "fade_in", e["transition_in"])

        main_tel = [t for t in telops if 12 <= int(t["start"]) <= 18 and t["v_region"] == "bottom"]
        check("テロップ（15-45フレーム・下部中央）", bool(main_tel), f"{len(telops)}件検出")
        if main_tel:
            t = main_tel[0]
            check("テロップの表示時間 約1秒", 0.8 <= float(t["dur_sec"]) <= 1.3, t["dur_sec"])
            check("テロップは中央揃え", t["h_align"] == "center", t["h_align"])
            check("テロップの文字色=白系・縁=黒系", (t["fill"] or "").startswith("#F") and (t["outline"] or "#FF")[1] in "0123",
                  f"fill={t['fill']} outline={t['outline']}")
        spurious = [t for t in telops if not (12 <= int(t["start"]) <= 18)]
        check("テロップの誤検出が少ない（2件以下）", len(spurious) <= 2, f"{len(spurious)}件")

        a = s["audio"]
        check("ラウドネス計測", a and a["integrated_lufs"] is not None, str(a and a["integrated_lufs"]))
        rms = [float(r["rms_db"]) for r in csv.DictReader(open(out / "frames.csv", encoding="utf-8"))]
        quiet = rms[int(4.2 * FPS):int(5.8 * FPS)]
        check("無音区間の音量", max(quiet) < -70, f"max={max(quiet):.1f}dB")
        on = ev["audio_onsets_sec"]
        check("クリック音の立ち上がり検出", all(any(abs(o - c) < 0.05 for o in on) for c in (2.0, 3.0)))
        for f in ["summary.json", "style_profile.yaml", "report.md", "timeline.png", "frames.csv"]:
            check(f"出力 {f}", (out / f).exists())
        check("コンタクトシート", any((out / "sheets").glob("*.jpg")))
        check("コマ送り画像", any((out / "transitions").glob("*.jpg")))
        check("frames.csv が全フレーム", len(rms) == N, f"{len(rms)}行")

    print("\n結果:", "すべてOK" if not FAIL else f"NG {len(FAIL)}件: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
