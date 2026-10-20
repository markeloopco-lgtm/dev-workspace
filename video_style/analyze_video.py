#!/usr/bin/env python3
"""動画の編集スタイルを「全フレーム」解析して、数値化・可視化・再現用プロファイル化する。

使い方:
    python video_style/analyze_video.py <動画ファイル or YouTube URL> -o analysis/<名前>

出力（-o で指定したフォルダ）:
    summary.json         スタイル統計（カット頻度・テロップ・色・音など）
    style_profile.yaml   再現用プロファイル（定量部分 + 目視レビュー記入欄）
    report.md            上記の日本語サマリ
    frames.csv           全フレームの指標（1行 = 1フレーム）
    shots.csv            ショット（カットとカットの間）一覧
    telops.csv           テロップ（画面上の文字）の出現一覧
    events.json          フラッシュ・暗転・ズーム演出・画面揺れなどのイベント
    keyframes/           各ショットの代表フレーム
    sheets/              代表フレームの一覧画像（目視レビュー用）
    transitions/         切り替わり前後のコマ送り画像（目視レビュー用）
    timeline.png         時系列グラフ
    static_overlay.png   常時表示の枠・ロゴ領域の推定

依存: ffmpeg/ffprobe（PATHに必要）, numpy, opencv-python-headless, Pillow, PyYAML,
      rapidocr-onnxruntime（テロップ検出）, webrtcvad-wheels（発話区間）, yt-dlp（URL指定時）
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import yaml
except ImportError:  # PyYAMLが無ければ style_profile は JSON 形式で書く
    yaml = None

HERE = Path(__file__).resolve().parent
YUNET_URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
             "models/face_detection_yunet/face_detection_yunet_2023mar.onnx")
DEFAULT_FACE_MODEL = HERE / "models" / "face_detection_yunet_2023mar.onnx"
GRAD_K = 8        # なだらかな切り替わり（ディゾルブ等）検出で比較するフレーム間隔
TINY_W = 80       # 簡易比較用の極小画像の幅
NAN = float("nan")
FONT_CANDIDATES = [
    "C:/Windows/Fonts/meiryo.ttc", "C:/Windows/Fonts/YuGothM.ttc", "C:/Windows/Fonts/msgothic.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


# ---------------------------------------------------------------- 小道具

def log(msg: str) -> None:
    print(msg, flush=True)


def tc(sec: float) -> str:
    """秒 → MM:SS.ss"""
    m, s = divmod(max(float(sec), 0.0), 60)
    return f"{int(m):02d}:{s:05.2f}"


def pct(a, q):
    a = np.asarray(a, float)
    a = a[np.isfinite(a)]
    return round(float(np.percentile(a, q)), 4) if len(a) else None


def fmean(a):
    a = np.asarray(a, float)
    a = a[np.isfinite(a)]
    return round(float(a.mean()), 4) if len(a) else None


def runs(mask):
    """bool配列の True 連続区間を [(start, end_exclusive), ...] で返す"""
    m = np.concatenate([[False], np.asarray(mask, bool), [False]]).astype(np.int8)
    d = np.diff(m)
    return list(zip(np.where(d == 1)[0].tolist(), np.where(d == -1)[0].tolist()))


def hexcolor(bgr) -> str:
    b, g, r = [int(np.clip(round(float(v)), 0, 255)) for v in bgr]
    return f"#{r:02X}{g:02X}{b:02X}"


def load_font(size: int):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def moving_avg(a, k):
    a = np.nan_to_num(np.asarray(a, float))
    if len(a) < k:
        return a
    ker = np.ones(k) / k
    return np.convolve(a, ker, mode="same")


# ---------------------------------------------------------------- 入力

def fetch_url(url: str, out: Path) -> Path:
    """yt-dlp で動画（1080p以下）と字幕（あれば）を取得する"""
    out.mkdir(parents=True, exist_ok=True)
    existing = sorted(out.glob("source.*"))
    vids = [p for p in existing if p.suffix in (".mp4", ".mkv", ".webm")]
    if vids:
        log(f"[取得] 既存ファイルを使用: {vids[0].name}")
        return vids[0]
    log("[取得] yt-dlp で動画をダウンロード中…")
    subprocess.run(["yt-dlp", "--no-playlist",
                    "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b",
                    "--merge-output-format", "mp4", "--write-info-json",
                    "-o", str(out / "source.%(ext)s"), url], check=True)
    # 字幕は失敗しても続行（タイミング付き文字数から話速を出すのに使うだけ）
    subprocess.run(["yt-dlp", "--no-playlist", "--skip-download", "--write-subs", "--write-auto-subs",
                    "--sub-langs", "ja.*,ja", "--sub-format", "vtt",
                    "-o", str(out / "source.%(ext)s"), url], check=False)
    vids = [p for p in sorted(out.glob("source.*")) if p.suffix in (".mp4", ".mkv", ".webm")]
    if not vids:
        raise SystemExit("ダウンロードに失敗しました")
    return vids[0]


def probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    v = next(s for s in d["streams"] if s.get("codec_type") == "video")
    a = next((s for s in d["streams"] if s.get("codec_type") == "audio"), None)

    def rate(s):
        try:
            n, dn = s.split("/")
            return float(n) / float(dn) if float(dn) else 0.0
        except (ValueError, AttributeError):
            return 0.0

    avg, rr = v.get("avg_frame_rate", "0/1"), v.get("r_frame_rate", "0/1")
    fps_str = avg if rate(avg) > 0 else rr
    return {
        "width": int(v["width"]), "height": int(v["height"]),
        "fps": round(rate(fps_str), 4), "fps_str": fps_str,
        "vfr": abs(rate(avg) - rate(rr)) > 0.05,
        "duration": float(d["format"].get("duration") or v.get("duration") or 0),
        "vcodec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"),
        "bitrate_kbps": int(int(d["format"].get("bit_rate", 0) or 0) / 1000),
        "has_audio": a is not None,
        "acodec": a.get("codec_name") if a else None,
        "sample_rate": int(a.get("sample_rate", 0)) if a else None,
        "channels": a.get("channels") if a else None,
    }


class FrameReader:
    """ffmpeg で全フレームを一定fps・指定幅の BGR 配列として先頭から順に読む。
    pass1 / pass2 で同じ fps フィルタを通すので、フレーム番号は両者で一致する。"""

    def __init__(self, path: Path, width: int, info: dict, max_seconds=None):
        sw, sh = info["width"], info["height"]
        w = min(int(width), sw)
        w -= w % 2
        h = max(2, int(round(sh * w / sw / 2)) * 2)
        self.w, self.h = w, h
        self.cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path)]
        if max_seconds:
            self.cmd += ["-t", str(max_seconds)]
        self.cmd += ["-map", "0:v:0", "-an", "-sn",
                     "-vf", f"fps={info['fps_str']},scale={w}:{h}:flags=area",
                     "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]

    def __iter__(self):
        n = self.w * self.h * 3
        p = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, bufsize=n * 4)
        try:
            while True:
                buf = p.stdout.read(n)
                if len(buf) < n:
                    break
                yield np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
        finally:
            p.stdout.close()
            if p.poll() is None:
                p.kill()
            p.wait()


# ---------------------------------------------------------------- 検出器

class TextDetector:
    """PP-OCRv3 の文字領域検出モデル（rapidocr に同梱）でテロップの位置だけを取る。
    文字の読み取りはしない（日本語の読み取りは目視レビューで行う）。"""

    def __init__(self, threads: int = 0):
        import onnxruntime as ort
        import rapidocr_onnxruntime
        model = Path(rapidocr_onnxruntime.__file__).parent / "models" / "ch_PP-OCRv3_det_infer.onnx"
        so = ort.SessionOptions()
        so.log_severity_level = 3
        if threads:
            so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        self.mean = np.array([0.485, 0.456, 0.406], np.float32)
        self.std = np.array([0.229, 0.224, 0.225], np.float32)

    def __call__(self, bgr):
        h, w = bgr.shape[:2]
        H, W = max(32, (h + 31) // 32 * 32), max(32, (w + 31) // 32 * 32)
        x = cv2.resize(bgr, (W, H)).astype(np.float32) / 255.0
        x = ((x - self.mean) / self.std).transpose(2, 0, 1)[None]
        prob = self.sess.run(None, {self.inp: x})[0][0, 0]
        mask = (prob > 0.3).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
        boxes = []
        for i in range(1, n):
            X, Y, BW, BH, A = (int(v) for v in st[i])
            if A < 12 or BH < 2:
                continue
            sub = prob[Y:Y + BH, X:X + BW][lab[Y:Y + BH, X:X + BW] == i]
            score = float(sub.mean())
            if score < 0.55:
                continue
            d = A * 1.5 / (2 * (BW + BH))  # DB方式で縮んだ領域を戻す（unclip）
            x0, y0 = max(0.0, X - d), max(0.0, Y - d)
            x1, y1 = min(float(W), X + BW + d), min(float(H), Y + BH + d)
            bh = (y1 - y0) / H
            if bh < 0.02:  # 画面高の2%未満はテロップではないとみなす
                continue
            boxes.append((x0 / W, y0 / H, (x1 - x0) / W, bh, score))
        return boxes


def ensure_face_model(path) -> Path | None:
    p = Path(path) if path else DEFAULT_FACE_MODEL
    if p.exists():
        return p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        log(f"[顔検出] YuNetモデルを取得: {YUNET_URL}")
        urllib.request.urlretrieve(YUNET_URL, p)
        if p.stat().st_size < 100_000:
            p.unlink()
            raise OSError("model too small")
        return p
    except Exception as e:  # ネットワーク不可でも解析自体は続ける
        log(f"[顔検出] モデルを取得できないため顔検出をスキップ ({e})")
        return None


class FaceDetector:
    def __init__(self, model: Path, w: int, h: int):
        self.det = cv2.FaceDetectorYN.create(str(model), "", (w, h), 0.7, 0.3, 50)
        self.w, self.h = w, h

    def __call__(self, bgr):
        _, faces = self.det.detect(bgr)
        if faces is None:
            return []
        return [(float(f[0]) / self.w, float(f[1]) / self.h, float(f[2]) / self.w,
                 float(f[3]) / self.h, float(f[-1])) for f in faces]


def estimate_motion(prev, cur):
    """前フレーム→現フレームの画面全体の動き（ズーム倍率・パン・回転）を推定。
    戻り値: (scale, pan_x, pan_y, rot_deg, local, inlier_ratio)。
    pan は「画面中央の像が動いた量」を画面幅/高さに対する割合で表す。"""
    h, w = prev.shape[:2]
    pts = cv2.goodFeaturesToTrack(prev, maxCorners=200, qualityLevel=0.01, minDistance=7, blockSize=7)
    if pts is None or len(pts) < 12:
        return None
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev, cur, pts, None, winSize=(21, 21), maxLevel=3)
    ok = st.reshape(-1) == 1
    if ok.sum() < 12:
        return None
    p0, p1 = pts.reshape(-1, 2)[ok], nxt.reshape(-1, 2)[ok]
    M, inl = cv2.estimateAffinePartial2D(p0, p1, method=cv2.RANSAC, ransacReprojThreshold=1.5,
                                         maxIters=500, confidence=0.98)
    if M is None:
        return None
    s = math.hypot(M[0, 0], M[1, 0])
    rot = math.degrees(math.atan2(M[1, 0], M[0, 0]))
    c = np.array([w / 2, h / 2])
    c2 = M[:, :2] @ c + M[:, 2]
    pred = p0 @ M[:, :2].T + M[:, 2]
    local = float(np.median(np.linalg.norm(p1 - pred, axis=1))) / w
    return (s, (c2[0] - c[0]) / w, (c2[1] - c[1]) / h, rot, local, float(inl.mean()))


# ---------------------------------------------------------------- pass1: 全フレーム計測

METRIC_KEYS = ["luma", "luma_std", "sat", "colorful", "lab_a", "lab_b", "content", "hist", "pixdiff",
               "gradual", "blend_rel", "scale", "pan_x", "pan_y", "rot", "local", "inlier"]


def pass1(path: Path, info: dict, args):
    reader = FrameReader(path, args.analysis_width, info, args.max_seconds)
    W, H = reader.w, reader.h
    sw = min(320, W)
    sh = max(2, int(round(H * sw / W / 2)) * 2)
    tw, th = TINY_W, max(2, int(round(H * TINY_W / W)))
    textdet = TextDetector(args.threads) if args.text_step > 0 else None
    facedet = None
    if args.face_step > 0:
        mp = ensure_face_model(args.face_model)
        if mp:
            facedet = FaceDetector(mp, W, H)

    M = {k: [] for k in METRIC_KEYS}
    text_samples, face_samples = [], []
    ring = deque(maxlen=GRAD_K + 1)
    row_dark = np.zeros(sh)
    col_dark = np.zeros(sw)
    prev_hsv = prev_gray = prev_hist = None
    dur = min(info["duration"], args.max_seconds or 1e12)
    expected = max(1, int(dur * info["fps"]))
    t0 = time.time()
    last_report = 0.0
    n = -1
    for n, frame in enumerate(reader):
        small = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB)
        f32 = small.astype(np.float32)
        rg = f32[..., 2] - f32[..., 1]
        yb = 0.5 * (f32[..., 2] + f32[..., 1]) - f32[..., 0]
        M["luma"].append(float(gray.mean()))
        M["luma_std"].append(float(gray.std()))
        M["sat"].append(float(hsv[..., 1].mean()))
        M["colorful"].append(math.hypot(rg.std(), yb.std()) + 0.3 * math.hypot(rg.mean(), yb.mean()))
        M["lab_a"].append(float(lab[..., 1].mean()) - 128)
        M["lab_b"].append(float(lab[..., 2].mean()) - 128)
        hist = cv2.calcHist([hsv], [0, 1], None, [18, 8], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 1, 0, cv2.NORM_L1)
        row_dark += gray.mean(axis=1) < 14
        col_dark += gray.mean(axis=0) < 14

        if prev_hsv is None:
            content = hd = pd = 0.0
            mot = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
        else:
            content = float(cv2.absdiff(hsv, prev_hsv).mean())
            hd = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA))
            pd = float(cv2.absdiff(gray, prev_gray).mean())
            if pd < 0.25:  # 完全に止まっている
                mot = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
            else:
                mot = estimate_motion(prev_gray, gray) or (NAN,) * 6
        M["content"].append(content)
        M["hist"].append(hd)
        M["pixdiff"].append(pd)
        for k, v in zip(["scale", "pan_x", "pan_y", "rot", "local", "inlier"], mot):
            M[k].append(float(v))

        # なだらかな切り替わり: GRAD_K フレーム前との差と「AとBの混合で中間が説明できるか」
        tiny = cv2.resize(small, (tw, th), interpolation=cv2.INTER_AREA).astype(np.float32)
        ring.append(tiny)
        gs = br = NAN
        if len(ring) == GRAD_K + 1:
            A, B, Mid = ring[0], ring[-1], ring[GRAD_K // 2]
            d = A - B
            gs = float(np.abs(d).mean())
            den = float((d * d).sum())
            if den > 1e-3:
                alpha = float(np.clip(((Mid - B) * d).sum() / den, 0, 1))
                res = float(np.abs(Mid - (alpha * A + (1 - alpha) * B)).mean())
                br = res / (gs + 1e-6)
        M["gradual"].append(gs)
        M["blend_rel"].append(br)

        if textdet and n % args.text_step == 0:
            boxes = textdet(frame)
            if boxes:
                fg = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                withsig = []
                for (x, y, w, h, s) in boxes:
                    x0, y0 = int(x * W), int(y * H)
                    x1, y1 = max(x0 + 2, int((x + w) * W)), max(y0 + 2, int((y + h) * H))
                    crop = cv2.resize(fg[y0:y1, x0:x1], (48, 12), interpolation=cv2.INTER_AREA).astype(np.float32)
                    crop = (crop - crop.mean()) / (crop.std() + 1e-3)
                    withsig.append((x, y, w, h, s, crop))
                text_samples.append((n, withsig))
            else:
                text_samples.append((n, []))
        if facedet and n % args.face_step == 0:
            face_samples.append((n, facedet(frame)))

        prev_hsv, prev_gray, prev_hist = hsv, gray, hist
        now = time.time()
        if now - last_report > 15:
            last_report = now
            el = now - t0
            sp = (n + 1) / max(el, 1e-6)
            log(f"[pass1] {n + 1}/{expected} フレーム ({100 * (n + 1) / expected:.0f}%) "
                f"{sp:.1f}fps 残り約{max(0, expected - n - 1) / max(sp, 1e-6) / 60:.1f}分")
    N = n + 1
    if N <= 0:
        raise SystemExit("フレームを読めませんでした")
    log(f"[pass1] 完了: {N}フレーム / {time.time() - t0:.0f}秒")
    arr = {k: np.array(v, float) for k, v in M.items()}
    letterbox = {
        "top_rows": int(np.argmax(row_dark < 0.97 * N)) if (row_dark >= 0.97 * N).any() else 0,
        "rows": sh, "cols": sw,
        "bottom_rows": int(np.argmax(row_dark[::-1] < 0.97 * N)) if (row_dark >= 0.97 * N).any() else 0,
        "left_cols": int(np.argmax(col_dark < 0.97 * N)) if (col_dark >= 0.97 * N).any() else 0,
        "right_cols": int(np.argmax(col_dark[::-1] < 0.97 * N)) if (col_dark >= 0.97 * N).any() else 0,
    }
    return arr, N, text_samples, face_samples, letterbox, (W, H)


# ---------------------------------------------------------------- 音声

def analyze_audio(path: Path, info: dict, N: int, fps: float, max_seconds=None):
    if not info["has_audio"]:
        return None
    sr = 16000
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path)]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-map", "0:a:0", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    pcm = np.frombuffer(raw, "<i2")
    x = pcm.astype(np.float32) / 32768.0

    # フレームごとの音量 (dBFS)
    edges = np.minimum((np.arange(N + 1) * sr / fps).astype(np.int64), len(x))
    cs = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    cnt = np.maximum(np.diff(edges), 1)
    rms_db = 20 * np.log10(np.sqrt(np.maximum(cs[edges[1:]] - cs[edges[:-1]], 0) / cnt) + 1e-6)

    # 発話区間 (webrtcvad, 30ms単位)
    speech_frames = np.zeros(N, bool)
    segs = []
    try:
        import webrtcvad
        vad = webrtcvad.Vad(2)
        fl = int(sr * 0.03)
        nv = len(pcm) // fl
        b = pcm[:nv * fl].astype("<i2").tobytes()
        sp = np.array([vad.is_speech(b[i * fl * 2:(i + 1) * fl * 2], sr) for i in range(nv)], bool)
        # 0.12秒未満の途切れは埋め、0.09秒未満の発話は捨てる
        for s0, e0 in runs(~sp):
            if 0 < s0 and e0 < nv and e0 - s0 < 4:
                sp[s0:e0] = True
        for s0, e0 in runs(sp):
            if e0 - s0 < 3:
                sp[s0:e0] = False
        segs = [(s0 * 0.03, e0 * 0.03) for s0, e0 in runs(sp)]
        for a, e in segs:
            speech_frames[int(a * fps):min(N, int(math.ceil(e * fps)))] = True
    except ImportError:
        log("[音声] webrtcvad が無いので発話区間の解析をスキップ")

    gaps = [segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1)]
    short_gaps = [g for g in gaps if g < 3.0]
    gap_levels = []
    for i in range(len(segs) - 1):
        a, e = segs[i][1] + 0.08, segs[i + 1][0] - 0.08
        if e - a >= 0.2:
            fa, fe = int(a * fps), int(e * fps)
            if fe > fa:
                gap_levels.append(float(np.median(rms_db[fa:fe])))
    speech_level = float(np.median(rms_db[speech_frames])) if speech_frames.any() else None

    # 音の立ち上がり（効果音・ビート候補）: スペクトルフラックスのピーク
    n_fft, hop = 1024, 256
    onsets = []
    if len(x) > n_fft * 4:
        win = np.hanning(n_fft).astype(np.float32)
        nfr = 1 + (len(x) - n_fft) // hop
        flux = np.zeros(nfr, np.float32)
        prev_mag = None
        for c0 in range(0, nfr, 4096):
            c1 = min(nfr, c0 + 4096)
            idx = (np.arange(c0, c1) * hop)[:, None] + np.arange(n_fft)[None, :]
            mag = np.log1p(10 * np.abs(np.fft.rfft(x[idx] * win, axis=1)))
            if prev_mag is not None:
                mag_all = np.vstack([prev_mag[None], mag])
            else:
                mag_all = np.vstack([mag[:1], mag])
            flux[c0:c1] = np.maximum(np.diff(mag_all, axis=0), 0).sum(axis=1)
            prev_mag = mag[-1]
        k = 31
        pad = np.pad(flux, (k // 2, k // 2), mode="edge")
        med = np.median(np.lib.stride_tricks.sliding_window_view(pad, k), axis=1)
        thr = med + 1.5 * float(np.std(flux))
        is_peak = (flux > thr) & (flux >= np.roll(flux, 1)) & (flux >= np.roll(flux, -1))
        last = -1e9
        for i in np.where(is_peak)[0]:
            t = i * hop / sr
            if t - last >= 0.08:
                onsets.append(round(float(t), 3))
                last = t

    lufs = loudness(path, max_seconds)
    return {
        "rms_db": rms_db, "speech_frames": speech_frames, "segments": segs, "onsets": onsets,
        "summary": {
            **lufs,
            "speech_ratio": round(float(speech_frames.mean()), 4),
            "speech_segments": len(segs),
            "pause_median_sec": pct(short_gaps, 50), "pause_p90_sec": pct(short_gaps, 90),
            "pauses_over_0_5s_ratio": round(float(np.mean(np.array(short_gaps) > 0.5)), 4) if short_gaps else None,
            "long_silences_over_3s": int(sum(g >= 3.0 for g in gaps)),
            "speech_level_db": round(speech_level, 1) if speech_level is not None else None,
            "gap_level_db": round(float(np.median(gap_levels)), 1) if gap_levels else None,
            "noise_floor_db_p5": round(float(np.percentile(rms_db, 5)), 1),
            "onsets_per_min": round(len(onsets) / max(len(x) / sr / 60, 1e-6), 1),
        },
    }


def loudness(path: Path, max_seconds=None) -> dict:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path)]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stderr
    summ = err[err.rfind("Summary:"):] if "Summary:" in err else ""

    def g(pat):
        m = re.search(pat, summ)
        try:
            return float(m.group(1)) if m else None
        except ValueError:
            return None
    return {"integrated_lufs": g(r"I:\s+(-?[\d.]+) LUFS"), "lra_lu": g(r"LRA:\s+(-?[\d.]+) LU"),
            "true_peak_dbfs": g(r"Peak:\s+(-?[\d.]+) dBFS")}


def parse_vtt(path: Path):
    """YouTube字幕(VTT)から重複を除いた行テキストを取り出す（話速の目安用）"""
    lines, last = [], None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "-->" in raw or not raw.strip() or raw.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        t = re.sub(r"<[^>]+>", "", raw).strip()
        if t and t != last:
            lines.append(t)
            last = t
    return lines


# ---------------------------------------------------------------- 切り替わり検出

def detect_boundaries(m: dict, N: int, fps: float, args):
    """ショットの境界を検出する。
    戻り値: boundaries（フレーム→種類）、特殊区間（フラッシュ・黒画面）、なだらかな遷移の一覧"""
    cs, luma, lstd = m["content"], m["luma"], m["luma_std"]

    # 1) ハードカット: 前後数フレームの平均に比べて突出した変化（PySceneDetect AdaptiveDetector 相当）
    cut = np.zeros(N, bool)
    w = 2
    for t in range(1, N):
        nb = np.r_[cs[max(1, t - w):t], cs[t + 1:t + 1 + w]]
        avg = float(nb.mean()) if len(nb) else 0.0
        ratio = cs[t] / max(avg, 1.0)
        if (cs[t] >= args.cut_min and ratio >= args.cut_ratio) or cs[t] >= args.cut_hard:
            cut[t] = True
    # 連続フレームで立った場合は大きい方だけ残す
    for s0, e0 in runs(cut):
        if e0 - s0 > 1:
            keep = s0 + int(np.argmax(cs[s0:e0]))
            cut[s0:e0] = False
            cut[keep] = True

    # 2) 黒画面・白フラッシュ
    dark = (luma < 20) & (lstd < 12)
    win = 15
    pad = np.pad(luma, (win // 2, win // 2), mode="edge")
    base = np.median(np.lib.stride_tricks.sliding_window_view(pad, win), axis=1)
    bright = (luma - base > 50) | ((luma > 235) & (lstd < 12))
    specials = []
    for s0, e0 in runs(dark):
        if e0 - s0 >= 2:
            fo = ramp_len(luma, s0, -1)
            fi = ramp_len(luma, e0 - 1, +1)
            specials.append({"kind": "black", "start": s0, "end": e0,
                             "fade_out_frames": fo, "fade_in_frames": fi})
    for s0, e0 in runs(bright):
        if e0 - s0 <= 6 and not any(sp["start"] <= s0 < sp["end"] for sp in specials):
            specials.append({"kind": "flash", "start": s0, "end": e0})
    specials.sort(key=lambda d: d["start"])

    def in_special(t, margin=1):
        return any(sp["start"] - margin <= t <= sp["end"] + margin for sp in specials)

    # 3) なだらかな遷移（ディゾルブ・ワイプ等）。ハードカットや黒/白区間で説明できるものは除外
    gs = np.nan_to_num(m["gradual"])
    cut_cum = np.concatenate([[0], np.cumsum(cut)])
    grads = []
    for s0, e0 in runs(gs > args.grad_min):
        c = s0 + int(np.argmax(gs[s0:e0]))
        lo, hi = max(0, s0 - GRAD_K), min(N, e0)
        if cut_cum[hi] - cut_cum[lo] > 0:
            continue
        if any(in_special(t, GRAD_K) for t in (lo, c, hi - 1)):
            continue
        seg = slice(max(0, c - GRAD_K), c + 1)
        inl = np.nan_to_num(m["inlier"][seg], nan=0.0)
        sc = np.abs(np.log(np.nan_to_num(m["scale"][seg], nan=1.0)))
        pan = np.hypot(np.nan_to_num(m["pan_x"][seg]), np.nan_to_num(m["pan_y"][seg]))
        if np.median(inl) > 0.7 and (np.median(pan) > 0.004 or np.median(sc) > 0.004):
            continue  # 画面全体の動き（カメラワーク）で説明できる
        br = float(np.nanmedian(m["blend_rel"][s0:e0])) if np.isfinite(m["blend_rel"][s0:e0]).any() else NAN
        grads.append({"center": c, "start": max(0, s0 - GRAD_K), "end": e0,
                      "frames": int(e0 - s0 + GRAD_K),
                      "type": "dissolve" if (np.isfinite(br) and br < 0.45) else "gradual_other",
                      "blend_rel": round(br, 3) if np.isfinite(br) else None})

    # 境界の確定（frame -> 種類）
    bnd = {}
    for t in np.where(cut)[0]:
        t = int(t)
        if not in_special(t):
            bnd[t] = "cut"
    for sp in specials:
        bnd[sp["start"]] = sp["kind"] + "_start"
        if sp["end"] < N:
            bnd[sp["end"]] = sp["kind"] + "_end"
    for g in grads:
        if not any(abs(g["center"] - b) <= 2 for b in bnd):
            bnd[g["center"]] = g["type"]
    bnd.pop(0, None)
    return dict(sorted(bnd.items())), specials, grads


def ramp_len(luma, idx, direction):
    """黒区間の端から外側へ、輝度が単調に上がっていく（= フェード）フレーム数。
    direction=-1: 黒区間の手前を遡る（フェードアウト） / +1: 後ろを進む（フェードイン）"""
    n, total, cur = 0, 0.0, idx
    while n < 120:
        nxt = cur + direction
        if not (0 <= nxt < len(luma)):
            break
        step = luma[nxt] - luma[cur]
        if step < 0.5:
            break
        total += step
        n += 1
        cur = nxt
    return n if (n >= 3 and total >= 25) else 0


# ---------------------------------------------------------------- ショット

SHOT_SIZE = [(0.42, "close_up"), (0.24, "bust"), (0.12, "medium"), (0.05, "long"), (0.0, "wide")]


def classify_motion(m, s, e, fps):
    """ショット内のカメラワーク（ズーム・パン・揺れ等）を分類"""
    if e - s < 3:
        return {"motion": "too_short", "zoom_pct": 0.0, "pan_x_pct": 0.0, "pan_y_pct": 0.0,
                "jitter": 0.0, "tracked": 0.0}
    sl = slice(s + 1, e)
    scale = m["scale"][sl]
    ok = np.isfinite(scale)
    tracked = float(ok.mean())
    ls = np.log(np.where(ok, scale, 1.0))
    zoom = float(np.exp(ls.sum()) - 1)
    px, py = np.nan_to_num(m["pan_x"][sl]), np.nan_to_num(m["pan_y"][sl])
    cam_x, cam_y = -float(px.sum()), -float(py.sum())  # 像が左へ動く = カメラは右へ
    hp = np.hypot(px - moving_avg(px, 7), py - moving_avg(py, 7))
    jitter = float(np.sqrt(np.mean(hp[ok] ** 2))) if ok.any() else 0.0
    moving = ls[ok & (np.abs(ls) > 1e-4)]
    consist = float(np.mean(np.sign(moving) == np.sign(zoom))) if len(moving) else 0.0
    pixd = float(np.mean(m["pixdiff"][sl]))
    local = float(np.nanmean(m["local"][sl])) if ok.any() else NAN
    pan_mag = math.hypot(cam_x, cam_y)
    if tracked < 0.5:
        mo = "dynamic"
    elif pixd < 0.6 and (not np.isfinite(local) or local < 0.002):
        mo = "still"
    elif jitter > 0.004:
        mo = "shake"
    elif abs(zoom) >= 0.03 and (consist >= 0.65 or abs(zoom) >= 0.08) and abs(zoom) >= 0.5 * pan_mag:
        mo = "zoom_in" if zoom > 0 else "zoom_out"
    elif pan_mag >= 0.04:
        if abs(cam_x) >= abs(cam_y):
            mo = "pan_right" if cam_x > 0 else "pan_left"
        else:
            mo = "tilt_down" if cam_y > 0 else "tilt_up"
    else:
        mo = "fixed"
    return {"motion": mo, "zoom_pct": round(100 * zoom, 2), "pan_x_pct": round(100 * cam_x, 2),
            "pan_y_pct": round(100 * cam_y, 2), "jitter": round(jitter, 5), "tracked": round(tracked, 3)}


def build_shots(m, N, fps, bnd, specials, text_samples, face_samples, audio):
    edges = [0] + list(bnd.keys()) + [N]
    special_at = {sp["start"]: sp for sp in specials}
    tframes = np.array([f for f, _ in text_samples]) if text_samples else np.array([], int)
    ffr = np.array([f for f, _ in face_samples]) if face_samples else np.array([], int)
    shots = []
    for i in range(len(edges) - 1):
        s, e = edges[i], edges[i + 1]
        if e <= s:
            continue
        kind = "normal"
        if s in special_at and e <= special_at[s]["end"] + 1:
            kind = special_at[s]["kind"]
        tin = "start" if s == 0 else bnd.get(s, "cut")
        if tin.endswith("_end"):
            sp = next((x for x in specials if x["end"] == s), None)
            if sp and sp["kind"] == "black":
                tin = "fade_in" if sp.get("fade_in_frames") else "from_black"
            else:
                tin = "after_flash"
        sh = {"idx": len(shots) + 1, "kind": kind, "start": s, "end": e, "frames": e - s,
              "start_sec": round(s / fps, 3), "dur_sec": round((e - s) / fps, 3),
              "transition_in": tin, "mid": (s + e - 1) // 2}
        sh.update(classify_motion(m, s, e, fps))
        sl = slice(s, e)
        for k in ["luma", "luma_std", "sat", "colorful", "lab_b"]:
            sh[k] = round(float(np.mean(m[k][sl])), 2)
        # テロップ
        tsel = [text_samples[j][1] for j in np.where((tframes >= s) & (tframes < e))[0]] if len(tframes) else []
        if tsel:
            sh["text_boxes"] = round(float(np.mean([len(b) for b in tsel])), 2)
            sh["text_area"] = round(float(np.mean([sum(bb[2] * bb[3] for bb in b) for b in tsel])), 4)
        else:
            sh["text_boxes"] = sh["text_area"] = None
        # 顔
        fsel = [face_samples[j][1] for j in np.where((ffr >= s) & (ffr < e))[0]] if len(ffr) else []
        if fsel:
            has = [len(f) > 0 for f in fsel]
            sh["face_ratio"] = round(float(np.mean(has)), 3)
            hs = [max(ff[3] for ff in f) for f in fsel if f]
            xs = [max(f, key=lambda ff: ff[3]) for f in fsel if f]
            if hs:
                fh = float(np.median(hs))
                sh["face_h"] = round(fh, 3)
                sh["shot_size"] = next(lbl for th, lbl in SHOT_SIZE if fh >= th)
                cx = float(np.median([ff[0] + ff[2] / 2 for ff in xs]))
                sh["face_x"] = "left" if cx < 0.4 else ("right" if cx > 0.6 else "center")
            else:
                sh["face_h"], sh["shot_size"], sh["face_x"] = None, "no_face", None
        else:
            sh["face_ratio"] = sh["face_h"] = sh["face_x"] = None
            sh["shot_size"] = None
        if audio is not None:
            sh["speech_ratio"] = round(float(audio["speech_frames"][sl].mean()), 3)
            sh["rms_db"] = round(float(np.mean(audio["rms_db"][sl])), 1)
        shots.append(sh)
    return shots


def detect_motion_events(m, shots, fps):
    """ショット内の素早いズーム（強調ズーム）と画面揺れのイベント"""
    events = {"quick_zoom": [], "shake": []}
    for sh in shots:
        s, e = sh["start"] + 1, sh["end"]
        if e - s < 2 or sh["kind"] != "normal":
            continue
        sc = m["scale"][s:e]
        inl = np.nan_to_num(m["inlier"][s:e])
        ls = np.where(np.isfinite(sc), np.log(np.where(np.isfinite(sc), sc, 1.0)), 0.0)
        fast = (np.abs(ls) >= 0.015) & (inl >= 0.5)
        for a, b in runs(fast):
            tot = float(ls[a:b].sum())
            if abs(tot) >= 0.05:
                events["quick_zoom"].append({"frame": s + a, "sec": round((s + a) / fps, 3),
                                             "frames": b - a, "zoom_pct": round(100 * (math.exp(tot) - 1), 1),
                                             "shot": sh["idx"]})
        px, py = np.nan_to_num(m["pan_x"][s:e]), np.nan_to_num(m["pan_y"][s:e])
        hp = np.hypot(px - moving_avg(px, 7), py - moving_avg(py, 7))
        for a, b in runs(hp > 0.006):
            if b - a >= 4:
                events["shake"].append({"frame": s + a, "sec": round((s + a) / fps, 3), "frames": b - a,
                                        "amp_pct": round(100 * float(hp[a:b].max()), 2), "shot": sh["idx"]})
    return events


# ---------------------------------------------------------------- テロップの追跡

def iou(a, b):
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax1, bx1) - max(a[0], b[0]))
    ih = max(0.0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    u = a[2] * a[3] + b[2] * b[3] - inter
    return inter / u if u > 0 else 0.0


def track_telops(text_samples, step, fps, duration):
    """フレームごとの文字領域を、位置と見た目の一致でつないで「テロップ1件」にまとめる"""
    active, done = [], []
    for f, boxes in text_samples:
        used = set()
        for inst in active:
            best, bj = 0.0, -1
            for j, bx in enumerate(boxes):
                if j in used:
                    continue
                v = iou(inst["box"], bx)
                if v > best:
                    best, bj = v, j
            if bj >= 0 and best >= 0.4:
                corr = float((inst["sig"] * boxes[bj][5]).mean())
                if corr >= 0.55:
                    used.add(bj)
                    inst["last"] = f
                    inst["box"] = boxes[bj][:4]
                    inst["hist"].append(boxes[bj][:4])
                    inst["sig"] = boxes[bj][5]
        for j, bx in enumerate(boxes):
            if j not in used:
                active.append({"first": f, "last": f, "box": bx[:4], "hist": [bx[:4]], "sig": bx[5]})
        still = []
        for inst in active:
            (still if f - inst["last"] <= 2 * step else done).append(inst)
        active = still
    done += active
    telops = []
    for inst in done:
        hb = np.array(inst["hist"])
        med = np.median(hb, axis=0)
        dur = (inst["last"] - inst["first"] + step) / fps
        first = hb[0]
        anim = "none"
        if len(hb) >= 3:
            if first[3] < 0.8 * med[3] or first[2] < 0.6 * med[2]:
                anim = "grow_in"
            elif abs((first[0] + first[2] / 2) - (med[0] + med[2] / 2)) > 0.05:
                anim = "slide_in_x"
            elif abs((first[1] + first[3] / 2) - (med[1] + med[3] / 2)) > 0.05:
                anim = "slide_in_y"
        cx, cy = float(med[0] + med[2] / 2), float(med[1] + med[3] / 2)
        telops.append({
            "start": inst["first"], "end": inst["last"] + step, "start_sec": round(inst["first"] / fps, 3),
            "dur_sec": round(dur, 3), "x": round(float(med[0]), 4), "y": round(float(med[1]), 4),
            "w": round(float(med[2]), 4), "h": round(float(med[3]), 4),
            "cx": round(cx, 4), "cy": round(cy, 4),
            "v_region": "top" if cy < 1 / 3 else ("bottom" if cy > 2 / 3 else "middle"),
            "h_align": "center" if abs(cx - 0.5) < 0.08 else ("left" if cx < 0.5 else "right"),
            "samples": len(hb), "anim_in": anim,
            "persistent": bool(dur >= max(10.0, 0.3 * duration)),
            "mid": (inst["first"] + inst["last"]) // 2 // step * step,
        })
    telops.sort(key=lambda t: t["start"])
    return telops


# ---------------------------------------------------------------- pass2: 代表フレーム・コマ送り・色

def kmeans_colors(pixels_bgr, k):
    px = np.float32(pixels_bgr.reshape(-1, 3))
    if len(px) < k:
        return []
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, labels, centers = cv2.kmeans(px, k, None, crit, 2, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.reshape(-1), minlength=k)
    order = np.argsort(-counts)
    return [(centers[i], float(counts[i] / counts.sum()), labels.reshape(-1) == i) for i in order]


def telop_colors(frame, t):
    """テロップ領域を3色に分け、背景・縁取り・文字色を推定する（目安）"""
    H, W = frame.shape[:2]
    pad_x, pad_y = t["w"] * 0.04, t["h"] * 0.15
    x0, y0 = int(max(0, t["x"] - pad_x) * W), int(max(0, t["y"] - pad_y) * H)
    x1, y1 = int(min(1, t["x"] + t["w"] + pad_x) * W), int(min(1, t["y"] + t["h"] + pad_y) * H)
    crop = frame[y0:y1, x0:x1]
    if crop.size < 300:
        return None
    if crop.shape[0] > 80:
        f = 80 / crop.shape[0]
        crop = cv2.resize(crop, (max(3, int(crop.shape[1] * f)), 80), interpolation=cv2.INTER_AREA)
    cl = kmeans_colors(crop, 3)
    if len(cl) < 3:
        return None
    hh, ww = crop.shape[:2]
    border = np.zeros((hh, ww), bool)
    bw = max(1, int(min(hh, ww) * 0.12))
    border[:bw, :] = border[-bw:, :] = border[:, :bw] = border[:, -bw:] = True
    border = border.reshape(-1)
    bscore = [float(lbl[border].mean()) / max(float(lbl.mean()), 1e-6) for _, _, lbl in cl]
    bg = int(np.argmax(bscore))
    rest = [i for i in range(3) if i != bg]
    # 文字色は縁取りに囲まれている → 各色のすぐ外側1pxに背景が含まれる割合が低い方を文字色とみなす
    k3 = np.ones((3, 3), np.uint8)
    touch_bg = []
    for i in rest:
        mk = cl[i][2].reshape(hh, ww).astype(np.uint8)
        ring = (cv2.dilate(mk, k3) - mk).reshape(-1).astype(bool)
        touch_bg.append(float(cl[bg][2][ring].mean()) if ring.any() else 1.0)
    fill, outline = (rest[0], rest[1]) if touch_bg[0] <= touch_bg[1] else (rest[1], rest[0])
    return {"fill": hexcolor(cl[fill][0]), "outline": hexcolor(cl[outline][0]), "bg": hexcolor(cl[bg][0]),
            "fill_share": round(cl[fill][1], 3), "outline_share": round(cl[outline][1], 3)}


def jump_analysis(a, b):
    """カット前後が同じ構図の撮り直し（ジャンプカット）か、拡大して繋いだか（パンチイン）"""
    ga = cv2.cvtColor(cv2.resize(a, (480, int(480 * a.shape[0] / a.shape[1]))), cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(cv2.resize(b, (480, int(480 * b.shape[0] / b.shape[1]))), cv2.COLOR_BGR2GRAY)
    ha = cv2.calcHist([cv2.cvtColor(a, cv2.COLOR_BGR2HSV)], [0, 1], None, [18, 8], [0, 180, 0, 256])
    hb = cv2.calcHist([cv2.cvtColor(b, cv2.COLOR_BGR2HSV)], [0, 1], None, [18, 8], [0, 180, 0, 256])
    cv2.normalize(ha, ha, 1, 0, cv2.NORM_L1)
    cv2.normalize(hb, hb, 1, 0, cv2.NORM_L1)
    hsim = 1 - float(cv2.compareHist(ha, hb, cv2.HISTCMP_BHATTACHARYYA))
    orb = cv2.ORB_create(1500)
    ka, da = orb.detectAndCompute(ga, None)
    kb, db = orb.detectAndCompute(gb, None)
    res = {"hist_sim": round(hsim, 3), "same_setup": False, "scale": None, "inliers": 0}
    if da is None or db is None or len(ka) < 20 or len(kb) < 20:
        return res
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(matches) < 20:
        return res
    pa = np.float32([ka[mm.queryIdx].pt for mm in matches])
    pb = np.float32([kb[mm.trainIdx].pt for mm in matches])
    Mx, inl = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    if Mx is None:
        return res
    ninl = int(inl.sum())
    s = math.hypot(Mx[0, 0], Mx[1, 0])
    res.update({"inliers": ninl, "scale": round(float(s), 3)})
    if ninl >= 25 and ninl / len(matches) >= 0.25 and 0.5 < s < 2.0:
        res["same_setup"] = True
    return res


def pass2(path, info, args, N, fps, shots, bnd, specials, grads, telops, out: Path):
    reader = FrameReader(path, args.keyframe_width, info, args.max_seconds)
    KW, KH = reader.w, reader.h
    kdir, sdir, tdir = out / "keyframes", out / "sheets", out / "transitions"
    for d in (kdir, sdir, tdir):
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.jpg"):
            old.unlink()
    key_at = {sh["mid"]: sh for sh in shots}
    # コマ送りの対象: 全境界の前後3フレーム（なだらかな遷移は2フレーム刻み）
    strips = []
    grad_types = {"dissolve", "gradual_other"}
    for b, kind in bnd.items():
        step = 2 if kind in grad_types else 1
        frames = [min(N - 1, max(0, b + o * step)) for o in range(-3, 4)]
        strips.append({"b": b, "kind": kind, "frames": frames, "thumbs": [None] * 7, "step": step})
    strip_map = {}
    for j, st in enumerate(strips):
        for pos, f in enumerate(st["frames"]):
            strip_map.setdefault(f, []).append((j, pos))
    telop_at = {}
    for i, t in enumerate(telops):
        telop_at.setdefault(min(N - 1, t["mid"]), []).append(i)
    cut_frames = {b for b, k in bnd.items() if k == "cut"}
    jumps = {}
    tiny_keys, sheet_items = [], []
    last_needed = max([0] + list(key_at) + list(strip_map) + list(telop_at) + list(cut_frames))

    font_s = load_font(14)
    thumb_w = 192
    thumb_h = max(2, int(round(KH * thumb_w / KW)))
    sheet_cols, sheet_rows = 5, 6
    cw = 320
    ch = max(2, int(round(KH * cw / KW)))
    state = {"sheet_no": 0, "trans_no": 0, "trans_rows": [], "next_strip": 0}

    def flush_sheet(final=False):
        per = sheet_cols * sheet_rows
        while len(sheet_items) >= per or (final and sheet_items):
            items = sheet_items[:per]
            del sheet_items[:per]
            img = Image.new("RGB", (sheet_cols * cw, sheet_rows * (ch + 20)), (24, 24, 24))
            dr = ImageDraw.Draw(img)
            for k, (thumb, label) in enumerate(items):
                x, y = (k % sheet_cols) * cw, (k // sheet_cols) * (ch + 20)
                img.paste(thumb, (x, y))
                dr.text((x + 4, y + ch + 2), label, fill=(235, 235, 235), font=font_s)
            state["sheet_no"] += 1
            img.save(sdir / f"sheet_{state['sheet_no']:03d}.jpg", quality=85)

    def flush_trans(final=False):
        per = 12
        while len(state["trans_rows"]) >= per or (final and state["trans_rows"]):
            rows = state["trans_rows"][:per]
            del state["trans_rows"][:per]
            lw = 170
            img = Image.new("RGB", (lw + 7 * thumb_w, len(rows) * (thumb_h + 6)), (24, 24, 24))
            dr = ImageDraw.Draw(img)
            for r, (label, thumbs) in enumerate(rows):
                y = r * (thumb_h + 6)
                for line_no, line in enumerate(label.split("\n")):
                    dr.text((4, y + 4 + 16 * line_no), line, fill=(235, 235, 235), font=font_s)
                for k, th in enumerate(thumbs):
                    if th is not None:
                        img.paste(th, (lw + k * thumb_w, y))
                dr.rectangle([lw + 3 * thumb_w, y, lw + 4 * thumb_w - 1, y + thumb_h - 1], outline=(255, 64, 64))
            state["trans_no"] += 1
            img.save(tdir / f"transitions_{state['trans_no']:03d}.jpg", quality=85)

    prev = None
    t0 = time.time()
    for n, frame in enumerate(reader):
        if n > last_needed:
            break
        if n in key_at:
            sh = key_at[n]
            name = f"shot_{sh['idx']:04d}.jpg"
            cv2.imwrite(str(kdir / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
            sh["keyframe"] = f"keyframes/{name}"
            th = Image.fromarray(cv2.cvtColor(cv2.resize(frame, (cw, ch), interpolation=cv2.INTER_AREA),
                                              cv2.COLOR_BGR2RGB))
            label = f"#{sh['idx']} {tc(sh['start_sec'])} {sh['dur_sec']:.2f}s {sh['transition_in']} {sh['motion']}"
            sheet_items.append((th, label))
            flush_sheet()
            tiny = cv2.resize(frame, (96, max(2, int(96 * KH / KW))), interpolation=cv2.INTER_AREA)
            tiny_keys.append((tiny, sh["frames"]))
            dom = kmeans_colors(tiny, 3)
            sh["dom_color"] = hexcolor(dom[0][0]) if dom else None
        if n in strip_map:
            th = Image.fromarray(cv2.cvtColor(cv2.resize(frame, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA),
                                              cv2.COLOR_BGR2RGB))
            for j, pos in strip_map[n]:
                strips[j]["thumbs"][pos] = th
        if n in telop_at:
            for i in telop_at[n]:
                telops[i]["colors"] = telop_colors(frame, telops[i])
        if n in cut_frames and prev is not None:
            jumps[n] = jump_analysis(prev, frame)
        # 完成したコマ送りを順に書き出す
        while state["next_strip"] < len(strips) and strips[state["next_strip"]]["frames"][-1] <= n:
            st = strips[state["next_strip"]]
            off = "/".join(str(o * st["step"]) for o in (-3, 3))
            label = f"{tc(st['b'] / fps)}\nframe {st['b']}\n{st['kind']}\n({off}f)"
            state["trans_rows"].append((label, st["thumbs"]))
            st["thumbs"] = None
            state["next_strip"] += 1
            flush_trans()
        prev = frame
    while state["next_strip"] < len(strips):
        st = strips[state["next_strip"]]
        state["trans_rows"].append((f"{tc(st['b'] / fps)}\n{st['kind']}", st["thumbs"]))
        state["next_strip"] += 1
    flush_sheet(final=True)
    flush_trans(final=True)
    log(f"[pass2] 代表フレーム {len(key_at)}枚・コマ送り {len(strips)}箇所 ({time.time() - t0:.0f}秒)")
    return jumps, tiny_keys


def static_overlay(tiny_keys, out: Path):
    """各ショット代表フレームを重ねて、場面が変わっても動かない領域（枠・ロゴ等）を探す"""
    if len(tiny_keys) < 6:
        return {"regions": [], "note": "ショット数が少なく判定不可"}
    stack = np.stack([cv2.cvtColor(t, cv2.COLOR_BGR2GRAY).astype(np.float32) for t, _ in tiny_keys])
    std = stack.std(axis=0)
    if float(np.median(std)) < 12:
        return {"regions": [], "note": "全体の変化が小さい（同じ構図が続く動画）ため判定不可",
                "median_std": round(float(np.median(std)), 1)}
    mask = (std < 6).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    h, w = mask.shape
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask)
    regions = []
    for i in range(1, n):
        x, y, bw, bh, a = (int(v) for v in st[i])
        if a / (h * w) >= 0.004:
            mean_c = np.stack([t for t, _ in tiny_keys]).astype(np.float32)[:, lab == i].mean(axis=(0, 1))
            regions.append({"x": round(x / w, 3), "y": round(y / h, 3), "w": round(bw / w, 3),
                            "h": round(bh / h, 3), "area": round(a / (h * w), 4), "color": hexcolor(mean_c)})
    mean_img = stack.mean(axis=0)
    vis = cv2.cvtColor(mean_img.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    vis[mask > 0] = (0.4 * vis[mask > 0] + 0.6 * np.array([0, 0, 255])).astype(np.uint8)
    vis = cv2.resize(vis, (w * 6, h * 6), interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(str(out / "static_overlay.png"), vis)
    regions.sort(key=lambda r: -r["area"])
    return {"regions": regions[:10], "static_area_ratio": round(float(mask.mean()), 4)}


def palette(tiny_keys, k=8):
    if not tiny_keys:
        return []
    px = np.concatenate([t.reshape(-1, 3) for t, _ in tiny_keys])
    if len(px) > 200_000:
        px = px[np.random.default_rng(0).choice(len(px), 200_000, replace=False)]
    return [{"color": hexcolor(c), "share": round(s, 3)} for c, s, _ in kmeans_colors(px, k)]


# ---------------------------------------------------------------- 集計

def hist_bins(vals, edges):
    out = {}
    for a, b in zip(edges[:-1], edges[1:]):
        key = f"{a}-{b}s" if b != float("inf") else f"{a}s+"
        out[key] = int(sum(a <= v < b for v in vals))
    return out


def share(counter: Counter, total=None):
    total = total or sum(counter.values())
    return {k: round(v / total, 3) for k, v in counter.most_common()} if total else {}


def summarize(info, N, fps, m, shots, bnd, specials, grads, telops, events, jumps, audio, faces,
              letterbox, overlay, pal, subs_lines, args, src_label):
    dur = N / fps
    normal = [s for s in shots if s["kind"] == "normal"]
    sl = np.array([s["dur_sec"] for s in normal]) if normal else np.array([0.0])
    n_bound = len(bnd)
    # 時間帯ごとのペース
    win = 30.0
    pacing = []
    for w0 in np.arange(0, dur, win):
        w1 = min(dur, w0 + win)
        cuts = [s for s in normal if w0 <= s["start_sec"] < w1]
        pacing.append({"from_sec": round(float(w0), 1), "shots": len(cuts),
                       "avg_shot_sec": round(float(np.mean([c["dur_sec"] for c in cuts])), 2) if cuts else None})
    hook = [s for s in normal if s["start_sec"] < 30]
    kinds = Counter()
    for k in bnd.values():
        if k.endswith("_end"):
            continue
        kinds[k.replace("_start", "")] += 1
    # ジャンプカット
    jl = list(jumps.values())
    same = [j for j in jl if j["same_setup"]]
    punch_in = [j for j in same if j["scale"] and j["scale"] >= 1.06]
    punch_out = [j for j in same if j["scale"] and j["scale"] <= 0.94]
    # カメラワーク（時間比）
    mshare = Counter()
    for s in normal:
        mshare[s["motion"]] += s["dur_sec"]
    zoom_rates = [s["zoom_pct"] / s["dur_sec"] for s in normal if s["motion"] in ("zoom_in", "zoom_out") and s["dur_sec"] > 0]
    # 顔・画角
    sizes = Counter()
    fx = Counter()
    for s in normal:
        if s.get("shot_size"):
            sizes[s["shot_size"]] += s["dur_sec"]
        if s.get("face_x"):
            fx[s["face_x"]] += s["dur_sec"]
    face_ratio = float(np.mean([len(f) > 0 for _, f in faces])) if faces else None
    # テロップ
    tel = [t for t in telops if not t["persistent"]]
    pers = [t for t in telops if t["persistent"]]
    step = max(1, args.text_step)
    sampled = {}
    for t in tel:
        for f in range(t["start"], t["end"], step):
            sampled[f] = True
    n_samples = max(1, int(math.ceil(N / step)))
    cut_list = np.array(sorted(b for b in bnd))
    def near_cut(f, tol):
        if not len(cut_list):
            return False
        i = np.searchsorted(cut_list, f)
        return any(0 <= j < len(cut_list) and abs(int(cut_list[j]) - f) <= tol for j in (i - 1, i))
    sync = [near_cut(t["start"], step + 1) for t in tel] if tel else []
    vreg = Counter(t["v_region"] for t in tel)
    halign = Counter(t["h_align"] for t in tel)
    anims = Counter(t["anim_in"] for t in tel)
    fills = Counter(t["colors"]["fill"] for t in tel if t.get("colors"))
    outlines = Counter(t["colors"]["outline"] for t in tel if t.get("colors"))

    def quant(c):  # 似た色をまとめて数える（各チャンネル32段階）
        r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        return "#%02X%02X%02X" % (r // 32 * 32 + 16, g // 32 * 32 + 16, b // 32 * 32 + 16)
    fillq = Counter()
    for c, v in fills.items():
        fillq[quant(c)] += v
    outq = Counter()
    for c, v in outlines.items():
        outq[quant(c)] += v

    # 音とカットの同期
    cut_onset = None
    if audio is not None and audio["onsets"] and n_bound:
        on = np.array(audio["onsets"])
        tol = 2 / fps
        cts = np.array([b / fps for b in bnd])
        idx = np.searchsorted(on, cts)
        near = [any(0 <= j < len(on) and abs(on[j] - c) <= tol for j in (i - 1, i)) for i, c in zip(idx, cts)]
        rate = len(on) / max(dur, 1e-6)
        cut_onset = {"ratio": round(float(np.mean(near)), 3),
                     "chance": round(float(1 - math.exp(-rate * 2 * tol)), 3)}
    chars_per_sec = None
    if subs_lines and audio is not None:
        speech_sec = float(audio["speech_frames"].sum()) / fps
        nchar = sum(len(re.sub(r"\s", "", ln)) for ln in subs_lines)
        chars_per_sec = round(nchar / speech_sec, 2) if speech_sec > 0 else None

    lb = letterbox
    lb_ratio = {k: round(lb[k] / lb["rows"], 3) for k in ("top_rows", "bottom_rows")}
    lb_ratio.update({k: round(lb[k] / lb["cols"], 3) for k in ("left_cols", "right_cols")})

    s = {
        "source": {"input": src_label, **info, "analyzed_frames": N, "analyzed_sec": round(dur, 2),
                   "analyzed_at": datetime.now().isoformat(timespec="seconds")},
        "pacing": {
            "shots": len(normal), "cuts_per_min": round(n_bound / dur * 60, 2) if dur else None,
            "avg_shot_sec": round(float(sl.mean()), 3), "median_shot_sec": round(float(np.median(sl)), 3),
            "p10_shot_sec": pct(sl, 10), "p25_shot_sec": pct(sl, 25), "p75_shot_sec": pct(sl, 75),
            "p90_shot_sec": pct(sl, 90), "min_shot_sec": round(float(sl.min()), 3),
            "max_shot_sec": round(float(sl.max()), 3),
            "shot_length_hist": hist_bins(sl.tolist(), [0, 0.5, 1, 2, 3, 5, 8, 13, float("inf")]),
            "first_30s": {"shots": len(hook),
                          "avg_shot_sec": round(float(np.mean([h["dur_sec"] for h in hook])), 3) if hook else None},
            "per_30s": pacing,
        },
        "transitions": {
            "counts": dict(kinds), "share": share(kinds),
            "dissolve_frames_median": pct([g["frames"] for g in grads if g["type"] == "dissolve"], 50),
            "flash_frames_median": pct([sp["end"] - sp["start"] for sp in specials if sp["kind"] == "flash"], 50),
            "black": [{"sec": round(sp["start"] / fps, 2), "frames": sp["end"] - sp["start"],
                       "fade_out_frames": sp["fade_out_frames"], "fade_in_frames": sp["fade_in_frames"]}
                      for sp in specials if sp["kind"] == "black"],
        },
        "jump_cuts": {
            "hard_cuts_checked": len(jl),
            "same_setup_ratio": round(len(same) / len(jl), 3) if jl else None,
            "punch_in": len(punch_in), "punch_out": len(punch_out),
            "punch_in_scale_median": pct([j["scale"] for j in punch_in], 50),
            "punch_out_scale_median": pct([j["scale"] for j in punch_out], 50),
            "punch_in_per_min": round(len(punch_in) / dur * 60, 2) if dur else None,
        },
        "camera": {
            "motion_time_share": share(mshare, sum(mshare.values())),
            "zoom_pct_per_sec_median": pct(zoom_rates, 50),
            "quick_zoom_per_min": round(len(events["quick_zoom"]) / dur * 60, 2) if dur else None,
            "quick_zoom_pct_median": pct([abs(e["zoom_pct"]) for e in events["quick_zoom"]], 50),
            "shake_per_min": round(len(events["shake"]) / dur * 60, 2) if dur else None,
        },
        "framing": {"face_frame_ratio": round(face_ratio, 3) if face_ratio is not None else None,
                    "shot_size_time_share": share(sizes, sum(sizes.values())),
                    "face_x_time_share": share(fx, sum(fx.values()))},
        "telop": {
            "count": len(tel), "per_min": round(len(tel) / dur * 60, 2) if dur else None,
            "screen_time_ratio": round(len(sampled) / n_samples, 3),
            "dur_sec_median": pct([t["dur_sec"] for t in tel], 50),
            "dur_sec_p90": pct([t["dur_sec"] for t in tel], 90),
            "line_height_pct_median": round(100 * pct([t["h"] for t in tel], 50), 2) if tel else None,
            "line_height_pct_p90": round(100 * pct([t["h"] for t in tel], 90), 2) if tel else None,
            "approx_font_px_at_1080p_median": round(1080 * 0.8 * pct([t["h"] for t in tel], 50)) if tel else None,
            "width_pct_median": round(100 * pct([t["w"] for t in tel], 50), 1) if tel else None,
            "v_region_share": share(vreg), "h_align_share": share(halign),
            "starts_on_cut_ratio": round(float(np.mean(sync)), 3) if sync else None,
            "anim_in_share": share(anims),
            "fill_colors_top": [{"color": c, "count": v} for c, v in fillq.most_common(6)],
            "outline_colors_top": [{"color": c, "count": v} for c, v in outq.most_common(6)],
            "persistent": [{k: t[k] for k in ("x", "y", "w", "h", "dur_sec")} for t in pers],
        },
        "layout": {"letterbox_ratio": lb_ratio, "static_overlay": overlay},
        "color": {
            "palette": pal,
            "luma_mean": fmean(m["luma"]), "luma_p10": pct(m["luma"], 10), "luma_p90": pct(m["luma"], 90),
            "contrast_mean": fmean(m["luma_std"]), "saturation_mean": fmean(m["sat"]),
            "colorfulness_mean": fmean(m["colorful"]),
            "warmth_b_mean": fmean(m["lab_b"]), "tint_a_mean": fmean(m["lab_a"]),
        },
        "audio": dict(audio["summary"], cut_on_onset=cut_onset, chars_per_sec=chars_per_sec) if audio else None,
        "events": {"quick_zoom": len(events["quick_zoom"]), "shake": len(events["shake"]),
                   "flash": sum(sp["kind"] == "flash" for sp in specials),
                   "black": sum(sp["kind"] == "black" for sp in specials)},
    }
    return s


def profile_from_summary(s):
    """再現に使うパラメータだけを抜き出したプロファイル"""
    p, t, c, a = s["pacing"], s["telop"], s["color"], s["audio"] or {}
    return {
        "meta": {"generated_by": "video_style/analyze_video.py", "source": s["source"]["input"],
                 "analyzed_at": s["source"]["analyzed_at"]},
        "canvas": {"width": s["source"]["width"], "height": s["source"]["height"], "fps": s["source"]["fps"],
                   "letterbox_ratio": s["layout"]["letterbox_ratio"]},
        "pacing": {"cuts_per_min": p["cuts_per_min"], "avg_shot_sec": p["avg_shot_sec"],
                   "median_shot_sec": p["median_shot_sec"], "shot_sec_range_p10_p90": [p["p10_shot_sec"], p["p90_shot_sec"]],
                   "first_30s_avg_shot_sec": p["first_30s"]["avg_shot_sec"]},
        "transitions": {"share": s["transitions"]["share"],
                        "dissolve_frames": s["transitions"]["dissolve_frames_median"],
                        "flash_frames": s["transitions"]["flash_frames_median"]},
        "jump_cut": {"same_setup_ratio": s["jump_cuts"]["same_setup_ratio"],
                     "punch_in_per_min": s["jump_cuts"]["punch_in_per_min"],
                     "punch_in_scale": s["jump_cuts"]["punch_in_scale_median"]},
        "camera": {"motion_time_share": s["camera"]["motion_time_share"],
                   "slow_zoom_pct_per_sec": s["camera"]["zoom_pct_per_sec_median"],
                   "quick_zoom_per_min": s["camera"]["quick_zoom_per_min"],
                   "quick_zoom_pct": s["camera"]["quick_zoom_pct_median"],
                   "shake_per_min": s["camera"]["shake_per_min"]},
        "framing": s["framing"],
        "telop": {"per_min": t["per_min"], "screen_time_ratio": t["screen_time_ratio"],
                  "dur_sec": t["dur_sec_median"], "line_height_pct": t["line_height_pct_median"],
                  "font_px_at_1080p": t["approx_font_px_at_1080p_median"],
                  "v_region_share": t["v_region_share"], "h_align_share": t["h_align_share"],
                  "starts_on_cut_ratio": t["starts_on_cut_ratio"], "anim_in_share": t["anim_in_share"],
                  "fill_colors": [x["color"] for x in t["fill_colors_top"][:3]],
                  "outline_colors": [x["color"] for x in t["outline_colors_top"][:3]]},
        "color": {"palette": [x["color"] for x in c["palette"][:6]], "luma_mean": c["luma_mean"],
                  "contrast": c["contrast_mean"], "saturation": c["saturation_mean"],
                  "warmth_b": c["warmth_b_mean"]},
        "audio": {"integrated_lufs": a.get("integrated_lufs"), "true_peak_dbfs": a.get("true_peak_dbfs"),
                  "pause_median_sec": a.get("pause_median_sec"), "speech_ratio": a.get("speech_ratio"),
                  "bgm_level_in_pauses_db": a.get("gap_level_db"), "chars_per_sec": a.get("chars_per_sec"),
                  "cut_on_onset": a.get("cut_on_onset")},
        "manual_review": {  # sheets/ と transitions/ を見て記入する欄
            "telop_font": "", "telop_design": "", "telop_animation": "", "emphasis_rules": "",
            "sound_effects": "", "bgm": "", "overlays_and_stickers": "", "structure": "", "notes": "",
        },
    }


# ---------------------------------------------------------------- 書き出し

def write_csv(path: Path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})


def write_frames_csv(path, m, N, fps, text_samples, face_samples, audio, bnd):
    tb = {f: b for f, b in text_samples}
    fb = {f: b for f, b in face_samples}
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        cols = ["frame", "sec"] + METRIC_KEYS + ["text_boxes", "text_area", "faces", "face_h", "rms_db", "speech", "boundary"]
        w.writerow(cols)
        for n in range(N):
            row = [n, round(n / fps, 4)] + [("" if not np.isfinite(m[k][n]) else round(float(m[k][n]), 5)) for k in METRIC_KEYS]
            if n in tb:
                row += [len(tb[n]), round(sum(b[2] * b[3] for b in tb[n]), 5)]
            else:
                row += ["", ""]
            if n in fb:
                row += [len(fb[n]), round(max([x[3] for x in fb[n]], default=0), 4)]
            else:
                row += ["", ""]
            if audio is not None:
                row += [round(float(audio["rms_db"][n]), 2), int(audio["speech_frames"][n])]
            else:
                row += ["", ""]
            row.append(bnd.get(n, ""))
            w.writerow(row)


def draw_timeline(path, m, N, fps, shots, telops, audio, events):
    Wd, lane = 1800, 70
    names = ["shots", "shot length", "brightness/saturation", "zoom & pan", "telop", "audio"]
    Hd = 30 + lane * len(names)
    img = Image.new("RGB", (Wd + 170, Hd), (250, 250, 250))
    dr = ImageDraw.Draw(img)
    font = load_font(13)
    X = lambda f: 170 + int(f / max(N, 1) * Wd)
    dur = N / fps
    for i, nm in enumerate(names):
        y = 30 + i * lane
        dr.text((6, y + lane // 2 - 8), nm, fill=(40, 40, 40), font=font)
        dr.line([170, y + lane - 1, 170 + Wd, y + lane - 1], fill=(220, 220, 220))
    step = 60 if dur > 180 else 10
    for sec in np.arange(0, dur + 1e-6, step):
        x = X(sec * fps)
        dr.line([x, 22, x, Hd], fill=(232, 232, 232))
        dr.text((x + 2, 4), tc(sec)[:5], fill=(90, 90, 90), font=font)
    colors = {"normal": [(70, 130, 180), (120, 170, 210)], "flash": [(255, 200, 0)] * 2, "black": [(0, 0, 0)] * 2}
    y0 = 30
    for i, sh in enumerate(shots):
        col = colors.get(sh["kind"], colors["normal"])[i % 2]
        dr.rectangle([X(sh["start"]), y0 + 10, max(X(sh["start"]) + 1, X(sh["end"]) - 1), y0 + lane - 10], fill=col)
        if sh["transition_in"] in ("dissolve", "gradual_other", "fade_in"):
            dr.polygon([(X(sh["start"]), y0 + 2), (X(sh["start"]) - 4, y0 + 9), (X(sh["start"]) + 4, y0 + 9)], fill=(220, 0, 120))
    y0 = 30 + lane
    for sh in shots:
        hgt = min(sh["dur_sec"], 10) / 10 * (lane - 8)
        dr.line([X(sh["start"]), y0 + lane - 4, X(sh["start"]), y0 + lane - 4 - hgt], fill=(70, 130, 180))
    def series(y0, vals, lo, hi, col):
        pts = []
        for px in range(Wd):
            a, b = int(px / Wd * N), max(int(px / Wd * N) + 1, int((px + 1) / Wd * N))
            v = vals[a:b]
            v = v[np.isfinite(v)]
            if len(v):
                yv = y0 + lane - 4 - (np.clip(float(v.mean()), lo, hi) - lo) / (hi - lo) * (lane - 8)
                pts.append((170 + px, yv))
        if len(pts) > 1:
            dr.line(pts, fill=col, width=1)
    series(30 + 2 * lane, m["luma"], 0, 255, (60, 60, 60))
    series(30 + 2 * lane, m["sat"], 0, 255, (220, 80, 40))
    zoom = np.abs(np.log(np.where(np.isfinite(m["scale"]), m["scale"], 1.0)))
    pan = np.hypot(np.nan_to_num(m["pan_x"]), np.nan_to_num(m["pan_y"]))
    series(30 + 3 * lane, zoom, 0, 0.03, (150, 50, 200))
    series(30 + 3 * lane, pan, 0, 0.03, (40, 160, 90))
    for e in events["quick_zoom"]:
        dr.text((X(e["frame"]) - 3, 30 + 3 * lane), "Z", fill=(150, 50, 200), font=font)
    y0 = 30 + 4 * lane
    for t in telops:
        yy = y0 + 4 + t["cy"] * (lane - 8)
        col = (180, 180, 180) if t["persistent"] else (230, 120, 0)
        dr.line([X(t["start"]), yy, max(X(t["start"]) + 1, X(t["end"])), yy], fill=col, width=3)
    if audio is not None:
        y0 = 30 + 5 * lane
        for a, e in audio["segments"]:
            dr.rectangle([X(a * fps), y0 + lane - 10, X(e * fps), y0 + lane - 5], fill=(120, 200, 120))
        series(y0, audio["rms_db"], -60, 0, (30, 30, 30))
    img.save(path)


def write_report(path: Path, s: dict):
    p, tr, j, cam, fr, t, c, a = (s["pacing"], s["transitions"], s["jump_cuts"], s["camera"], s["framing"],
                                  s["telop"], s["color"], s["audio"])
    L = []
    src = s["source"]
    L.append("# 編集スタイル解析レポート（自動生成）\n")
    L.append(f"- 入力: `{src['input']}`")
    L.append(f"- 解析範囲: {src['analyzed_frames']}フレーム / {tc(src['analyzed_sec'])}（{src['width']}x{src['height']} {src['fps']}fps）\n")
    L.append("## テンポ（カット）")
    L.append(f"- ショット数 {p['shots']}、1分あたり切り替え {p['cuts_per_min']} 回")
    L.append(f"- 1ショットの長さ: 平均 {p['avg_shot_sec']}秒 / 中央値 {p['median_shot_sec']}秒（10〜90%: {p['p10_shot_sec']}〜{p['p90_shot_sec']}秒）")
    L.append(f"- 冒頭30秒の平均ショット長: {p['first_30s']['avg_shot_sec']}秒")
    L.append(f"- 長さの分布: {p['shot_length_hist']}\n")
    L.append("## 切り替え方")
    L.append(f"- 種類の割合: {tr['share']}（cut=普通のカット, flash=白フラッシュ, black=暗転, dissolve=クロスフェード）")
    L.append(f"- 同じ構図のままのカット（ジャンプカット）: {j['same_setup_ratio']}、拡大して繋ぐ（パンチイン）: {j['punch_in']}回（倍率中央値 {j['punch_in_scale_median']}）\n")
    L.append("## カメラワーク・演出")
    L.append(f"- ショットの動き（時間比）: {cam['motion_time_share']}")
    L.append(f"- 強調ズーム: 1分あたり {cam['quick_zoom_per_min']} 回（拡大率中央値 {cam['quick_zoom_pct_median']}%）、画面揺れ: 1分あたり {cam['shake_per_min']} 回")
    L.append(f"- 顔が映るフレーム比: {fr['face_frame_ratio']}、画角: {fr['shot_size_time_share']}\n")
    L.append("## テロップ")
    L.append(f"- 件数 {t['count']}（1分あたり {t['per_min']}）、画面に出ている時間の割合 {t['screen_time_ratio']}")
    L.append(f"- 表示時間 中央値 {t['dur_sec_median']}秒、行の高さ 画面の{t['line_height_pct_median']}%（1080pで約{t['approx_font_px_at_1080p_median']}px）")
    L.append(f"- 位置: {t['v_region_share']} / 横: {t['h_align_share']}、カットと同時に出る割合 {t['starts_on_cut_ratio']}")
    L.append(f"- 文字色候補: {[x['color'] for x in t['fill_colors_top']]}、縁取り候補: {[x['color'] for x in t['outline_colors_top']]}")
    if t["persistent"]:
        L.append(f"- 常時表示の文字（ロゴ・チャンネル名など）: {len(t['persistent'])}件")
    L.append("")
    L.append("## 色")
    L.append(f"- 主要色: {[x['color'] for x in c['palette'][:6]]}")
    r1 = lambda v: "-" if v is None else f"{v:.1f}"
    L.append(f"- 明るさ平均 {r1(c['luma_mean'])}/255 / コントラスト {r1(c['contrast_mean'])} / "
             f"彩度 {r1(c['saturation_mean'])}/255 / 暖色寄り度(b*、+で黄み・-で青み) {r1(c['warmth_b_mean'])}\n")
    if a:
        L.append("## 音")
        L.append(f"- ラウドネス {a['integrated_lufs']} LUFS / ピーク {a['true_peak_dbfs']} dBFS")
        L.append(f"- 発話の割合 {a['speech_ratio']}、話の切れ目（間）中央値 {a['pause_median_sec']}秒、間の音量 {a['gap_level_db']} dB（-50dBより大きければBGMが流れ続けている可能性）")
        L.append(f"- カットと音の立ち上がりの一致率 {a['cut_on_onset']}（chance=偶然の一致率）")
        if a.get("chars_per_sec"):
            L.append(f"- 話速（字幕ベース）: 約{a['chars_per_sec']}文字/秒")
        L.append("")
    L.append("## 目視レビュー用ファイル")
    L.append("- `sheets/` … 各ショットの代表フレーム一覧（ラベル: 番号 開始時刻 長さ 入り方 動き）")
    L.append("- `transitions/` … 切り替わり前後のコマ送り（赤枠 = 切り替わった直後のフレーム）")
    L.append("- `timeline.png` … 全体の時系列")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def to_jsonable(o):
    if isinstance(o, dict):
        return {str(k): to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not math.isfinite(float(o)) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


# ---------------------------------------------------------------- main

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="動画の編集スタイルをフレーム単位で解析する")
    ap.add_argument("video", help="動画ファイルのパス、または YouTube などの URL")
    ap.add_argument("-o", "--out", help="出力フォルダ（既定: analysis/<ファイル名>）")
    ap.add_argument("--analysis-width", type=int, default=640, help="解析に使う横幅px（既定640）")
    ap.add_argument("--keyframe-width", type=int, default=960, help="代表フレームの横幅px（既定960）")
    ap.add_argument("--text-step", type=int, default=3, help="テロップ検出を何フレームおきに行うか（1=全フレーム, 0=無効）")
    ap.add_argument("--face-step", type=int, default=5, help="顔検出を何フレームおきに行うか（0=無効）")
    ap.add_argument("--face-model", help="YuNet 顔検出モデル(.onnx)のパス")
    ap.add_argument("--subs", help="字幕ファイル(.vtt)。URL指定時は自動取得を試みる")
    ap.add_argument("--max-seconds", type=float, help="先頭から指定秒数だけ解析（お試し用）")
    ap.add_argument("--threads", type=int, default=0, help="テロップ検出のCPUスレッド数（0=自動）")
    ap.add_argument("--cut-min", type=float, default=15.0, help="カット判定の最小変化量")
    ap.add_argument("--cut-ratio", type=float, default=3.0, help="カット判定: 前後との変化量の比")
    ap.add_argument("--cut-hard", type=float, default=55.0, help="これ以上の変化は無条件でカット")
    ap.add_argument("--grad-min", type=float, default=30.0, help="ディゾルブ等の判定しきい値")
    args = ap.parse_args(argv)

    is_url = re.match(r"^https?://", args.video) is not None
    if args.out:
        out = Path(args.out)
    elif is_url:
        vid = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{6,})", args.video)
        out = Path("analysis") / (vid.group(1) if vid else "url_video")
    else:
        out = Path("analysis") / Path(args.video).stem
    out.mkdir(parents=True, exist_ok=True)
    path = fetch_url(args.video, out) if is_url else Path(args.video)
    if not path.exists():
        raise SystemExit(f"ファイルが見つかりません: {path}")

    info = probe(path)
    title = None
    ij = out / "source.info.json"
    if ij.exists():
        try:
            meta = json.loads(ij.read_text(encoding="utf-8"))
            title = meta.get("title")
            info.update({"title": title, "channel": meta.get("channel"), "upload_date": meta.get("upload_date"),
                         "webpage_url": meta.get("webpage_url")})
        except (json.JSONDecodeError, OSError):
            pass
    fps = info["fps"]
    log(f"[入力] {path.name}: {info['width']}x{info['height']} {fps}fps {tc(info['duration'])}"
        f"{' (可変fps→一定fpsに揃えて解析)' if info['vfr'] else ''}")

    m, N, text_samples, face_samples, letterbox, _ = pass1(path, info, args)
    audio = analyze_audio(path, info, N, fps, args.max_seconds)
    subs_lines = None
    subs = Path(args.subs) if args.subs else next(iter(sorted(out.glob("source*.vtt"))), None)
    if subs and subs.exists():
        subs_lines = parse_vtt(subs)

    bnd, specials, grads = detect_boundaries(m, N, fps, args)
    shots = build_shots(m, N, fps, bnd, specials, text_samples, face_samples, audio)
    events = detect_motion_events(m, shots, fps)
    telops = track_telops(text_samples, max(1, args.text_step), fps, N / fps) if args.text_step > 0 else []
    jumps, tiny_keys = pass2(path, info, args, N, fps, shots, bnd, specials, grads, telops, out)
    for sh in shots:
        j = jumps.get(sh["start"])
        if j:
            sh["jump"] = ("punch_in" if j["same_setup"] and j["scale"] and j["scale"] >= 1.06 else
                          "punch_out" if j["same_setup"] and j["scale"] and j["scale"] <= 0.94 else
                          "same_setup" if j["same_setup"] else "new_scene")
            sh["jump_scale"] = j["scale"]
    overlay = static_overlay(tiny_keys, out)
    pal = palette(tiny_keys)

    s = summarize(info, N, fps, m, shots, bnd, specials, grads, telops, events, jumps, audio, face_samples,
                  letterbox, overlay, pal, subs_lines, args, args.video if is_url else str(path))
    (out / "summary.json").write_text(json.dumps(to_jsonable(s), ensure_ascii=False, indent=2), encoding="utf-8")
    prof = to_jsonable(profile_from_summary(s))
    if yaml:
        (out / "style_profile.yaml").write_text(
            "# 編集スタイル・プロファイル（analyze_video.py が自動生成。manual_review は目視で記入）\n"
            + yaml.safe_dump(prof, allow_unicode=True, sort_keys=False), encoding="utf-8")
    else:
        (out / "style_profile.json").write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(out / "report.md", s)
    shot_cols = ["idx", "kind", "start", "end", "frames", "start_sec", "dur_sec", "transition_in", "jump",
                 "jump_scale", "motion", "zoom_pct", "pan_x_pct", "pan_y_pct", "jitter", "tracked", "luma",
                 "luma_std", "sat", "colorful", "lab_b", "dom_color", "text_boxes", "text_area", "face_ratio",
                 "face_h", "shot_size", "face_x", "speech_ratio", "rms_db", "keyframe"]
    write_csv(out / "shots.csv", shots, shot_cols)
    for t in telops:
        cc = t.get("colors") or {}
        t.update({"fill": cc.get("fill"), "outline": cc.get("outline"), "bg": cc.get("bg")})
    write_csv(out / "telops.csv", telops, ["start", "end", "start_sec", "dur_sec", "x", "y", "w", "h", "v_region",
                                           "h_align", "anim_in", "persistent", "fill", "outline", "bg", "samples"])
    ev = {"boundaries": [{"frame": f, "sec": round(f / fps, 3), "type": k} for f, k in bnd.items()],
          "specials": specials, "gradual": grads, **events,
          "audio_onsets_sec": audio["onsets"] if audio else []}
    (out / "events.json").write_text(json.dumps(to_jsonable(ev), ensure_ascii=False, indent=1), encoding="utf-8")
    write_frames_csv(out / "frames.csv", m, N, fps, text_samples, face_samples, audio, bnd)
    draw_timeline(out / "timeline.png", m, N, fps, shots, telops, audio, events)
    log(f"[完了] 出力: {out}")
    log(f"  ショット {s['pacing']['shots']} / 1分あたり切替 {s['pacing']['cuts_per_min']} / "
        f"平均ショット {s['pacing']['avg_shot_sec']}秒 / テロップ {s['telop']['count']}件")
    return s


if __name__ == "__main__":
    main()
