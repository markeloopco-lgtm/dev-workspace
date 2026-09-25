#!/usr/bin/env python3
"""参考動画をフレーム単位で分析し、同じクオリティで作るための数値と画像をまとめる。

  fetch    参考動画を取得する (yt-dlp)。refs/<動画ID>/source.mp4 などに保存
  analyze  全フレームを走査して カット・動き・画面レイアウト・色・音量 を計測し、
           report.md / report.json / 画像(タイムライン・代表フレーム一覧など)を書き出す
  frames   指定区間の全フレームを書き出す (テロップの出し方などを1コマずつ確認する用)
  compare  参考動画と自分の動画の report.json を並べ、差と調整の目安を出す

GPU不要。ffmpeg / ffprobe がPATHに必要 (docs/06 参照)。

usage:
  python scripts/analyze_video.py fetch https://youtu.be/XXXXXXXXXXX
  python scripts/analyze_video.py fetch URL --section 00:10:00-00:20:00
  python scripts/analyze_video.py analyze refs/XXXXXXXXXXX/source.mp4
  python scripts/analyze_video.py frames refs/XXXXXXXXXXX/source.mp4 --start 83.2 --duration 1.5
  python scripts/analyze_video.py compare refs/REF/source_report MINE_report
"""

import argparse
import csv
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORK = REPO_ROOT / "refs"   # 参考動画の置き場(videolabと共通。制作では使えない)
REPORT_VERSION = 1

ANALYSIS_LONG_SIDE = 320   # 分析用に縮小するときの長辺(px)。レイアウトと動きの把握には十分
MAX_SAMPLES = 400          # 中央値画像・配色用に等間隔で保持するフレーム数の上限
MAX_FRAME_DUMP = 300       # frames で一度に書き出すフレーム数の上限
STATIC_STD = 4.0           # 輝度の時間方向の標準偏差がこれ未満の画素を「固定」とみなす
CUT_MIN_CONTENT = 15.0     # カットとみなす直前フレームとの平均画素差の下限 (0-255)
CUT_RATIO = 3.0            # 前後2フレームの平均に対して何倍突出していればカット候補か
CUT_HIST = 0.55            # 色ヒストグラム距離がこれ以上ならカット候補 (動きの激しい場面同士)
CHANGE_THR = 20.0          # 約0.5秒前との平均画素差がこれ以上なら「大きな変化」区間
DISSOLVE_RESID = 0.3       # 中間コマを前後の画の混合で再現した残差の割合がこれ以下ならクロスフェード
QUIET_DB = -45.0           # これ未満(RMS dBFS)を無音とみなす
AUDIO_BLOCK = 0.1          # 音量を測る単位(秒)
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)  # BT.709
PTS_RE = re.compile(r"pts_time:\s*(-?[\d.]+(?:e-?\d+)?)")
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".avi")
THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")

TRANSITION_JA = {"start": "先頭", "cut": "カット", "dissolve": "クロスフェード", "fade": "暗転"}
LIVE_JA = {
    "was_live": "ライブ配信のアーカイブ", "post_live": "ライブ配信のアーカイブ(処理中)",
    "is_live": "ライブ配信中", "is_upcoming": "配信予定", "not_live": "通常の動画",
}

# 図の配色 (明るい背景。線は1系列1色、カット位置だけ別色。文字は墨色系)
SURFACE = (252, 252, 251)
INK = (11, 11, 11)
INK2 = (82, 81, 78)
MUTED = (137, 135, 129)
GRID = (225, 224, 217)
AXIS = (195, 194, 183)
SERIES = (42, 120, 214)
ACCENT = (235, 104, 52)   # カット
ACCENT2 = (27, 175, 122)  # クロスフェード・暗転


# ---------------------------------------------------------------- 共通

def parse_time(text) -> float:
    """'83.2' / '1:23.2' / '0:01:23.2' → 秒"""
    sec = 0.0
    for part in str(text).strip().split(":"):
        sec = sec * 60 + float(part)
    return sec


def fmt_time(sec, frac=2) -> str:
    """秒 → 'mm:ss.ss' (1時間以上は 'h:mm:ss.ss')"""
    sec = round(max(0.0, float(sec)), frac)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    width = 3 + frac if frac else 2
    body = f"{int(m):02d}:{s:0{width}.{frac}f}"
    return f"{int(h)}:{body}" if h >= 1 else body


def require_tools(*names):
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        raise SystemExit(
            f"[error] {', '.join(missing)} が見つかりません。docs/06 の準備手順で ffmpeg を入れてください"
            "（Windows: winget install --id Gyan.FFmpeg -e → PowerShellを開き直す）")


def _range_args(start, duration):
    args = []
    if start:
        args += ["-ss", f"{start:.3f}"]
    if duration:
        args += ["-t", f"{duration:.3f}"]
    return args


def _spawn(cmd, pts=None):
    """stdoutは呼び出し側で読む。stderrは別スレッドで回収(パイプ詰まり防止)し、
    showinfoの pts_time はリストへ、それ以外は末尾だけ残す。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tail = deque(maxlen=40)

    def pump():
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace")
            m = PTS_RE.search(line) if pts is not None else None
            if m:
                pts.append(float(m.group(1)))
            else:
                tail.append(line.rstrip())

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    return proc, tail, th


def _read_full(stream, buf) -> int:
    view = memoryview(buf)
    got = 0
    while got < len(buf):
        n = stream.readinto(view[got:])
        if not n:
            break
        got += n
    return got


def _runs(mask, min_len=1, max_gap=0):
    """Trueが連続する区間 [(開始, 終了(含まない)), ...]。max_gap以下の途切れはつなぐ"""
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return []
    runs = []
    a = b = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - b - 1 <= max_gap:
            b = i
        else:
            runs.append((a, b + 1))
            a = b = i
    runs.append((a, b + 1))
    return [(a, b) for a, b in runs if b - a >= min_len]


def _pct(values, q):
    return float(np.percentile(values, q)) if len(values) else None


def _smooth(x, win):
    """移動平均。端は端の値で埋める(0埋めだと両端が不自然に下がる/上がる)"""
    x = np.asarray(x, dtype=np.float64)
    win = min(win, len(x))
    if win <= 1:
        return x
    padded = np.pad(x, (win // 2, win - 1 - win // 2), mode="edge")
    return np.convolve(padded, np.ones(win) / win, mode="valid")


# ---------------------------------------------------------------- ffprobe

def _ratio(text):
    try:
        a, b = str(text).split("/")
        return float(a) / float(b) if float(b) else None
    except ValueError:
        return None


def _rotation(stream) -> int:
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            return int(round(float(sd["rotation"]))) % 360
    tag = (stream.get("tags") or {}).get("rotate")
    return int(tag) % 360 if tag else 0


def probe(path) -> dict:
    path = Path(path)
    res = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                          "-show_streams", str(path)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise SystemExit(f"[error] ffprobeで読めません: {path}\n{res.stderr.strip()}")
    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise SystemExit(f"[error] 映像が入っていません: {path}")
    fmt = data.get("format", {})
    w, h = int(v["width"]), int(v["height"])
    if _rotation(v) in (90, 270):
        w, h = h, w
    fps = _ratio(v.get("avg_frame_rate")) or _ratio(v.get("r_frame_rate")) or 30.0

    def kbps(x):
        return round(int(x) / 1000) if str(x).isdigit() else None

    return {
        "file": path.name,
        "container": fmt.get("format_name"),
        "duration_sec": round(float(fmt.get("duration") or v.get("duration") or 0.0), 3),
        "size_mb": round(int(fmt.get("size") or 0) / 1e6, 1),
        "overall_kbps": kbps(fmt.get("bit_rate")),
        "video": {
            "codec": v.get("codec_name"), "profile": v.get("profile"),
            "width": w, "height": h, "fps": round(fps, 3),
            "r_frame_rate": v.get("r_frame_rate"), "avg_frame_rate": v.get("avg_frame_rate"),
            "pix_fmt": v.get("pix_fmt"), "color_range": v.get("color_range"),
            "color_space": v.get("color_space"), "color_transfer": v.get("color_transfer"),
            "kbps": kbps(v.get("bit_rate")),
        },
        "audio": None if a is None else {
            "codec": a.get("codec_name"), "sample_rate": int(a.get("sample_rate") or 0),
            "channels": a.get("channels"), "layout": a.get("channel_layout"),
            "kbps": kbps(a.get("bit_rate")),
        },
    }


def analysis_size(w, h, long_side=ANALYSIS_LONG_SIDE):
    s = min(1.0, long_side / max(w, h))
    return max(2, int(round(w * s / 2)) * 2), max(2, int(round(h * s / 2)) * 2)


# ---------------------------------------------------------------- 映像の走査

@dataclass
class Scan:
    fps: float
    size: tuple            # 分析解像度 (w, h)
    long_lag: int          # diff_long の比較間隔(フレーム)
    t: np.ndarray          # 各フレームの時刻(秒・ファイル先頭基準)
    diff: np.ndarray       # 直前フレームとの平均画素差 (0-255)
    diff2: np.ndarray      # 2フレーム前との差 (フラッシュ判定用)
    diff3: np.ndarray      # 3フレーム前との差
    diff_long: np.ndarray  # 約0.5秒前との差 (ゆっくりした転換の検出用)
    changed: np.ndarray    # 直前フレームから明るさが変わった画素の割合 (0-1)
    hist: np.ndarray       # 直前フレームとの色ヒストグラム距離 (0-1)
    blend: np.ndarray      # 中間コマを前後(約0.5秒)の画の混合で再現した残差の割合 (0に近い=クロスフェード)
    luma: np.ndarray       # 平均輝度 (0-255)
    sat: np.ndarray        # 平均彩度 (0-1)
    motion_map: np.ndarray  # 画素ごとの平均変化量 (h, w)
    std_map: np.ndarray     # 画素ごとの輝度の時間方向の標準偏差 (h, w)
    median: np.ndarray      # 間引きフレームの画素ごとの中央値 = 画面の「土台」
    samples: list           # 等間隔に間引いたフレーム (uint8 RGB)


def scan_video(path, info, start=0.0, duration=None, sample_fps=None, verbose=True) -> Scan:
    """ffmpegで縮小RGBフレームを1枚ずつ受け取り、フレームごとの指標と画素ごとの累積を取る。"""
    w, h = analysis_size(info["video"]["width"], info["video"]["height"])
    fps = float(sample_fps or info["video"]["fps"])
    span = duration or max(0.0, info["duration_sec"] - start)
    est = max(1, int(span * fps))
    every = max(1, math.ceil(est / MAX_SAMPLES))
    lag = max(2, int(round(fps * 0.5)))
    vf = ([f"fps={sample_fps}"] if sample_fps else []) + [f"scale={w}:{h}:flags=area", "showinfo"]
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", *_range_args(start, duration),
           "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", ",".join(vf),
           "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    pts = []
    proc, tail, th = _spawn(cmd, pts)
    buf = bytearray(w * h * 3)
    ring = deque(maxlen=lag)  # 過去フレーム(int16)。ring[0]がlagフレーム前
    rows = []
    motion = np.zeros((h, w), np.float64)
    motion_n = 0
    pending = deque()  # (フレーム番号, 輝度差マップ)。前後2フレームが揃うまで保留
    dvals, hvals = [], []  # カット候補判定用の d1 / ヒストグラム距離
    lsum = np.zeros((h, w), np.float64)
    lsq = np.zeros((h, w), np.float64)
    samples = []
    prev_luma = prev_hist = None
    last = time.monotonic()
    try:
        while _read_full(proc.stdout, buf) == len(buf):
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            cur = frame.astype(np.int16)
            luma = frame.astype(np.float32) @ LUMA
            small = frame[::2, ::2]
            q = small >> 6  # 各チャンネル4段階 → 64色のヒストグラム
            codes = q[..., 0].astype(np.int32) * 16 + q[..., 1] * 4 + q[..., 2]
            hist = np.bincount(codes.ravel(), minlength=64) / codes.size
            mx = small.max(axis=2).astype(np.float32)
            sat = float(((mx - small.min(axis=2)) / np.maximum(mx, 1.0)).mean())
            if ring:
                d1 = float(np.abs(cur - ring[-1]).mean())
                d2 = float(np.abs(cur - ring[-2]).mean()) if len(ring) >= 2 else d1
                d3 = float(np.abs(cur - ring[-3]).mean()) if len(ring) >= 3 else d2
                dl = float(np.abs(cur - ring[0]).mean()) if len(ring) == lag else 0.0
                blend = float("nan")
                if dl >= CHANGE_THR:
                    # 中間のコマを「前後の画を画面全体で同じ割合αで混ぜたもの」で再現してみる。
                    # クロスフェード・暗転なら残差はほぼ0。ズーム・パンでは画素ごとに前後どちらかの
                    # 色になるので、1つのαでは再現しきれず残差が大きい
                    ba = (cur - ring[0]).astype(np.float32)
                    ma = (ring[lag - lag // 2] - ring[0]).astype(np.float32)
                    alpha = float((ma * ba).sum()) / max(float((ba * ba).sum()), 1.0)
                    if 0.2 <= alpha <= 0.8:
                        blend = float(np.abs(ma - alpha * ba).mean()) / dl
                dluma = np.abs(luma - prev_luma)
                chg = float((dluma > 6.0).mean())
                hd = float(0.5 * np.abs(hist - prev_hist).sum())
            else:
                dluma = None
                d1 = d2 = d3 = dl = chg = hd = 0.0
                blend = float("nan")
            lsum += luma
            lsq += luma * luma
            if len(rows) % every == 0 and len(samples) < MAX_SAMPLES:
                samples.append(frame.copy())
            rows.append((d1, d2, d3, dl, chg, hd, float(luma.mean()), sat, blend))
            dvals.append(d1)
            hvals.append(hd)
            pending.append((len(rows) - 1, dluma))
            while len(pending) > 2:
                motion_n += _accumulate_motion(motion, *pending.popleft(), dvals, hvals)
            ring.append(cur)
            prev_luma, prev_hist = luma, hist
            if verbose and time.monotonic() - last > 2:
                last = time.monotonic()
                print(f"\r  映像を走査中: {len(rows)}/{est} フレーム ({min(99, len(rows) * 100 // est)}%)",
                      end="", file=sys.stderr, flush=True)
    except BaseException:
        proc.kill()
        raise
    proc.wait()
    th.join(timeout=10)
    while pending:
        motion_n += _accumulate_motion(motion, *pending.popleft(), dvals, hvals)
    if verbose:
        print(f"\r  映像を走査中: {len(rows)} フレーム完了{' ' * 20}", file=sys.stderr)
    n = len(rows)
    if n == 0:
        raise SystemExit("[error] フレームを読み出せませんでした:\n" + "\n".join(list(tail)[-10:]))
    arr = np.array(rows, dtype=np.float64)
    t = start + (np.array(pts[:n]) if len(pts) >= n else np.arange(n) / fps)
    mean = lsum / n
    std = np.sqrt(np.maximum(lsq / n - mean * mean, 0.0))
    return Scan(
        fps=fps, size=(w, h), long_lag=lag, t=t,
        diff=arr[:, 0], diff2=arr[:, 1], diff3=arr[:, 2], diff_long=arr[:, 3],
        changed=arr[:, 4], hist=arr[:, 5], luma=arr[:, 6], sat=arr[:, 7], blend=arr[:, 8],
        motion_map=(motion / max(1, motion_n)).astype(np.float32), std_map=std.astype(np.float32),
        median=np.median(np.stack(samples), axis=0).astype(np.uint8), samples=samples,
    )


def _is_cut_candidate(d, hist, i):
    """detect_cuts と同じ基準の1フレーム版(d, histはフレームごとの値の列。範囲外は端の値)"""
    n = len(d)
    if i == 0:
        return False
    neigh = sum(d[min(max(k, 0), n - 1)] for k in (i - 2, i - 1, i + 1, i + 2)) / 4.0
    return ((d[i] >= CUT_MIN_CONTENT and d[i] / (neigh + 1.0) >= CUT_RATIO)
            or (hist[i] >= CUT_HIST and d[i] >= CUT_MIN_CONTENT * 0.5))


def _accumulate_motion(motion, i, dluma, d, hist) -> int:
    """カット・フラッシュのコマは画面全体が変わるので「動き」の集計から外す。"""
    if dluma is None or _is_cut_candidate(d, hist, i):
        return 0
    motion += dluma
    return 1


# ---------------------------------------------------------------- 検出

def detect_cuts(sc: Scan, min_content=CUT_MIN_CONTENT, ratio=CUT_RATIO, hist_thr=CUT_HIST, min_shot_sec=0.25):
    """カット(画面の切り替え)のフレーム番号と、カットと紛らわしいフラッシュの位置を返す。

    直前フレームとの差が前後2フレームの平均より突出していればカット候補
    (PySceneDetectのAdaptiveDetectorと同じ考え方)。動きの激しい場面同士のカットは
    色ヒストグラムの大きな変化で拾う。1〜2フレームだけ光って元の画に戻るものは除外。
    """
    d = sc.diff
    n = len(d)
    if n < 3:
        return [], []
    pad = np.pad(d, 2, mode="edge")
    neigh = (pad[0:n] + pad[1:n + 1] + pad[3:n + 3] + pad[4:n + 4]) / 4.0
    score = d / (neigh + 1.0)  # +1: 静止画面の微小ノイズで比が暴れるのを防ぐ
    cand = ((score >= ratio) & (d >= min_content)) | ((sc.hist >= hist_thr) & (d >= min_content * 0.5))
    cand[0] = False
    idx = [int(i) for i in np.flatnonzero(cand)]
    flashes, drop = [], set()
    for k, i in enumerate(idx):
        if i in drop:
            continue
        for j in idx[k + 1:k + 3]:
            gap = j - i
            if gap > 2:
                break
            back = sc.diff2[j] if gap == 1 else sc.diff3[j]  # 光る直前のコマとの差
            if back < min_content * 0.66:
                flashes.append(i)
                drop.update((i, j))
                break
    idx = [i for i in idx if i not in drop]
    min_gap = max(1, int(round(min_shot_sec * sc.fps)))
    kept = []
    for i in sorted(idx, key=lambda i: -d[i]):  # 近すぎる候補は強い方だけ残す
        if all(abs(i - c) >= min_gap for c in kept):
            kept.append(i)
    return sorted(kept), flashes


def detect_flash_events(sc: Scan, guard_flashes):
    """白く光るコマ(輝度が前後より大きく跳ねる)とカット判定で除外したフラッシュをまとめる。"""
    lum = sc.luma
    n = len(lum)
    pad = np.pad(lum, 2, mode="edge")
    base = np.minimum(pad[0:n], pad[4:n + 4])
    mask = (lum >= 200) & (lum - base >= 50)
    for i in guard_flashes:
        mask[i] = True
    return [{"start": float(sc.t[a]), "frames": b - a} for a, b in _runs(mask, 1, max_gap=2)]


def detect_black(sc: Scan, thr=12.0, min_frames=2):
    return _runs(sc.luma < thr, min_frames)


def detect_changes(sc: Scan, cuts, flash_frames, thr=CHANGE_THR):
    """カット以外で約0.5秒の間に画が大きく変わった区間を拾い、種類を分ける。

    dissolve: 2つの画が混ざりながら入れ替わる(クロスフェード) → 素材の切り替えに数える
    fade:     黒(暗転)へ/からのディゾルブ → 素材の切り替えに数える
    motion:   それ以外(ズーム・パン・スライド・ワイプ・大きな動き・演出)。切り替えには数えない

    素材自体が速く動いていると動画の大部分が「大きな変化」になるため、クロスフェードは
    「混合で再現できるコマ(blend ≦ DISSOLVE_RESID)」の連なりごとに1回として数え、
    その中間コマの位置の中央値を切り替えの中心とする。
    """
    lag, n = sc.long_lag, len(sc.t)
    mask = sc.diff_long >= thr
    for c in list(cuts) + list(flash_frames):
        mask[c:c + lag + 1] = False  # カット/フラッシュを跨ぐ比較は除外
    mixed = mask & (np.nan_to_num(sc.blend, nan=np.inf) <= DISSOLVE_RESID)
    segs = []
    covered = np.zeros(n, bool)
    for a, b in _runs(mixed, min_len=2, max_gap=3):
        idx = np.flatnonzero(mixed[a:b]) + a
        center = int(np.median(idx - lag // 2))
        dark = min(sc.luma[max(0, a - lag)], sc.luma[b - 1]) < 12
        segs.append(_change_seg(sc, a, b, "fade" if dark else "dissolve", center))
        covered[max(0, a - lag):min(n, b + lag)] = True
    for a, b in _runs(mask & ~covered, min_len=3, max_gap=2):
        segs.append(_change_seg(sc, a, b, "motion", None))
    return sorted(segs, key=lambda s: s["start"])


def _change_seg(sc: Scan, a, b, kind, center):
    blend = sc.blend[a:b]
    best = float(np.nanmin(blend)) if np.isfinite(blend).any() else None
    return {"start": float(sc.t[max(0, a - sc.long_lag)]), "end": float(sc.t[b - 1]),
            "peak": round(float(sc.diff_long[a:b].max()), 1), "kind": kind,
            "blend": None if best is None else round(best, 3),
            "frame": center, "at": None if center is None else float(sc.t[center])}


def merge_boundaries(sc: Scan, cuts, changes, black_mask=None, min_shot_sec=0.25):
    """カットとクロスフェード/暗転をまとめた画面の切り替え [(フレーム番号, 種類)]。
    黒画面の直前・直後のカットは「暗転」(素早いフェードも数コマのカットに見えるため)。"""
    min_gap = max(1, int(round(min_shot_sec * sc.fps)))

    def near_black(c):
        return black_mask is not None and bool(black_mask[max(0, c - min_gap):c + min_gap].any())

    marks = sorted([(c, "fade" if near_black(c) else "cut") for c in cuts]
                   + [(s["frame"], s["kind"]) for s in changes if s["kind"] != "motion"])
    out = []
    for f, kind in marks:
        if f <= 0:
            continue
        if out and f - out[-1][0] < min_gap:
            if kind == "cut" and out[-1][1] != "cut":  # 近すぎる時はカットを優先
                out[-1] = (f, kind)
            continue
        out.append((f, kind))
    return out


def effective_fps(sc: Scan, cuts):
    """動いている場面で、実際に絵が更新されている頻度を推定する。

    60fpsの動画でも中身が30fps(同じ絵が2回ずつ)ということがよくある。動いている場面
    (前後どちらかのコマで画素が変わっている)の中で、変化ゼロのコマ=重複コマの割合を数える。
    """
    c = sc.changed
    n = len(c)
    if n < 30:
        return None
    pad = np.pad(c, 1, mode="edge")
    local = np.maximum(np.maximum(pad[:-2], pad[2:]), c)
    moving = local >= 0.002
    moving[0] = False
    for i in cuts:
        moving[max(0, i - 1):i + 2] = False
    m = int(moving.sum())
    if m < 30:
        return None
    dup = moving & (c <= local * 0.1)
    return {"fps": round(float(sc.fps * (1 - dup.sum() / m)), 1), "frames_used": m}


def motion_ja(m):
    return "静止" if m < 0.5 else ("ゆっくり" if m < 2.5 else ("動きあり" if m < 7 else "激しい"))


def build_shots(sc: Scan, boundaries, black_mask):
    """切り替えで区切った各ショット(=素材1本ぶん)の長さ・入り方・明るさ・彩度・動き。"""
    n = len(sc.t)
    marks = [(0, "start")] + list(boundaries)
    end_t = float(sc.t[-1]) + 1.0 / sc.fps
    shots = []
    for k, (a, kind) in enumerate(marks):
        b = marks[k + 1][0] if k + 1 < len(marks) else n
        ta = float(sc.t[a])
        tb = float(sc.t[b]) if b < n else end_t
        inner = sc.diff[a + 1:b]
        bright = float(sc.luma[a:b].mean())
        shots.append({"index": k + 1, "start": round(ta, 3), "end": round(tb, 3),
                      "duration": round(tb - ta, 3), "transition_in": kind,
                      "brightness": round(bright, 1),
                      "saturation": round(float(sc.sat[a:b].mean()), 3),
                      "motion": round(float(np.median(inner)), 2) if len(inner) else 0.0,
                      # 黒が3割以上 or 1秒未満の真っ暗なつなぎ = 暗転の途中で、素材ではない
                      "black": bool(black_mask[a:b].mean() >= 0.3 or (tb - ta < 1.0 and bright < 25))})
    return shots


def material_transitions(shots):
    """素材から素材への切り替えの種類の列。黒いショットを挟んだものは「暗転」1回と数え、
    直後の素材の入り方も「暗転」にする。"""
    kinds, prev, dark_between = [], None, False
    for s in shots:
        if s["black"]:
            dark_between = prev is not None
            continue
        if prev is not None:
            if dark_between:
                s["transition_in"] = "fade"
            kinds.append(s["transition_in"])
        prev, dark_between = s, False
    return kinds


def write_shots_csv(shots, path):
    """ショット一覧(Excelで開けるようBOM付きUTF-8)。同じ構成で作るときの設計図になる。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["番号", "開始", "開始(秒)", "長さ(秒)", "入り方", "明るさ(0-255)", "彩度(0-1)",
                    "動き", "動きの目安", "黒画面"])
        for s in shots:
            w.writerow([s["index"], fmt_time(s["start"]), s["start"], s["duration"],
                        TRANSITION_JA[s["transition_in"]], s["brightness"], s["saturation"],
                        s["motion"], motion_ja(s["motion"]), "○" if s["black"] else ""])


def _position_ja(x0, x1, y0, y1):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx = "左" if cx < 40 else ("右" if cx > 60 else "中央")
    vy = "上" if cy < 40 else ("下" if cy > 60 else "中")
    return f"{hx}・{vy}"


def dynamic_regions(motion_map, cell=8, top=3):
    """動きが集まっている領域(キャラ・字幕・コメント欄など)を粗いグリッドの連結成分で求める。"""
    h, w = motion_map.shape
    gh, gw = h // cell, w // cell
    if gh == 0 or gw == 0:
        return []
    grid = motion_map[:gh * cell, :gw * cell].reshape(gh, cell, gw, cell).mean(axis=(1, 3))
    total = float(grid.sum())
    if total <= 0:
        return []
    active = grid >= max(0.3, 0.2 * float(np.percentile(grid, 99)))
    seen = np.zeros_like(active)
    regions = []
    for y in range(gh):
        for x in range(gw):
            if not active[y, x] or seen[y, x]:
                continue
            stack, cells = [(y, x)], []
            seen[y, x] = True
            while stack:
                cy, cx = stack.pop()
                cells.append((cy, cx))
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < gh and 0 <= nx < gw and active[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            ys = [c[0] for c in cells]
            xs = [c[1] for c in cells]
            energy = float(sum(grid[c] for c in cells))
            x0, x1 = min(xs) * cell / w * 100, (max(xs) + 1) * cell / w * 100
            y0, y1 = min(ys) * cell / h * 100, (max(ys) + 1) * cell / h * 100
            regions.append({
                "x0": round(x0, 1), "x1": round(x1, 1), "y0": round(y0, 1), "y1": round(y1, 1),
                "area": round((x1 - x0) * (y1 - y0) / 100, 1),  # 画面に対する外接矩形の面積%
                "share": round(energy / total, 3),               # 全体の動きのうちこの領域の割合
                "motion": round(energy / len(cells), 2),         # 領域内の平均変化量
                "where": _position_ja(x0, x1, y0, y1),
            })
    regions = [r for r in regions if r["share"] >= 0.05]
    regions.sort(key=lambda r: -r["share"])
    return regions[:top]


# ---------------------------------------------------------------- 色

def palette(samples, k=8, seed=0):
    """間引きフレームの画素をk-meansでまとめた主要色(割合の多い順)。"""
    px = np.concatenate([s[::3, ::3].reshape(-1, 3) for s in samples]).astype(np.float32)
    rng = np.random.default_rng(seed)
    if len(px) > 60000:
        px = px[rng.choice(len(px), 60000, replace=False)]
    centers = [px[rng.integers(len(px))]]
    for _ in range(1, k):  # k-means++ 初期化
        d2 = np.min(((px[:, None, :] - np.array(centers)[None]) ** 2).sum(-1), axis=1).astype(np.float64)
        if d2.sum() <= 0:
            break
        centers.append(px[rng.choice(len(px), p=d2 / d2.sum())])
    cen = np.array(centers)
    for _ in range(30):
        lab = np.argmin(((px[:, None, :] - cen[None]) ** 2).sum(-1), axis=1)
        new = np.array([px[lab == j].mean(0) if np.any(lab == j) else cen[j] for j in range(len(cen))])
        done = np.allclose(new, cen, atol=0.5)
        cen = new
        if done:
            break
    lab = np.argmin(((px[:, None, :] - cen[None]) ** 2).sum(-1), axis=1)
    share = np.bincount(lab, minlength=len(cen)) / len(px)
    return [{"hex": "#%02x%02x%02x" % tuple(int(round(v)) for v in cen[j]), "share": round(float(share[j]), 3)}
            for j in np.argsort(-share) if share[j] > 0]


def color_stats(sc: Scan):
    contrast, colorful = [], []
    for s in sc.samples:
        f = s.astype(np.float32)
        contrast.append(float((f @ LUMA).std()))
        rg = f[..., 0] - f[..., 1]
        yb = 0.5 * (f[..., 0] + f[..., 1]) - f[..., 2]
        # Hasler & Süsstrunk (2003) のカラフルさ指標
        colorful.append(float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean())))
    return {
        "brightness": round(float(sc.luma.mean()), 1),
        "contrast": round(float(np.median(contrast)), 1),
        "saturation": round(float(sc.sat.mean()), 3),
        "colorfulness": round(float(np.median(colorful)), 1),
    }


# ---------------------------------------------------------------- 音

def scan_audio(path, start=0.0, duration=None):
    """モノラル16kHzで読み、0.1秒ごとのRMS(dBFS)を返す。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", *_range_args(start, duration),
           "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    proc, tail, th = _spawn(cmd)
    blk = int(16000 * AUDIO_BLOCK) * 2
    out, rest = [], b""
    try:
        while True:
            data = proc.stdout.read(blk * 200)
            if not data:
                break
            data = rest + data
            usable = len(data) // blk * blk
            a = np.frombuffer(data[:usable], "<i2").astype(np.float32).reshape(-1, blk // 2) / 32768.0
            out.append(np.sqrt((a * a).mean(axis=1)))
            rest = data[usable:]
    except BaseException:
        proc.kill()
        raise
    proc.wait()
    th.join(timeout=10)
    rms = np.concatenate(out) if out else np.zeros(0)
    return 20 * np.log10(np.maximum(rms, 1e-6))


def measure_loudness(path, start=0.0, duration=None):
    """ffmpegのebur128で 統合ラウドネス(LUFS)・ラウドネスレンジ(LU)・トゥルーピーク(dBFS)。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", *_range_args(start, duration), "-i", str(path),
           "-map", "0:a:0", "-vn", "-af", "ebur128=peak=true:framelog=verbose", "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    summary = res.stderr[res.stderr.rfind("Summary:"):]

    def grab(pat):
        m = re.search(pat, summary)
        return float(m.group(1)) if m else None

    return {"lufs": grab(r"I:\s+(-?[\d.]+) LUFS"), "lra": grab(r"LRA:\s+(-?[\d.]+) LU"),
            "true_peak": grab(r"Peak:\s+(-?[\d.]+) dBFS")}


def audio_summary(db):
    if len(db) == 0:
        return {}
    quiet = db < QUIET_DB
    pauses = _runs(quiet, min_len=int(round(0.3 / AUDIO_BLOCK)))  # 0.3秒以上の無音
    minutes = len(db) * AUDIO_BLOCK / 60
    floor = float(np.percentile(db, 10))
    lens = [(b - a) * AUDIO_BLOCK for a, b in pauses]
    if floor > QUIET_DB and quiet.mean() < 0.05:
        hint = "常に何か鳴っている（BGMが流れ続けている可能性が高い）"
    elif quiet.mean() >= 0.15:
        hint = "無音の間がはっきりある（BGMなし、または声の間で止まる）"
    else:
        hint = "無音は少なめ（BGMが小さめに流れている可能性）"
    return {
        "quiet_ratio": round(float(quiet.mean()), 3),
        "pauses_per_min": round(len(pauses) / minutes, 1) if minutes else None,
        "pause_median_sec": round(float(np.median(lens)), 2) if lens else None,
        "floor_db": round(floor, 1),
        "median_db": round(float(np.percentile(db, 50)), 1),
        "loud_db": round(float(np.percentile(db, 95)), 1),
        "bgm_hint": hint,
    }


# ---------------------------------------------------------------- 描画

_FONTS = {}
_CJK = [None]


def _font_candidates():
    win = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    return [win / "meiryo.ttc", win / "YuGothM.ttc", win / "msgothic.ttc",
            Path("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"),
            Path("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf")]


def font(size):
    """日本語フォントがあれば使う。無ければPillow内蔵フォント(図中の文字は英語になる)。"""
    if size not in _FONTS:
        for p in _font_candidates():
            if p.exists():
                try:
                    _FONTS[size] = ImageFont.truetype(str(p), size)
                    _CJK[0] = True
                    break
                except OSError:
                    continue
        else:
            _FONTS[size] = ImageFont.load_default(size=size)
            _CJK[0] = False
    return _FONTS[size]


def txt(ja, en):
    font(12)
    return ja if _CJK[0] else en


def _nice_step(span, target):
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    return next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)


TIME_STEPS = (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)


def draw_timeline(sc: Scan, boundaries, audio_db, audio_t0, path):
    """映像の変化量と音量を時間軸でそろえた2段の図。素材の切り替え位置は縦線。"""
    W, left, right, ph, top, gap = 1600, 72, 24, 170, 60, 78
    panels = ["motion"] + (["audio"] if audio_db is not None and len(audio_db) else [])
    H = top + len(panels) * (ph + gap) + 8
    img = Image.new("RGB", (W, H), SURFACE)
    d = ImageDraw.Draw(img)
    t0 = float(sc.t[0])
    t1 = max(float(sc.t[-1]) + 1.0 / sc.fps, t0 + 1e-3)
    pw = W - left - right

    def X(t):
        return left + (t - t0) / (t1 - t0) * pw

    d.text((left, 16), txt("タイムライン", "Timeline") + f"  {fmt_time(t0, 0)} - {fmt_time(t1, 0)}",
           font=font(22), fill=INK)
    tstep = next((s for s in TIME_STEPS if (t1 - t0) / s <= 12), 3600)
    for k, kind in enumerate(panels):
        y0 = top + k * (ph + gap) + 34
        y1 = y0 + ph
        if kind == "motion":
            ts, vals = sc.t, _smooth(sc.diff, max(1, int(round(sc.fps * 0.5))))
            lo, hi = 0.0, max(1.0, float(np.percentile(vals, 99.5)) * 1.15)
            title = txt("映像の変化量（前フレームとの平均画素差・0.5秒平均）",
                        "Visual change (mean abs diff vs previous frame, 0.5s avg)")
            key = txt("変化量", "change")
        else:
            power = _smooth(10 ** (audio_db / 10), int(round(1.0 / AUDIO_BLOCK)))  # dBはエネルギーで平均
            vals = 10 * np.log10(np.maximum(power, 1e-12))
            ts = audio_t0 + (np.arange(len(vals)) + 0.5) * AUDIO_BLOCK
            lo, hi = -60.0, 0.0
            title = txt("音量（RMS dBFS・1秒平均）", "Level (RMS dBFS, 1s avg)")
            key = txt("音量", "level")
        d.text((left, y0 - 30), title, font=font(16), fill=INK)
        kx = W - right - 390  # 凡例: 線=指標 / 縦線=切り替えの種類
        d.line([(kx, y0 - 20), (kx + 24, y0 - 20)], fill=SERIES, width=2)
        d.text((kx + 30, y0 - 29), key, font=font(14), fill=INK2)
        d.line([(kx + 110, y0 - 28), (kx + 110, y0 - 12)], fill=ACCENT, width=2)
        d.text((kx + 118, y0 - 29), txt("カット", "cut"), font=font(14), fill=INK2)
        d.line([(kx + 190, y0 - 28), (kx + 190, y0 - 12)], fill=ACCENT2, width=2)
        d.text((kx + 198, y0 - 29), txt("クロスフェード・暗転", "dissolve / fade"), font=font(14), fill=INK2)

        def Y(v, lo=lo, hi=hi, y1=y1):
            return y1 - (min(max(v, lo), hi) - lo) / (hi - lo) * ph

        step = _nice_step(hi - lo, 4)
        v = math.ceil(lo / step) * step
        while v <= hi + 1e-9:
            y = Y(v)
            d.line([(left, y), (W - right, y)], fill=GRID, width=1)
            d.text((left - 8, y), f"{v:g}", font=font(12), fill=MUTED, anchor="rm")
            v += step
        for f, kind in boundaries:
            x = X(float(sc.t[f]))
            d.line([(x, y0), (x, y1)], fill=ACCENT if kind == "cut" else ACCENT2, width=2)
        cols = np.clip(((ts - t0) / (t1 - t0) * (pw - 1)).astype(int), 0, pw - 1)
        cnt = np.bincount(cols, minlength=pw)
        tot = np.bincount(cols, weights=vals, minlength=pw)
        pts = [(left + int(i), Y(tot[i] / cnt[i])) for i in np.flatnonzero(cnt)]
        if len(pts) >= 2:
            d.line(pts, fill=SERIES, width=2, joint="curve")
        d.line([(left, y1), (W - right, y1)], fill=AXIS, width=1)
        tt = math.ceil(t0 / tstep) * tstep
        while tt <= t1:
            d.text((X(tt), y1 + 6), fmt_time(tt, 0 if tstep >= 1 else 1), font=font(12), fill=MUTED, anchor="mt")
            tt += tstep
    img.save(path)


def draw_palette(pal, path):
    W, bar, pad = 960, 56, 8
    img = Image.new("RGB", (W + 2 * pad, bar + 62), SURFACE)
    d = ImageDraw.Draw(img)
    x = float(pad)
    for i, p in enumerate(pal):
        w = p["share"] * W if i < len(pal) - 1 else (W + pad - x)
        rgb = tuple(int(p["hex"][k:k + 2], 16) for k in (1, 3, 5))
        if w > 2:
            d.rectangle([x, pad, x + w - 2, pad + bar], fill=rgb)  # 2pxのすき間で区切る
        if w >= 64:
            d.text((x + 2, pad + bar + 6), p["hex"], font=font(13), fill=INK2)
            d.text((x + 2, pad + bar + 25), f"{p['share'] * 100:.0f}%", font=font(13), fill=MUTED)
        x += w
    img.save(path)


def _checker(size, cell=12):
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    board = ((xx // cell + yy // cell) % 2)[..., None]
    return Image.fromarray(np.repeat(np.where(board == 1, 232, 248).astype(np.uint8), 3, axis=2))


def draw_layout(sc: Scan, regions, outdir: Path):
    """layout_motion.png: 画面の土台(中央値画像)に動きの多い場所を青で重ねる
    layout_static.png: 動画全体でほぼ変化しない部分だけを残した画(背景・枠・ロゴ)"""
    scale = max(1, round(960 / max(sc.size)))
    W, H = sc.size[0] * scale, sc.size[1] * scale
    base = Image.fromarray(sc.median).resize((W, H), Image.BICUBIC)
    gray = Image.blend(ImageOps.grayscale(base).convert("RGB"), Image.new("RGB", (W, H), (255, 255, 255)), 0.35)
    hi = float(np.percentile(sc.motion_map, 99.5)) or 1.0
    a = np.clip(sc.motion_map / hi, 0, 1) ** 0.7
    alpha = Image.fromarray((a * 220).astype(np.uint8)).resize((W, H), Image.BILINEAR)
    heat = Image.composite(Image.new("RGB", (W, H), SERIES), gray, alpha)
    d = ImageDraw.Draw(heat)
    for k, r in enumerate(regions, 1):
        box = [r["x0"] / 100 * W, r["y0"] / 100 * H, r["x1"] / 100 * W - 1, r["y1"] / 100 * H - 1]
        d.rectangle(box, outline=INK, width=2)
        d.rectangle([box[0], box[1], box[0] + 22, box[1] + 22], fill=INK)
        d.text((box[0] + 11, box[1] + 11), str(k), font=font(15), fill=(255, 255, 255), anchor="mm")
    heat.save(outdir / "layout_motion.png")
    mask = Image.fromarray(((sc.std_map < STATIC_STD) * 255).astype(np.uint8)).resize((W, H), Image.NEAREST)
    Image.composite(base, _checker((W, H)), mask).save(outdir / "layout_static.png")


def grab_frame(path, t, width):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
           "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2pipe", "-c:v", "png", "-"]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0 or not res.stdout:
        return None
    return Image.open(io.BytesIO(res.stdout)).convert("RGB")


def render_sheets(tiles, out_prefix: Path, cols, per_sheet, tile_w):
    """tiles: [(PIL画像 or None, 見出し)] を格子に並べた一覧画像を書き、パス一覧を返す。"""
    ref = next((im for im, _ in tiles if im is not None), None)
    tile_h = round(tile_w * ref.height / ref.width) if ref else round(tile_w * 9 / 16)
    pad, cap = 8, 26
    files = []
    for s in range(0, len(tiles), per_sheet):
        chunk = tiles[s:s + per_sheet]
        rows = math.ceil(len(chunk) / cols)
        sheet = Image.new("RGB", (pad + cols * (tile_w + pad), pad + rows * (tile_h + cap + pad)), SURFACE)
        d = ImageDraw.Draw(sheet)
        for k, (im, label) in enumerate(chunk):
            r, c = divmod(k, cols)
            x, y = pad + c * (tile_w + pad), pad + r * (tile_h + cap + pad)
            if im is None:
                d.rectangle([x, y, x + tile_w - 1, y + tile_h - 1], fill=GRID)
            else:
                sheet.paste(im if im.size == (tile_w, tile_h) else im.resize((tile_w, tile_h), Image.LANCZOS), (x, y))
            d.text((x + 2, y + tile_h + 4), label, font=font(15), fill=INK)
        fn = out_prefix.parent / f"{out_prefix.name}_{s // per_sheet + 1:02d}.jpg"
        sheet.save(fn, quality=88)
        files.append(fn)
    return files


def _sheet_layout(width, height):
    """横長: 400px×4列×20枚 / 縦長: 216px×6列×18枚"""
    return (400, 4, 20) if width >= height else (216, 6, 18)


def contact_sheets(video, items, out_prefix, width, height):
    tile_w, cols, per = _sheet_layout(width, height)
    with ThreadPoolExecutor(max_workers=4) as ex:
        imgs = list(ex.map(lambda it: grab_frame(video, it[0], tile_w), items))
    return render_sheets(list(zip(imgs, [label for _, label in items])), out_prefix, cols, per, tile_w)


# ---------------------------------------------------------------- 分析本体

def load_source_meta(video: Path):
    """fetchで保存した <名前>.info.json があれば動画の素性を拾う。"""
    p = video.with_name(video.stem + ".info.json")
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    date = d.get("upload_date") or ""
    return {
        "title": d.get("title"), "channel": d.get("channel") or d.get("uploader"),
        "channel_url": d.get("channel_url") or d.get("uploader_url"), "url": d.get("webpage_url"),
        "upload_date": f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else (date or None),
        "duration_sec": d.get("duration"), "live_status": d.get("live_status"),
        "view_count": d.get("view_count"), "like_count": d.get("like_count"),
        "categories": d.get("categories"), "tags": (d.get("tags") or [])[:15],
        "chapters": [{"start": c.get("start_time"), "title": c.get("title")} for c in d.get("chapters") or []][:30],
        "section_start": d.get("section_start"), "section_end": d.get("section_end"),
        "description": (d.get("description") or "")[:800],
    }


def editing_style(per_min):
    if per_min < 0.5:
        return "ほぼ切り替えなし（配信・固定画面型）"
    if per_min < 4:
        return "ゆったりした編集"
    if per_min < 12:
        return "標準的な編集テンポ"
    return "テンポの速い編集（ショート・切り抜き系に多い）"


def analyze(video, outdir=None, start=0.0, duration=None, sample_fps=None, sheets=True, verbose=True) -> dict:
    require_tools("ffmpeg", "ffprobe")
    video = Path(video)
    if str(video).startswith(("http://", "https://")):
        raise SystemExit("[error] URLは先に fetch で取得してください: python scripts/analyze_video.py fetch URL")
    if not video.exists():
        raise SystemExit(f"[error] ファイルがありません: {video}")
    log = print if verbose else (lambda *a, **k: None)
    info = probe(video)
    vi = info["video"]
    partial = bool(start or duration)
    if outdir is None:
        end_hint = start + duration if duration else info["duration_sec"]
        outdir = video.parent / (f"{video.stem}_report" + (f"_{int(start)}-{int(end_hint)}s" if partial else ""))
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    log(f"分析: {video}  {vi['width']}x{vi['height']} {vi['fps']:g}fps  長さ {fmt_time(info['duration_sec'], 0)}"
        + (f"  区間 {fmt_time(start, 0)}から{fmt_time(duration, 0) if duration else '最後まで'}" if partial else ""))

    sc = scan_video(video, info, start, duration, sample_fps, verbose)
    n = len(sc.t)
    cuts, guard_flashes = detect_cuts(sc)
    flashes = detect_flash_events(sc, guard_flashes)
    black_runs = detect_black(sc)
    black_mask = np.zeros(n, bool)
    for a, b in black_runs:
        black_mask[a:b] = True
    flash_frames = [int(np.searchsorted(sc.t, f["start"])) for f in flashes]
    changes = detect_changes(sc, cuts, flash_frames)
    boundaries = merge_boundaries(sc, cuts, changes, black_mask)
    shots = build_shots(sc, boundaries, black_mask)
    kinds = material_transitions(shots)
    eff = None if sample_fps else effective_fps(sc, cuts)
    regions = dynamic_regions(sc.motion_map)
    pal = palette(sc.samples)
    col = color_stats(sc)
    range_end = float(sc.t[-1]) + 1.0 / sc.fps
    span = range_end - start

    audio, db = None, None
    if info["audio"]:
        log("  音声を計測中…")
        db = scan_audio(video, start, duration)
        summary = audio_summary(db)  # 音声を1ブロックも読めなければ音声なし扱い
        audio = {**measure_loudness(video, start, duration), **summary} if summary else None

    log("  図を作成中…")
    draw_timeline(sc, boundaries, db, start, outdir / "timeline.png")
    write_shots_csv(shots, outdir / "shots.csv")
    draw_palette(pal, outdir / "palette.png")
    draw_layout(sc, regions, outdir)
    images = {"timeline": "timeline.png", "palette": "palette.png", "shots_csv": "shots.csv",
              "layout_motion": "layout_motion.png", "layout_static": "layout_static.png"}
    thumb = next((video.with_suffix(e) for e in THUMB_EXTS if video.with_suffix(e).exists()), None)
    if thumb:
        images["thumbnail"] = Path(os.path.relpath(thumb, outdir)).as_posix()
    if sheets:
        for old in list(outdir.glob("sheet_shots_*.jpg")) + list(outdir.glob("sheet_timeline_*.jpg")):
            old.unlink()
        real = [s for s in shots if not s["black"]]
        if len(real) >= 2:
            pick = real if len(real) <= 40 else [real[round(i * (len(real) - 1) / 39)] for i in range(40)]
            wide = vi["width"] >= vi["height"]
            items = [((s["start"] + s["end"]) / 2, f"#{s['index']}  {fmt_time(s['start'])}  ({s['duration']:.1f}s)"
                      + (f"  {txt(TRANSITION_JA[s['transition_in']], s['transition_in'])}" if wide else ""))
                     for s in pick]
            images["sheet_shots"] = [p.name for p in contact_sheets(
                video, items, outdir / "sheet_shots", vi["width"], vi["height"])]
        items = [(start + (i + 0.5) * span / 20, fmt_time(start + (i + 0.5) * span / 20)) for i in range(20)]
        images["sheet_timeline"] = [p.name for p in contact_sheets(
            video, items, outdir / "sheet_timeline", vi["width"], vi["height"])]

    real = [s for s in shots if not s["black"]]
    real_d = np.array([s["duration"] for s in real])
    tpm = len(kinds) / (span / 60) if span > 0 else 0.0
    if len(real) >= 3:  # 素材ごとの色味のそろい方(小さいほど統一感がある)
        col["shot_brightness_sd"] = round(float(np.std([s["brightness"] for s in real])), 1)
        col["shot_saturation_sd"] = round(float(np.std([s["saturation"] for s in real])), 3)
    motions = sorted((c for c in changes if c["kind"] == "motion"), key=lambda c: -c["peak"])[:12]
    report = {
        "version": REPORT_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "video_path": str(video),
        "source": load_source_meta(video),
        "file": info,
        "range": {"start": round(start, 3), "end": round(range_end, 3), "partial": partial},
        "analysis": {"width": sc.size[0], "height": sc.size[1], "frames": n, "fps_used": sc.fps,
                     "sample_fps": sample_fps},
        "video": {
            "effective_fps": eff,
            "motion_mean": round(float(sc.diff[1:].mean()), 2) if n > 1 else 0.0,
            "changed_mean": round(float(sc.changed[1:].mean()), 4) if n > 1 else 0.0,
            "static_ratio": round(float((sc.std_map < STATIC_STD).mean()), 3),
            "regions": regions,
        },
        "editing": {
            "transitions": len(kinds), "transitions_per_min": round(tpm, 2), "style": editing_style(tpm),
            "cuts": kinds.count("cut"), "dissolves": kinds.count("dissolve"), "fades": kinds.count("fade"),
            "dissolve_ratio": round((kinds.count("dissolve") + kinds.count("fade")) / len(kinds), 3) if kinds else None,
            "shots": int(len(real_d)),
            "shot_hist": {label: int(((real_d >= lo) & (real_d < hi)).sum())
                          for label, lo, hi in (("〜2秒", 0, 2), ("2〜4秒", 2, 4), ("4〜8秒", 4, 8),
                                                ("8秒〜", 8, float("inf")))},
            "shot_median": _pct(real_d, 50), "shot_mean": float(real_d.mean()) if len(real_d) else None,
            "shot_p10": _pct(real_d, 10), "shot_p90": _pct(real_d, 90),
            "black_segments": [{"start": float(sc.t[a]), "duration": round((b - a) / sc.fps, 2)}
                               for a, b in black_runs],
            "flashes": flashes,
            "change_segments": sorted(motions, key=lambda c: c["start"]),
            "transition_list": [{"at": round(float(sc.t[f]), 3), "kind": k} for f, k in boundaries],
            "cut_times": [round(float(sc.t[f]), 3) for f, k in boundaries if k == "cut"],
            "shot_list": shots[:1000],
        },
        "color": {**col, "palette": pal},
        "audio": audio,
        "images": images,
    }
    (outdir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    report["outdir"] = str(outdir)
    return report


# ---------------------------------------------------------------- レポート(Markdown)

def _f(x, fmt="{:.1f}", unit=""):
    return "—" if x is None else fmt.format(x) + unit


def _db(v):
    return "-90未満" if v < -90 else f"{v:.0f}"


def _brightness_ja(v):
    return "暗め" if v < 85 else ("明るめ" if v > 170 else "標準的")


def _saturation_ja(v):
    return "落ち着いた色" if v < 0.2 else ("鮮やか" if v > 0.45 else "標準的")


def _colorfulness_ja(v):
    # Hasler & Süsstrunk の区分: 15 わずか / 33 控えめ / 45 中くらい / 59 かなり / 82 非常に
    for lim, label in ((15, "ほぼ無彩色"), (33, "わずかに色がある"), (45, "控えめ"), (59, "中くらい"),
                       (82, "カラフル"), (109, "とてもカラフル")):
        if v < lim:
            return label
    return "極めてカラフル"


def _frames_cmd(video_arg, start, dur):
    return f"`python scripts/analyze_video.py frames {video_arg} --start {start:.2f} --duration {dur:.2f}`"


def render_markdown(rep) -> str:
    src, info, v, ed, col, aud = (rep.get("source"), rep["file"], rep["video"], rep["editing"],
                                  rep["color"], rep.get("audio"))
    vi, ai = info["video"], info.get("audio")
    rg, an, img = rep["range"], rep["analysis"], rep["images"]
    video_arg = Path(rep["video_path"]).as_posix()
    eff = (v.get("effective_fps") or {}).get("fps")
    L = [f"# 参考動画 分析レポート: {(src or {}).get('title') or info['file']}", ""]
    if src:
        L += [f"- チャンネル: {src.get('channel') or '—'}",
              f"- URL: {src.get('url') or '—'}",
              f"- 公開日: {src.get('upload_date') or '—'} ／ 種類: {LIVE_JA.get(src.get('live_status'), '—')}"]
        if src.get("section_start") is not None:
            L.append(f"- 取得範囲: 元動画の {fmt_time(src['section_start'], 0)}〜"
                     f"{fmt_time(src.get('section_end') or 0, 0)} を切り出したもの（以下の時刻は切り出し後の時刻）")
    L += [f"- 分析区間: {fmt_time(rg['start'], 0)}〜{fmt_time(rg['end'], 0)}"
          f"（{'一部' if rg['partial'] else '全体'}・{an['frames']}フレーム"
          f"{'・' + format(an['sample_fps'], 'g') + 'fpsに間引き' if an['sample_fps'] else ''}）",
          f"- 作成: {rep['generated_at']}（scripts/analyze_video.py）", ""]
    if img.get("thumbnail"):
        L += [f"![サムネイル]({img['thumbnail']})", ""]

    orient = "横長" if vi["width"] >= vi["height"] else "縦長"
    fps_note = (f"動く場面で実際に絵が更新される頻度は **約{eff:g}fps**" if eff
                else "動きが少なく実効fpsは判定できず" if not an["sample_fps"] else "間引き分析のため実効fpsは未計測")
    from_yt = bool(src)
    L += ["## 1. 基本スペック", "", "| 項目 | 値 | メモ |", "|---|---|---|",
          f"| 解像度 | {vi['width']}x{vi['height']}（{orient}） | |",
          f"| フレームレート | {vi['fps']:g}fps | {fps_note} |",
          f"| 映像 | {vi['codec']} / {_f(vi.get('kbps') or info.get('overall_kbps'), '{:,.0f}', ' kbps')} | "
          + ("YouTube再エンコード後の値。配信・アップロード設定の目安にはしない |" if from_yt else "録画ファイルの値 |")]
    if ai:
        L.append(f"| 音声 | {ai['codec']} / {ai['sample_rate']} Hz / {ai['channels']}ch / "
                 f"{_f(ai.get('kbps'), '{:,.0f}', ' kbps')} | |")
    if aud:
        L.append(f"| ラウドネス | {_f(aud.get('lufs'), '{:.1f}', ' LUFS')}（レンジ {_f(aud.get('lra'), '{:.1f}', ' LU')}・"
                 f"ピーク {_f(aud.get('true_peak'), '{:.1f}', ' dBFS')}） | 聴感上の音量。YouTubeは約-14 LUFSを目安に"
                 "大きすぎる動画の再生音量を下げる |")
    L.append("")

    L += ["## 2. 編集テンポ（素材・場面の切り替え）", "", "| 項目 | 値 |", "|---|---|",
          f"| 切り替え | {ed['transitions']}回（1分あたり **{ed['transitions_per_min']:.1f}回**）→ {ed['style']} |",
          f"| つなぎ方の内訳 | カット {ed['cuts']}回 ／ クロスフェード {ed['dissolves']}回 ／ 暗転 {ed['fades']}回 |",
          f"| ショット（1素材の表示時間） | 中央値 {_f(ed['shot_median'], '{:.1f}', '秒')}（短い方10%: "
          f"{_f(ed['shot_p10'], '{:.1f}', '秒')} ／ 長い方10%: {_f(ed['shot_p90'], '{:.1f}', '秒')}） |",
          "| ショット長の分布 | " + " ／ ".join(f"{k}: {v}本" for k, v in ed["shot_hist"].items()) + " |",
          f"| 黒い画面 | {len(ed['black_segments'])}回 |",
          f"| フラッシュ（一瞬光る演出） | {len(ed['flashes'])}回 |", "",
          "ショットごとの長さ・入り方・明るさ・動きは `shots.csv`（Excelで開ける）。"
          "スライド・ワイプ・ズームで入れ替わるつなぎは切り替えに数えず、7章の「大きな変化」に出る。", ""]

    L += ["## 3. 画面レイアウトと動き", "",
          "![動きの分布](layout_motion.png)", "",
          "青いほどよく動く場所。枠と番号は下の「動く領域」。", "",
          "![固定部分](layout_static.png)", "",
          "動画全体でほぼ変化しない部分だけを残した画（市松模様の所は動く）。背景・枠・ロゴなどの「固定レイヤー」にあたる。", "",
          "| 項目 | 値 | メモ |", "|---|---|---|",
          f"| 固定されている画面の割合 | {v['static_ratio'] * 100:.0f}% | カットが多い動画では小さく出る |",
          f"| 平均の変化量 | {v['motion_mean']:.2f}（動いた画素 {v['changed_mean'] * 100:.1f}%/フレーム） | 自分の動画との比較用 |"]
    for k, r in enumerate(v["regions"], 1):
        L.append(f"| 動く領域{k} | {r['where']}（横{r['x0']:.0f}〜{r['x1']:.0f}%・縦{r['y0']:.0f}〜{r['y1']:.0f}%、"
                 f"画面の{r['area']:.0f}%） | 動き全体の{r['share'] * 100:.0f}%・領域内の動き量 {r['motion']:.2f} |")
    L.append("")

    L += ["## 4. 色", "", "![主要色](palette.png)", "", "| 項目 | 値 |", "|---|---|",
          "| 主要色 | " + " ".join(f"`{p['hex']}` {p['share'] * 100:.0f}%" for p in col["palette"][:6]) + " |",
          f"| 明るさ | {col['brightness']:.0f} / 255（{_brightness_ja(col['brightness'])}） |",
          f"| コントラスト | {col['contrast']:.0f}（明るさのばらつき） |",
          f"| 彩度 | {col['saturation']:.2f}（{_saturation_ja(col['saturation'])}） |",
          f"| カラフルさ | {col['colorfulness']:.0f}（{_colorfulness_ja(col['colorfulness'])}） |"]
    if col.get("shot_brightness_sd") is not None:
        L.append(f"| 素材ごとの色のばらつき | 明るさ ±{col['shot_brightness_sd']:.0f} ／ 彩度 ±{col['shot_saturation_sd']:.2f}"
                 "（小さいほど素材どうしの色味がそろっている） |")
    L.append("")

    if aud:
        L += ["## 5. 音", "", "| 項目 | 値 |", "|---|---|",
              f"| 無音（{QUIET_DB:.0f} dBFS未満）の割合 | {aud['quiet_ratio'] * 100:.0f}% |",
              f"| 0.3秒以上の間 | 1分あたり{_f(aud['pauses_per_min'], '{:.1f}', '回')}"
              f"（長さの中央値 {_f(aud['pause_median_sec'], '{:.2f}', '秒')}） |",
              f"| 音量の分布 | 小さい所 {_db(aud['floor_db'])} / 中央 {_db(aud['median_db'])} / "
              f"大きい所 {_db(aud['loud_db'])} dBFS |",
              f"| BGMの推定 | {aud['bgm_hint']} |", ""]

    L += ["## 6. タイムライン", "", "![タイムライン](timeline.png)", ""]

    L += ["## 7. フレーム単位で確認したい区間", "",
          "切り替えの瞬間と、切り替え以外で画が大きく変わった所（ズーム・パン・スライド・演出・大きな動きの候補）。"
          "右のコマンドで該当区間の全フレームを書き出して1コマずつ確認する（つなぎの長さ・加工の有無が分かる）。", "",
          "| 時刻 | 種類 | 確認コマンド |", "|---|---|---|"]
    rows = [(c["start"], f"大きな変化（強さ{c['peak']:.0f}）", c["start"] - 0.2,
             min(3.0, c["end"] - c["start"] + 0.4)) for c in ed["change_segments"]]
    rows += [(f["start"], "フラッシュ", f["start"] - 0.2, 0.6) for f in ed["flashes"][:5]]
    rows += [(b["start"], f"黒画面（{b['duration']:.2f}秒）", b["start"] - 0.5, min(3.0, b["duration"] + 1.0))
             for b in ed["black_segments"][:5]]
    rows += [(t, "カット", t - 0.3, 0.6) for t in ed["cut_times"][:3]]
    for kind, label in (("dissolve", "クロスフェード"), ("fade", "暗転")):
        rows += [(x["at"], label, x["at"] - 0.8, 1.6) for x in ed["transition_list"] if x["kind"] == kind][:4]
    for t, kind, s, dur in sorted(rows):
        L.append(f"| {fmt_time(t)} | {kind} | {_frames_cmd(video_arg, max(0.0, s), dur)} |")
    if not rows:
        L.append("| — | 該当なし | |")
    L.append("")

    L += ["## 8. 代表フレーム", ""]
    for name in img.get("sheet_shots", []):
        L += [f"![ショット一覧]({name})", ""]
    for name in img.get("sheet_timeline", []):
        L += [f"![等間隔20枚]({name})", ""]
    L += ["`sheet_shots_*.jpg` はショットごとの中間フレーム、`sheet_timeline_*.jpg` は区間を20等分した時点のフレーム。", ""]

    L += ["## 9. 目視レビュー（数値では測れない所。代表フレームと frames の書き出しを見て埋める）", "",
          "- [ ] テロップ・字幕: フォントの系統 / 縁取り・影 / 色 / 位置 / 出し方（何フレームで出るか）",
          "- [ ] キャラ: 画面内の大きさと位置 / 表情の種類と切り替え頻度 / まばたき・口パク・体の揺れの大きさ",
          "- [ ] 背景: 静止画か動くか / キャラとの色の関係",
          "- [ ] 画面の飾り: 枠・ロゴ・コメント欄・話題表示などの有無と位置",
          "- [ ] 音: BGMの有無と音量バランス / 効果音の頻度 / 声のトーン・速さ",
          "- [ ] サムネイル: 文字の量と大きさ / 配色 / キャラの表情", ""]

    L += ["## 10. 同じクオリティにするための目標値（この動画に合わせる場合）", "",
          "| 項目 | 目標 | どこで合わせるか |", "|---|---|---|",
          f"| 解像度・fps | {vi['width']}x{vi['height']} / {vi['fps']:.0f}fps"
          + (f"（実効 約{eff:.0f}fps）" if eff else "") + " | OBS 設定→映像（基本・出力解像度、FPS） |"]
    if aud and aud.get("lufs") is not None:
        L.append(f"| 音量 | {aud['lufs']:.0f} LUFS 前後・ピーク -1 dBFS 以下 | OBSの音声フィルタ（ゲイン・リミッター）。"
                 "録画して analyze → compare で確認 |")
        L.append(f"| 間の取り方 | 無音 {aud['quiet_ratio'] * 100:.0f}% | 応答の間隔・BGMの有無 |")
    if v["regions"]:
        r = v["regions"][0]
        L.append(f"| キャラ（主な動く領域）の配置 | {r['where']}・画面の{r['area']:.0f}% | OBSでキャラのソースを配置・拡大縮小 |")
    L.append("| 配色 | " + " ".join(f"`{p['hex']}`" for p in col["palette"][:4]) + " | 背景・枠・字幕の色 |")
    L.append(f"| 編集テンポ | 1分あたり{ed['transitions_per_min']:.1f}回の切り替え（{ed['style']}） | "
             + ("配信型なら不要" if ed["transitions_per_min"] < 0.5 else "編集工程が必要（docs/06 参照）") + " |")
    if ed["transitions_per_min"] >= 0.5:
        n_tr = max(1, ed["transitions"])
        L.append(f"| 素材1本の表示時間 | 中央値 {_f(ed['shot_median'], '{:.1f}', '秒')}"
                 f"（{_f(ed['shot_p10'], '{:.1f}')}〜{_f(ed['shot_p90'], '{:.1f}', '秒')}） | 素材を切り出す長さ |")
        L.append(f"| つなぎ方 | カット {ed['cuts'] / n_tr:.0%} ／ クロスフェード {ed['dissolves'] / n_tr:.0%}"
                 f" ／ 暗転 {ed['fades'] / n_tr:.0%} | トランジションの種類と比率 |")
    L.append("")
    if src and (src.get("description") or src.get("tags")):
        L += ["## 付録: 動画の情報", ""]
        if src.get("chapters"):
            L += ["チャプター: " + " / ".join(f"{fmt_time(c['start'] or 0, 0)} {c['title']}" for c in src["chapters"]), ""]
        if src.get("tags"):
            L += ["タグ: " + ", ".join(src["tags"]), ""]
        if src.get("description"):
            L += ["概要欄（先頭）:", ""] + ["> " + line for line in src["description"].splitlines()] + [""]
    return "\n".join(L)


# ---------------------------------------------------------------- frames

def extract_frames(video, start, duration=None, count=None, width=640, outdir=None, verbose=True) -> dict:
    require_tools("ffmpeg", "ffprobe")
    video = Path(video)
    info = probe(video)
    fps = info["video"]["fps"]
    if count:
        duration = (count + 1) / fps
    duration = duration or 1.0
    n_est = count or int(math.ceil(duration * fps))
    if n_est > MAX_FRAME_DUMP:
        raise SystemExit(f"[error] 約{n_est}フレームは多すぎます（上限{MAX_FRAME_DUMP}）。--duration を短くしてください")
    outdir = Path(outdir) if outdir else video.parent / "frames" / f"t{start:.2f}s"
    outdir.mkdir(parents=True, exist_ok=True)
    for old in list(outdir.glob("f_*.png")) + list(outdir.glob("filmstrip_*.jpg")):
        old.unlink()
    w = min(width, info["video"]["width"])
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", *_range_args(start, duration),
           "-i", str(video), "-map", "0:v:0", "-vf", f"scale={w}:-2,showinfo", "-fps_mode", "passthrough"]
    if count:
        cmd += ["-frames:v", str(count)]
    res = subprocess.run(cmd + [str(outdir / "f_%04d.png")], capture_output=True)
    err = res.stderr.decode("utf-8", "replace")
    files = sorted(outdir.glob("f_*.png"))
    if res.returncode != 0 or not files:
        raise SystemExit("[error] フレームを書き出せませんでした:\n" + "\n".join(err.splitlines()[-8:]))
    pts = [float(m) for m in PTS_RE.findall(err)]
    tile_w, cols, per = (320, 5, 30) if info["video"]["width"] >= info["video"]["height"] else (180, 8, 32)
    meta, tiles, prev = [], [], None
    for i, f in enumerate(files):
        t = start + (pts[i] if i < len(pts) else i / fps)
        im = Image.open(f).convert("RGB")
        small = np.asarray(im.resize((160, max(2, round(160 * im.height / im.width)))), np.int16)
        delta = None if prev is None else float(np.abs(small - prev).mean())
        prev = small
        meta.append({"file": f.name, "frame": i + 1, "t": round(t, 4),
                     "delta": None if delta is None else round(delta, 2)})
        mark = "" if delta is None else ("  =" if delta < 0.5 else f"  Δ{delta:.1f}")
        tiles.append((im.resize((tile_w, round(tile_w * im.height / im.width)), Image.LANCZOS),
                      f"{i + 1:03d}  {fmt_time(t, 3)}{mark}"))
    sheets = render_sheets(tiles, outdir / "filmstrip", cols, per, tile_w)
    result = {"video": str(video), "start": start, "fps": fps, "frames": meta,
              "filmstrips": [p.name for p in sheets]}
    (outdir / "frames.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["outdir"] = str(outdir)
    return result


# ---------------------------------------------------------------- compare

def _get(d, path):
    for k in path.split("."):
        if isinstance(d, list):
            d = d[int(k)] if k.isdigit() and int(k) < len(d) else None
        elif isinstance(d, dict):
            d = d.get(k)
        else:
            return None
    return d


COMPARE_ROWS = [
    # (表示名, report.jsonのキー, 書式)
    ("fps", "file.video.fps", "{:.0f}"),
    ("実効fps（動く場面）", "video.effective_fps.fps", "{:.0f}"),
    ("ラウドネス (LUFS)", "audio.lufs", "{:.1f}"),
    ("トゥルーピーク (dBFS)", "audio.true_peak", "{:.1f}"),
    ("ラウドネスレンジ (LU)", "audio.lra", "{:.1f}"),
    ("無音の割合", "audio.quiet_ratio", "{:.0%}"),
    ("切り替え/分", "editing.transitions_per_min", "{:.1f}"),
    ("クロスフェード・暗転の割合", "editing.dissolve_ratio", "{:.0%}"),
    ("ショット長 中央値 (秒)", "editing.shot_median", "{:.1f}"),
    ("明るさ (0-255)", "color.brightness", "{:.0f}"),
    ("コントラスト", "color.contrast", "{:.0f}"),
    ("彩度 (0-1)", "color.saturation", "{:.2f}"),
    ("カラフルさ", "color.colorfulness", "{:.0f}"),
    ("素材ごとの明るさのばらつき", "color.shot_brightness_sd", "{:.0f}"),
    ("固定画面の割合", "video.static_ratio", "{:.0%}"),
    ("主な動く領域の面積 (%)", "video.regions.0.area", "{:.0f}"),
    ("主な動く領域の動き量", "video.regions.0.motion", "{:.2f}"),
]


def _load_report(p):
    p = Path(p)
    if p.is_dir():
        p = p / "report.json"
    return json.loads(p.read_text(encoding="utf-8"))


def compare_reports(ref, mine) -> str:
    L = ["# 参考動画との比較", "",
         f"- 参考: {(ref.get('source') or {}).get('title') or ref['file']['file']}",
         f"- 自分: {(mine.get('source') or {}).get('title') or mine['file']['file']}", "",
         "| 項目 | 参考 | 自分 | 差（自分−参考） |", "|---|---|---|---|"]
    rv, mv = ref["file"]["video"], mine["file"]["video"]
    L.append(f"| 解像度 | {rv['width']}x{rv['height']} | {mv['width']}x{mv['height']} | |")
    for label, key, fmt in COMPARE_ROWS:
        a, b = _get(ref, key), _get(mine, key)
        diff = "" if a is None or b is None else ("+" if b - a >= 0 else "−") + fmt.format(abs(b - a))
        L.append(f"| {label} | {_f(a, fmt)} | {_f(b, fmt)} | {diff} |")
    tips = []
    if mv["height"] < rv["height"]:
        tips.append(f"解像度が低い → OBSの出力解像度を {rv['width']}x{rv['height']} に")
    if mv["fps"] + 1 < rv["fps"]:
        tips.append(f"fpsが低い → OBS 設定→映像→FPS を {rv['fps']:.0f} に")
    ea, eb = _get(ref, "video.effective_fps.fps"), _get(mine, "video.effective_fps.fps")
    if ea and eb and eb < ea * 0.8:
        tips.append("実効fpsが低く動きがカクつく → キャプチャ元（ブラウザ）の描画fps・PC負荷を確認")
    la, lb = _get(ref, "audio.lufs"), _get(mine, "audio.lufs")
    if la is not None and lb is not None and abs(la - lb) >= 1.5:
        tips.append(f"音量が参考より{abs(la - lb):.1f} LU{'小さい' if lb < la else '大きい'} → "
                    f"OBSの音声フィルタ「ゲイン」で約{la - lb:+.0f} dB（上げる場合はリミッターも入れる）")
    tp = _get(mine, "audio.true_peak")
    if tp is not None and tp > -1.0:
        tips.append("ピークが-1 dBFSを超えている → OBSの音声フィルタ「リミッター」（しきい値 -1 dB 程度）で音割れ防止")
    qa, qb = _get(ref, "audio.quiet_ratio"), _get(mine, "audio.quiet_ratio")
    if qa is not None and qb is not None and abs(qa - qb) >= 0.15:
        tips.append(f"無音の割合が{'多い' if qb > qa else '少ない'} → 応答の間隔やBGMの有無を見直す")
    ca, cb = _get(ref, "editing.transitions_per_min"), _get(mine, "editing.transitions_per_min")
    if ca is not None and cb is not None and ca >= 0.5 and (cb < ca * 0.5 or cb > ca * 2):
        sm = _get(ref, "editing.shot_median")
        tips.append(f"編集テンポが違う（参考は1分あたり{ca:.1f}回の切り替え・素材1本 中央値{_f(sm, '{:.1f}', '秒')}）")
    ba, bb = _get(ref, "color.brightness"), _get(mine, "color.brightness")
    if ba is not None and bb is not None and abs(ba - bb) >= 15:
        tips.append(f"画面が参考より{'暗い' if bb < ba else '明るい'} → 背景画像・配色を調整")
    sa, sb = _get(ref, "color.saturation"), _get(mine, "color.saturation")
    if sa is not None and sb is not None and abs(sa - sb) >= 0.08:
        tips.append(f"色が参考より{'くすんでいる' if sb < sa else '鮮やか'} → 背景・枠の彩度を調整")
    va, vb = _get(ref, "color.shot_brightness_sd"), _get(mine, "color.shot_brightness_sd")
    if va is not None and vb is not None and vb > va * 1.5 + 5:
        tips.append("素材ごとの明るさがばらついている → 素材選びをそろえるか、色調整で明るさ・色味を統一")
    ma, mb = _get(ref, "video.regions.0.motion"), _get(mine, "video.regions.0.motion")
    if ma and mb and mb < ma * 0.6:
        tips.append("キャラの動きが参考より小さい → アイドルモーション・物理演算（揺れ）を強める（docs/03）")
    L += ["", "## 調整の目安", ""] + ([f"- {t}" for t in tips] or ["- 大きな差はありません"])
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- fetch

def fetch(url, work=None, section=None, max_height=1080, subs=False) -> Path:
    try:
        import yt_dlp
        from yt_dlp.utils import download_range_func
    except ImportError:
        raise SystemExit('[error] yt-dlp が入っていません: pip install -U "yt-dlp[default,deno]"')
    require_tools("ffmpeg", "ffprobe")
    work = Path(work) if work else DEFAULT_WORK
    opts = {
        "format": "bv*+ba/b",
        "format_sort": [f"res:{max_height}", "fps", "vcodec:h264", "acodec:aac"],
        "merge_output_format": "mp4",
        "outtmpl": {"default": str(work / "%(id)s" / "source.%(ext)s")},
        "writeinfojson": True,
        "writethumbnail": True,
        "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}],
        "noplaylist": True,
    }
    if section:
        a, b = section.split("-", 1)
        opts["download_ranges"] = download_range_func(None, [(parse_time(a), parse_time(b))])
        opts["force_keyframes_at_cuts"] = True
    if subs:
        opts.update({"writesubtitles": True, "writeautomaticsub": True,
                     "subtitleslangs": ["ja"], "subtitlesformat": "vtt"})
    with yt_dlp.YoutubeDL(opts) as ydl:
        meta = ydl.extract_info(url, download=True)
    folder = work / meta["id"]
    video = next((p for p in sorted(folder.glob("source.*")) if p.suffix in VIDEO_EXTS), None)
    if video is None:
        raise SystemExit(f"[error] 動画ファイルが見つかりません: {folder}")
    return video


# ---------------------------------------------------------------- CLI

def cmd_fetch(args):
    video = fetch(args.url, args.output, args.section, args.max_height, args.subs)
    meta = load_source_meta(video) or {}
    print(f"取得完了: {video}")
    print(f"  {meta.get('title')} ／ {meta.get('channel')}")
    print(f"次: python scripts/analyze_video.py analyze {video.as_posix()}")
    return 0


def cmd_analyze(args):
    rep = analyze(args.video, args.output, parse_time(args.start) if args.start else 0.0,
                  parse_time(args.duration) if args.duration else None, args.sample_fps, not args.no_sheets)
    v, ed, aud = rep["video"], rep["editing"], rep.get("audio") or {}
    eff = (v.get("effective_fps") or {}).get("fps")
    fv = rep["file"]["video"]
    print(f"完了: {Path(rep['outdir']) / 'report.md'}")
    print(f"  映像  {fv['width']}x{fv['height']} {fv['fps']:g}fps" + (f"（実効 約{eff:g}fps）" if eff else ""))
    print(f"  編集  1分あたり{ed['transitions_per_min']:.1f}回の切り替え（カット{ed['cuts']}・クロスフェード"
          f"{ed['dissolves']}・暗転{ed['fades']}）→ {ed['style']}")
    if aud:
        print(f"  音    {_f(aud.get('lufs'), '{:.1f}', ' LUFS')} ／ 無音 {aud['quiet_ratio'] * 100:.0f}%")
    if v["regions"]:
        r = v["regions"][0]
        print(f"  配置  主な動く領域: {r['where']}（画面の{r['area']:.0f}%）")
    return 0


def cmd_frames(args):
    res = extract_frames(args.video, parse_time(args.start),
                         parse_time(args.duration) if args.duration else None, args.count, args.width, args.output)
    print(f"{len(res['frames'])}フレーム書き出し: {res['outdir']}")
    print(f"  一覧: {', '.join(res['filmstrips'])}（Δ=前フレームとの差、= は前と同じ絵）")
    return 0


def cmd_compare(args):
    text = compare_reports(_load_report(args.reference), _load_report(args.mine))
    print(text)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="参考動画をダウンロード (yt-dlp)")
    p.add_argument("url")
    p.add_argument("-o", "--output", help="保存先の親フォルダ（既定: refs/）")
    p.add_argument("--section", help="一部だけ取得 例: 00:10:00-00:20:00（長い配信アーカイブ向け）")
    p.add_argument("--max-height", type=int, default=1080, help="解像度の上限（既定1080）")
    p.add_argument("--subs", action="store_true", help="日本語字幕（自動字幕含む）も取得")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("analyze", help="フレーム単位の分析レポートを作る")
    p.add_argument("video")
    p.add_argument("-o", "--output", help="出力フォルダ（既定: <動画名>_report/）")
    p.add_argument("--start", help="分析の開始位置 例: 600 / 10:00")
    p.add_argument("--duration", help="分析する長さ 例: 300 / 5:00")
    p.add_argument("--sample-fps", type=float, help="間引いて分析（長時間動画の時短用。実効fpsは測らない）")
    p.add_argument("--no-sheets", action="store_true", help="代表フレーム一覧を作らない")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("frames", help="指定区間の全フレームを書き出す")
    p.add_argument("video")
    p.add_argument("--start", required=True, help="開始位置 例: 83.2 / 1:23.2")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--duration", help="長さ（既定1秒）")
    g.add_argument("--count", type=int, help="フレーム数")
    p.add_argument("--width", type=int, default=640, help="書き出す幅（既定640px）")
    p.add_argument("-o", "--output", help="出力フォルダ（既定: <動画のフォルダ>/frames/t<開始秒>s/）")
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("compare", help="参考と自分の report.json（またはレポートフォルダ）を比較")
    p.add_argument("reference")
    p.add_argument("mine")
    p.add_argument("-o", "--output", help="比較結果をMarkdownで保存")
    p.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
