#!/usr/bin/env python3
"""動画の編集をフレーム単位で解析し、「同じ編集」を再現するための数値(編集レシピ)を取り出す。

YouTubeのURLかローカルの動画ファイルを受け取り、output/video_analysis/<ID>/ に書き出す:
  report.md      日本語の解析レポート(編集レシピのまとめ)
  recipe.json    再現用の数値(カット間隔・ズーム倍率・テロップの位置/大きさ/色/出方・音量など)
  analysis.json  検出結果の全データ(カット・トランジション・テロップ・ズーム・音声)
  frames.csv     全フレームの特徴量(1行=1フレーム)
  sheets/        目視確認用のコンタクトシート(ショット一覧・テロップ一覧・スタイル見本)
  frames/        代表フレーム画像
  timeline_*.png 時間軸で見た編集の密度(カット・ズーム・テロップ・音声)

元動画や切り出した画像の著作権は元の制作者にある。解析結果は手元での研究用にとどめ、
再配布や素材としての流用はしないこと(docs/06参照)。GPU不要。

usage:
  python scripts/analyze_video.py https://youtu.be/XXXXXXXXXXX
  python scripts/analyze_video.py input.mp4 -o output/video_analysis/mine
  python scripts/analyze_video.py URL --download-only
  python scripts/analyze_video.py URL --cookies-from-browser firefox   # YouTubeにbot判定された時
"""

import argparse
import math
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import video_audio  # noqa: E402
import video_io  # noqa: E402
import video_overlay as vo  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "output" / "video_analysis"


class Params:
    long_side = 640        # 全フレーム走査の解像度(長辺px)
    review_side = 1280     # 目視確認用に保存する画像の長辺px
    t1_abs = 6.0           # 1フレーム差分(セル平均, 0-255)がこれ未満なら変化とみなさない
    r1 = 4.0               # そのセルの普段の動きの何倍を「急変」とみなすか
    tK_abs = 10.0          # 約0.2秒差分のしきい値(ゆっくり出るテロップ・トランジション用)
    rK = 3.0
    tex_min = 3.0          # セル内の輝度の標準偏差がこれ以上なら「中身のあるセル」
    cut_frac = 0.55        # 中身のあるセルのうち何割が急変したらカットとみなすか
    max_saved_frames = 800    # 目視確認用に保存する画像の上限
    hires_side = 1920         # テロップの色・縁を測り直す解像度(長辺。元動画より大きくはしない)
    max_hires_events = 600


class Features:
    """全フレーム走査の結果(1フレーム1要素の配列)"""


LK_PARAMS = dict(winSize=(21, 21), maxLevel=3,
                 criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03))
K3 = np.ones((3, 3), np.uint8)


# ---------------------------------------------------------------- 全フレーム走査

def estimate_motion(prev, cur):
    """前フレーム→現フレームのカメラの動き(相似変換 2x3)と、それに従う特徴点の割合。
    動いている点が画面の広い範囲にある時は、止まっている点(テロップ・ロゴ・アバター等の
    上に重なった物)を除いて測る。混ぜるとスクロールが「ズーム」に見えたり、ズームが小さく出る"""
    h, w = prev.shape
    pts = cv2.goodFeaturesToTrack(prev, maxCorners=300, qualityLevel=0.01, minDistance=7, blockSize=7)
    if pts is None or len(pts) < 12:
        return None
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev, cur, pts, None, **LK_PARAMS)
    back, st2, _ = cv2.calcOpticalFlowPyrLK(cur, prev, nxt, None, **LK_PARAMS)
    p0, p1 = pts.reshape(-1, 2), nxt.reshape(-1, 2)
    fb = np.linalg.norm(p0 - back.reshape(-1, 2), axis=1)
    good = (st.ravel() == 1) & (st2.ravel() == 1) & (fb < 1.0)
    if good.sum() < 10:
        return None
    p0, p1 = p0[good], p1[good]
    moving = np.linalg.norm(p1 - p0, axis=1) > 0.25
    use = np.ones(len(p0), bool)
    if moving.sum() >= 12 and moving.mean() >= 0.25:
        pm = p0[moving]
        cells = set(zip((pm[:, 0] * 4 // w).astype(int).tolist(), (pm[:, 1] * 4 // h).astype(int).tolist()))
        if len(cells) >= 8:
            use = moving
    A, inl = cv2.estimateAffinePartial2D(p0[use], p1[use], method=cv2.RANSAC, ransacReprojThreshold=0.7,
                                         maxIters=500, confidence=0.99)
    if A is None:
        return None
    return A, float(inl.mean())


def similarity_params(A, w, h):
    """相似変換 → (倍率, 回転deg, 画面中心の移動量dx, dy[画面幅比])"""
    s = math.hypot(A[0, 0], A[1, 0])
    rot = math.degrees(math.atan2(A[1, 0], A[0, 0]))
    cx, cy = w / 2, h / 2
    nx = A[0, 0] * cx + A[0, 1] * cy + A[0, 2]
    ny = A[1, 0] * cx + A[1, 1] * cy + A[1, 2]
    return s, rot, (nx - cx) / w, (ny - cy) / w


def to_small_coords(A_mid, mid, small):
    """中解像度で推定した変換(2x3)を小解像度の3x3行列に直す"""
    S = np.diag([small[0] / mid[0], small[1] / mid[1], 1.0])
    return S @ np.vstack([A_mid, [0, 0, 1]]) @ np.linalg.inv(S)


def compensated_cell_diff(prev, cur, A3, cells, min_valid=0.9):
    """セル毎の「本当に変わった量」。画素ごとに 補正なしの差 と カメラの動きを打ち消した差 の小さい方を取る:
    止まっているテロップ(補正なしで0)も、ズーム・パンする背景(補正後で0)も「変化なし」になる。
    画面外から入ってきて比べられない画素が多いセルは0(=変化なし扱い)"""
    h, w = cur.shape
    warped = cv2.warpAffine(prev, A3[:2], (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    valid = cv2.warpAffine(np.ones((h, w), np.float32), A3[:2], (w, h), flags=cv2.INTER_NEAREST,
                           borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    c = cur.astype(np.float32)
    diff = np.minimum(np.abs(c - warped), np.abs(c - prev.astype(np.float32)))
    v = cells(valid).sum(axis=(1, 3)).ravel()
    d = cells(diff * valid).sum(axis=(1, 3)).ravel() / np.maximum(v, 1)
    d[v < min_valid * (cells(valid).shape[1] * cells(valid).shape[3])] = 0.0
    return d, warped


def motion_ratio(prev, cur, warped):
    """動き補償後の残差/補償前の差(画面中央部)。小さいほど変化をカメラワークで説明できる"""
    h, w = cur.shape
    my, mx = max(1, int(h * 0.1)), max(1, int(w * 0.1))
    c = cur[my:-my, mx:-mx].astype(np.float32)
    raw = float(np.abs(c - prev[my:-my, mx:-mx]).mean())
    mc = float(np.abs(c - warped[my:-my, mx:-mx]).mean())
    return raw, (mc + 0.5) / (raw + 0.5)


def edge_change_ratio(e0, e1):
    n0, n1 = int(e0.sum()), int(e1.sum())
    if n0 + n1 < 50:
        return 0.0
    d0 = cv2.dilate(e0.view(np.uint8), K3) > 0
    d1 = cv2.dilate(e1.view(np.uint8), K3) > 0
    return float(max((e1 & ~d0).sum() / max(n1, 1), (e0 & ~d1).sum() / max(n0, 1)))


def scan_frames(info, P, log=print):
    w, h = info.size_for(P.long_side)
    land = w >= h
    gx, gy = (16, 9) if land else (9, 16)
    cs = 10
    small = (gx * cs, gy * cs)
    mid = (max(16, w // 2), max(16, h // 2))
    tiny = (64, 36) if land else (36, 64)
    fps = float(info.fps)
    K = max(2, int(round(0.2 * fps)))
    C = gx * gy
    cells = lambda img: img.reshape(gy, cs, gx, cs)

    cols = defaultdict(list)
    d1s, dKs, d1Ls, dKLs, texs, tinies = [], [], [], [], [], []
    ring = deque(maxlen=K)      # 直近Kフレームの縮小グレー画像
    tring = deque(maxlen=K)     # 直近Kフレーム分のカメラの動き(3x3)
    I3 = np.eye(3)
    prev_g = prev_u8 = prev_m = prev_hist = prev_edge = None
    t0 = last = time.time()
    total = max(1, info.n_frames)
    for i, rgb in enumerate(video_io.iter_frames(info, (w, h))):
        s_rgb = cv2.resize(rgb, small, interpolation=cv2.INTER_AREA)
        s_gray = cv2.cvtColor(s_rgb, cv2.COLOR_RGB2GRAY)
        m_gray = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), mid, interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(s_rgb, cv2.COLOR_RGB2HSV)
        hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256]).ravel()
        hist /= max(float(hist.sum()), 1.0)
        edge = cv2.Canny(m_gray, 60, 150) > 0
        g = s_gray.astype(np.int16)

        texs.append(cells(s_gray.astype(np.float32)).std(axis=(1, 3)).ravel().astype(np.float16))
        tinies.append(cv2.resize(s_gray, tiny, interpolation=cv2.INTER_AREA))
        cols["luma"].append(float(s_gray.mean()))
        cols["luma_std"].append(float(s_gray.std()))
        cols["dark"].append(float((s_gray < 24).mean()))
        cols["bright"].append(float((s_gray > 232).mean()))
        cols["sat"].append(float(hsv[..., 1].mean()))
        cols["rgb"].append(s_rgb.reshape(-1, 3).mean(axis=0))

        if prev_g is None:
            d1 = np.zeros(C, np.float32)
            hist_d = ecr = raw = 0.0
            mot, mc = None, 1.0
        else:
            d1 = cells(np.abs(g - prev_g).astype(np.float32)).mean(axis=(1, 3)).ravel()
            hist_d = 0.5 * float(np.abs(hist - prev_hist).sum())
            ecr = edge_change_ratio(prev_edge, edge)
            mot = estimate_motion(prev_m, m_gray)
        A3 = I3 if mot is None else to_small_coords(mot[0], mid, small)
        if prev_g is None:
            d1L_cells = d1
        elif mot is None:
            d1L_cells, raw, mc = d1, float(np.abs(g - prev_g).mean()), 1.0
        else:
            d1L_cells, warped = compensated_cell_diff(prev_u8, s_gray, A3, cells)
            raw, mc = motion_ratio(prev_u8, s_gray, warped)
        tring.append(A3)
        if len(ring) == K:
            dK = cells(np.abs(g - ring[0]).astype(np.float32)).mean(axis=(1, 3)).ravel()
            acc = I3
            for T in tring:
                acc = T @ acc
            if np.allclose(acc, I3, atol=1e-4):
                dKL_cells = dK
            else:
                dKL_cells, _ = compensated_cell_diff(ring[0].astype(np.uint8), s_gray, acc, cells)
        else:
            dK = dKL_cells = np.zeros(C, np.float32)
        d1s.append(d1.astype(np.float16))
        dKs.append(dK.astype(np.float16))
        d1Ls.append(d1L_cells.astype(np.float16))
        dKLs.append(dKL_cells.astype(np.float16))
        cols["hist_d"].append(hist_d)
        cols["ecr"].append(ecr)
        cols["raw_mad"].append(raw)
        cols["mc_ratio"].append(mc)
        cols["m_A"].append(np.float32([[1, 0, 0], [0, 1, 0]]) if mot is None else mot[0].astype(np.float32))
        if mot is None:
            cols["m_ok"].append(False)
            for k, v in (("m_scale", 1.0), ("m_rot", 0.0), ("m_dx", 0.0), ("m_dy", 0.0), ("m_inl", 0.0)):
                cols[k].append(v)
        else:
            s, rot, dx, dy = similarity_params(mot[0], *mid)
            cols["m_ok"].append(True)
            cols["m_scale"].append(s)
            cols["m_rot"].append(rot)
            cols["m_dx"].append(dx)
            cols["m_dy"].append(dy)
            cols["m_inl"].append(mot[1])

        ring.append(g)
        prev_g, prev_u8, prev_m, prev_hist, prev_edge = g, s_gray, m_gray, hist, edge
        now = time.time()
        if now - last >= 10:
            last = now
            log(f"  全フレーム走査 {100 * (i + 1) / total:5.1f}% ({i + 1}/{total}フレーム, {now - t0:.0f}秒経過)")

    F = Features()
    F.n = len(tinies)
    if F.n < 2:
        raise RuntimeError("フレームが読めませんでした")
    F.fps, F.K, F.size, F.grid, F.cell, F.mid = fps, K, (w, h), (gx, gy), cs, mid
    for k, v in cols.items():
        F.__dict__[k] = np.array(v)
    F.d1 = np.array(d1s, np.float32)
    F.dK = np.array(dKs, np.float32)
    F.d1L = np.array(d1Ls, np.float32)   # カメラの動きの影響を除いたセル毎の変化量
    F.dKL = np.array(dKLs, np.float32)
    F.tex = np.array(texs, np.float32)
    F.tiny = np.array(tinies, np.uint8)
    tf = F.tiny.astype(np.float32)
    F.tiny_mad = np.concatenate([[0.0], np.abs(tf[1:] - tf[:-1]).mean(axis=(1, 2))])
    log(f"  全フレーム走査 完了: {F.n}フレーム ({time.time() - t0:.0f}秒)")
    return F


# ---------------------------------------------------------------- 構造の検出

def runs(flags):
    d = np.diff(np.concatenate([[0], np.asarray(flags, np.int8), [0]]))
    return list(zip(np.where(d == 1)[0].tolist(), (np.where(d == -1)[0] - 1).tolist()))


def rolling_baseline(x, half, excl, chunk=256):
    """各iの「普段の変化量」: 前側・後ろ側の窓(中心±exclを除く)それぞれの中央値の大きい方。
    フェードのように変化がゆっくり始まる所で、静止側だけに引っ張られて急変扱いしないため。x: (n, c)"""
    x = np.asarray(x, np.float32)
    n = len(x)
    left = np.arange(-half, -excl)
    right = np.arange(excl + 1, half + 1)
    pad = np.pad(x, ((half, half), (0, 0)), mode="edge")
    out = np.empty_like(x)
    for a in range(0, n, chunk):
        b = min(n, a + chunk)
        base = np.arange(a, b)[:, None] + half
        out[a:b] = np.maximum(np.median(pad[base + left[None, :]], axis=1),
                              np.median(pad[base + right[None, :]], axis=1))
    return out


def _corr(a, b):
    a = a.astype(np.float32).ravel() - a.mean()
    b = b.astype(np.float32).ravel() - b.mean()
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / d if d > 1e-6 else 1.0


def detect_flashes(F):
    out = []
    maxlen = max(2, int(round(0.25 * F.fps)))
    for a, b in runs(F.bright >= 0.6):
        if b - a + 1 > maxlen or a == 0 or F.luma[a] - F.luma[a - 1] < 40:
            continue  # 長い白画面は白背景のショット、急に明るくなっていなければフラッシュではない
        after = F.luma[b + 3:b + 9]
        base = float(np.median(after)) if len(after) else float(F.luma[b])
        e = b
        while e + 1 < F.n and e - b < 6 and F.luma[e + 1] > base + 20:
            e += 1
        nxt = min(F.n - 1, e + 1)
        out.append({"kind": "flash", "start": a, "end": e, "new_start": nxt,
                    "cut": _corr(F.tiny[a - 1], F.tiny[nxt]) < 0.7})
    return out


def classify_transition(F, s, e):
    """s..e-1: 前後のショットが混ざっている途中フレーム, e: 新しいショットの最初のフレーム"""
    base = {"start": int(s), "end": int(e - 1), "new_start": int(e), "frames": int(e - s)}
    if (F.dark[s:e] >= 0.85).any():
        k = s + int(np.argmin(F.luma[s:e]))
        return {**base, "kind": "fade_black", "out_frames": int(k - s + 1), "in_frames": int(e - 1 - k)}
    if (F.bright[s:e] >= 0.85).any():
        k = s + int(np.argmax(F.luma[s:e]))
        return {**base, "kind": "fade_white", "out_frames": int(k - s + 1), "in_frames": int(e - 1 - k)}
    A = F.tiny[s - 1].astype(np.float32)
    B = F.tiny[e].astype(np.float32)
    D = B - A
    dd = float((D * D).sum())
    if dd < (4.0 ** 2) * D.size:
        return {**base, "kind": "effect"}  # 前後がほぼ同じ絵: 画面効果
    alphas, res = [], []
    for m in range(s, e):
        T = F.tiny[m].astype(np.float32) - A
        a = float((T * D).sum()) / dd
        alphas.append(a)
        res.append(float(np.linalg.norm(T - a * D)) / math.sqrt(dd))
    mono = _corr(np.arange(len(alphas), dtype=np.float32), np.array(alphas, np.float32)) if len(alphas) > 2 else 1.0
    if np.median(res) <= 0.3 and mono >= 0.8:
        return {**base, "kind": "dissolve"}
    ls = np.abs(np.log(np.clip(F.m_scale[s:e + 1], 1e-3, None)))
    disp = np.hypot(F.m_dx[s:e + 1], F.m_dy[s:e + 1])
    if F.m_ok[s:e + 1].mean() > 0.5 and ls.mean() > 0.01:
        return {**base, "kind": "zoom_transition"}
    if F.m_ok[s:e + 1].mean() > 0.5 and disp.mean() > 0.02:
        return {**base, "kind": "slide"}
    return {**base, "kind": "wipe_or_other"}


def motion_segments(F, shots):
    fps = F.fps
    ls = np.where(F.m_ok, np.log(np.clip(F.m_scale, 1e-3, None)), 0.0)
    dx = np.where(F.m_ok, F.m_dx, 0.0)
    dy = np.where(F.m_ok, F.m_dy, 0.0)
    thr = 0.02 / fps                       # 2%/秒より速い変化を「動いている」とみなす
    min_len = max(3, int(round(0.3 * fps)))
    box = np.ones(5) / 5
    zooms, pans, shakes = [], [], []
    for sh in shots:
        a, b = sh["start"] + 1, sh["end"]
        if b - a + 1 < min_len:
            continue
        seg = slice(a, b + 1)
        sm = np.convolve(ls[seg], box, mode="same")
        for sign in (1, -1):
            for ra, rb in runs(sign * sm > thr):
                if rb - ra + 1 < min_len:
                    continue
                r = ls[a + ra:a + rb + 1]
                total = float(r.sum())
                if abs(total) < math.log(1.03):
                    continue
                q = max(1, len(r) // 5)
                head, mid_, tail = np.abs(r[:q]).mean(), np.abs(r[q:-q]).mean() if len(r) > 2 * q else np.abs(r).mean(), np.abs(r[-q:]).mean()
                easing = ("ease_in_out" if head < 0.6 * mid_ and tail < 0.6 * mid_ else
                          "ease_in" if head < 0.6 * mid_ else "ease_out" if tail < 0.6 * mid_ else "linear")
                zooms.append({"start": a + ra, "end": a + rb, "scale": round(math.exp(total), 4),
                              "sec": round((rb - ra + 1) / fps, 2), "easing": easing, "shot": sh["index"]})
        v = np.hypot(np.convolve(dx[seg], box, mode="same"), np.convolve(dy[seg], box, mode="same"))
        for ra, rb in runs(v > thr):
            if rb - ra + 1 < min_len:
                continue
            tx, ty = float(dx[a + ra:a + rb + 1].sum()), float(dy[a + ra:a + rb + 1].sum())
            if math.hypot(tx, ty) < 0.05:
                continue
            pans.append({"start": a + ra, "end": a + rb, "dx": round(tx, 3), "dy": round(ty, 3),
                         "sec": round((rb - ra + 1) / fps, 2), "shot": sh["index"]})
        # シェイク: 細かく向きの変わる揺れ
        win = max(4, int(round(0.4 * fps)))
        jit = np.zeros(b - a + 1, bool)
        for j in range(0, b - a + 1 - win + 1):
            wx, wy = dx[a + j:a + j + win], dy[a + j:a + j + win]
            big = np.hypot(wx, wy) > 0.004
            flips = ((np.diff(np.sign(wx)) != 0) | (np.diff(np.sign(wy)) != 0)).mean()
            if big.mean() >= 0.5 and flips >= 0.4:
                jit[j:j + win] = True
        for ra, rb in runs(jit):
            if rb - ra + 1 >= max(3, int(round(0.2 * fps))):
                amp = float(np.percentile(np.hypot(dx[a + ra:a + rb + 1], dy[a + ra:a + rb + 1]), 90))
                shakes.append({"start": a + ra, "end": a + rb, "sec": round((rb - ra + 1) / fps, 2),
                               "amp": round(amp, 4), "shot": sh["index"]})
    return {"zooms": zooms, "pans": pans, "shakes": shakes}


def detect_structure(F, P):
    n, fps, K = F.n, F.fps, F.K
    h1 = max(3, int(round(0.5 * fps)))
    hK = max(K + 4, int(round(1.0 * fps)))
    b1 = rolling_baseline(F.d1, h1, 1)
    bK = rolling_baseline(F.dK, hK, K + 1)
    a1 = F.d1 > np.maximum(P.t1_abs, P.r1 * b1)
    aK = F.dK > np.maximum(P.tK_abs, P.rK * bK)
    a1[0] = False
    aK[:K] = False
    tex = F.tex > P.tex_min
    prev1 = np.vstack([tex[:1], tex[:-1]])
    prevK = np.vstack([np.repeat(tex[:1], K, axis=0), tex[:-K]])
    frac1 = a1.sum(1) / np.maximum((tex | prev1 | a1).sum(1), 4)
    fracK = aK.sum(1) / np.maximum((tex | prevK | aK).sum(1), 4)

    # カメラワーク(ズーム・パン)で説明できるフレーム
    ls = np.log(np.clip(F.m_scale, 1e-3, None))
    disp = np.hypot(F.m_dx, F.m_dy)
    explained = (F.m_ok & (F.m_inl >= 0.5) & ((F.mc_ratio < 0.55) | (F.raw_mad < 2.0))
                 & (np.abs(ls) < 0.2) & (disp < 0.2))
    cam = explained & ((np.abs(ls) > 0.0015) | (disp > 0.0015) | (np.abs(F.m_rot) > 0.1))
    fast = F.m_ok & ((np.abs(ls) > 0.01) | (disp > 0.01))  # 補正しきれない速い動き

    flashes = detect_flashes(F)
    in_flash = np.zeros(n, bool)
    for fl in flashes:
        in_flash[max(0, fl["start"] - 1):fl["new_start"] + 1] = True

    hist_base = rolling_baseline(F.hist_d[:, None], h1, 1)[:, 0]
    hist_ratio = F.hist_d / (hist_base + 0.02)
    cand = (frac1 >= P.cut_frac) | ((frac1 >= 0.3) & (((F.hist_d >= 0.25) & (hist_ratio >= 3)) | (F.ecr >= 0.65)))
    cand &= ~explained & ~in_flash
    cand[0] = False
    cuts, spans = [], []
    for a, b in runs(cand):
        if b - a <= 1:
            cuts.append(a if (b == a or frac1[a] >= frac1[b]) else b)
        else:
            spans.append((a, b))  # 3フレーム以上続く急変 = 速いトランジション

    # なだらかなトランジション(フェード・ディゾルブ等): 1フレーム毎の変化が静かな区間に挟まれて
    # 一時的に上がり、その前後で絵が入れ替わっている所
    tm = F.tiny_mad
    quiet = ndimage.median_filter(tm, size=max(5, int(round(6 * fps)) | 1), mode="nearest")
    elev = (tm > np.maximum(0.8, 3 * quiet)) & ~cam
    for c in cuts:
        elev[max(0, c - 1):c + 2] = False
    for a, b in spans:
        elev[a:b + 1] = False
    for fl in flashes:
        elev[max(0, fl["start"] - 1):fl["new_start"] + 1] = False
    elev[1:-1] |= elev[:-2] & elev[2:]  # 1フレームの途切れはつなぐ
    for a, b in runs(elev):
        if b - a + 1 < 3 or b - a + 1 > 2.5 * fps or a < 1:
            continue
        inside = float(np.median(tm[a:b + 1]))
        pre, post = tm[max(1, a - 10):a], tm[b + 1:b + 11]
        if (len(pre) and np.median(pre) > 0.5 * inside) or (len(post) and np.median(post) > 0.5 * inside):
            continue  # 前後も動いている = 動きの激しいショットの途中
        if np.abs(F.tiny[b].astype(np.float32) - F.tiny[a - 1]).mean() < 8:
            continue  # 前後で絵が変わっていない
        spans.append((a, b))
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    transitions = [classify_transition(F, s, e) for s, e in merged if s >= 1 and e < n]
    transitions += [{"kind": "flash", "start": fl["start"], "end": fl["end"], "new_start": fl["new_start"],
                     "frames": fl["end"] - fl["start"] + 1, "cut": fl["cut"]} for fl in flashes]
    transitions.sort(key=lambda t: t["start"])

    # ショット分割(トランジション中のフレームはどのショットにも入れない)
    bounds = [(c, c, "cut") for c in cuts]
    for t in transitions:
        if t["kind"] == "flash" and not t["cut"]:
            continue  # 同じショットの途中で光っただけ
        if t["kind"] == "effect":
            continue
        bounds.append((t["start"], t["new_start"], t["kind"]))
    bounds.sort()
    shots, cur, cur_in = [], 0, "start"
    for s0, s1, kind in bounds:
        if s0 > cur:
            shots.append({"start": cur, "end": s0 - 1, "in": cur_in})
        if s1 >= cur:
            cur, cur_in = s1, kind
    if cur <= n - 1:
        shots.append({"start": cur, "end": n - 1, "in": cur_in})
    for k, sh in enumerate(shots):
        sh["index"] = k
        sh["frames"] = sh["end"] - sh["start"] + 1
        sh["sec"] = round(sh["frames"] / fps, 3)
    shot_starts = np.array([sh["start"] for sh in shots])

    # 部分変化イベント(テロップ・図形・ジャンプカット等)。カメラの動きを打ち消した差で探す
    # (動いていないテロップはズーム中も「変化なし」、背景のズームは補正で「変化なし」になる)
    d1L, dKL = F.d1L, F.dKL
    b1L = rolling_baseline(d1L, h1, 1)
    bKL = rolling_baseline(dKL, hK, K + 1)
    a1L = d1L > np.maximum(P.t1_abs, P.r1 * b1L)
    aKL = dKL > np.maximum(P.tK_abs, P.rK * bKL)
    a1L[0] = False
    aKL[:K] = False
    ex1 = fast.copy()
    exK = np.convolve(fast.astype(np.float32), np.ones(K), mode="full")[:n] > 0
    for c in cuts:
        ex1[max(0, c - 1):c + 2] = True
        exK[c:c + K + 1] = True
    for t in transitions:
        ex1[max(0, t["start"] - 1):t["new_start"] + 2] = True
        exK[t["start"]:t["new_start"] + K + 1] = True
    gx, gy = F.grid
    L = ((a1L & ~ex1[:, None]) | (aKL & ~exK[:, None])).reshape(n, gy, gx)
    Ld = ndimage.binary_dilation(L, structure=np.ones((3, 1, 1), bool))
    lab, _ = ndimage.label(Ld, structure=np.ones((3, 3, 3), bool))
    lab[~L] = 0
    events = []
    dyn_len = int(round(1.5 * fps))
    for k, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        sub = lab[sl] == k
        cells = np.zeros((gy, gx), bool)
        cells[sl[1], sl[2]] = sub.any(axis=0)
        cm = cells.ravel()
        t0, t1 = sl[0].start, sl[0].stop - 1
        strength = max(float(d1L[t0:t1 + 1][:, cm].max()), float(dKL[t0:t1 + 1][:, cm].max()))
        if cm.sum() < 2 and strength < 20:
            continue
        lo = max(1, t0 - K)
        act = (d1L[lo:t1 + 1][:, cm] > np.maximum(P.t1_abs * 0.5, 2 * b1L[lo:t1 + 1][:, cm])).mean(axis=1) >= 0.3
        idx = np.nonzero(act)[0]
        s, e = (lo + int(idx[0]), lo + int(idx[-1])) if len(idx) else (t0, t0)
        s = max(s, t0 - K)
        pre = b1L[max(0, s - h1):s][:, cm]
        shot = int(np.searchsorted(shot_starts, s, side="right") - 1)
        events.append({
            "start": s, "end": e, "cells": cells, "n_cells": int(cm.sum()),
            "cell_bbox": [int(sl[2].start), int(sl[1].start), int(sl[2].stop), int(sl[1].stop)],
            "instant": bool(e - s <= 1),
            "moving_before": bool(len(pre) and float(np.median(pre)) > 1.5),
            "dynamic": bool(t1 - t0 > dyn_len),
            "shot": max(0, shot),
        })
    events.sort(key=lambda ev: ev["start"])

    S = type("Structure", (), {})()
    S.cuts, S.transitions, S.shots, S.events = sorted(cuts), transitions, shots, events
    S.motion = motion_segments(F, shots)
    S.frac1, S.fracK, S.cam = frac1, fracK, cam
    return S


# ---------------------------------------------------------------- 指定フレームでの精査

ORB = None


def orb_similarity(B, A, min_inliers=15):
    """2枚の間の相似変換を特徴点(ORB)で測る。両方で変わっていない画素(テロップ・ロゴ等)は使わない。
    戻り値: {scale, rot, dx, dy, inliers, inlier_ratio, center(拡大の中心)} または None"""
    global ORB
    if ORB is None:
        ORB = cv2.ORB_create(nfeatures=1500, fastThreshold=10)
    gB, gA = cv2.cvtColor(B, cv2.COLOR_RGB2GRAY), cv2.cvtColor(A, cv2.COLOR_RGB2GRAY)
    H, W = gA.shape
    mask = cv2.dilate((cv2.absdiff(gA, gB) >= 12).astype(np.uint8) * 255, np.ones((9, 9), np.uint8))
    kb, db = ORB.detectAndCompute(gB, mask)
    ka, da = ORB.detectAndCompute(gA, mask)
    if db is None or da is None or len(kb) < 15 or len(ka) < 15:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(db, da, k=2)
    good = [m for m, *rest in pairs if rest and m.distance < 0.75 * rest[0].distance]
    if len(good) < 12:
        return None
    p0 = np.float32([kb[m.queryIdx].pt for m in good])
    p1 = np.float32([ka[m.trainIdx].pt for m in good])
    M, inl = cv2.estimateAffinePartial2D(p0, p1, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    if M is None or int(inl.sum()) < min_inliers:
        return None
    s, rot, dx, dy = similarity_params(M, W, H)
    out = {"scale": round(s, 4), "rot": round(rot, 2), "dx": round(dx, 4), "dy": round(dy, 4),
           "inliers": int(inl.sum()), "inlier_ratio": round(int(inl.sum()) / len(good), 3)}
    if abs(s - 1) >= 0.02 and abs(rot) < 5:
        c, sn = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        fp = np.linalg.solve(np.eye(2) - s * np.array([[c, -sn], [sn, c]]), M[:, 2])
        out["center"] = [round(float(fp[0] / W), 3), round(float(fp[1] / H), 3)]
    return out


def analyze_cut(B, A):
    """カット前後の2枚: 同じ構図(ジャンプカット)か、拡大/縮小(ズームカット)か"""
    gB, gA = cv2.cvtColor(B, cv2.COLOR_RGB2GRAY), cv2.cvtColor(A, cv2.COLOR_RGB2GRAY)
    diff = cv2.absdiff(gA, gB)
    grad = lambda g: cv2.dilate(g, K3).astype(np.int16) - cv2.erode(g, K3)
    content = (grad(gA) >= 20) | (grad(gB) >= 20)
    same = float(((diff < 12) & content).sum() / max(content.sum(), 1))
    out = {"same_framing": round(same, 3), "type": "cut"}
    sim = orb_similarity(B, A)
    if sim is not None:
        out.update({k: sim[k] for k in ("inliers", "inlier_ratio", "scale", "rot", "dx", "dy")})
        if sim["inlier_ratio"] >= 0.3 and abs(sim["scale"] - 1) >= 0.05 and abs(sim["rot"]) < 5 and "center" in sim:
            out["type"] = "zoom_in" if sim["scale"] > 1 else "zoom_out"
            out["center"] = sim["center"]
            return out
        if sim["inliers"] >= 25 and abs(sim["scale"] - 1) < 0.05 and math.hypot(sim["dx"], sim["dy"]) < 0.03:
            out["type"] = "jump"
            return out
    if same >= 0.5:
        out["type"] = "jump"
    return out


def make_aligner(F, size=None):
    """フレームiの絵をフレームjの位置に重ねる関数を作る(走査で求めたカメラの動きを積み重ねる)。
    size: 重ねる画像の解像度(既定は解析解像度)。カメラがほぼ動いていなければNoneを返す"""
    aw, ah = size or F.size
    S = np.diag([aw / F.mid[0], ah / F.mid[1], 1.0])
    Si = np.linalg.inv(S)
    A3 = np.array([S @ np.vstack([m, [0, 0, 1]]) @ Si for m in F.m_A])

    def align(img, i, j):
        M = np.eye(3)
        for k in range(min(i, j) + 1, max(i, j) + 1):
            M = A3[k] @ M
        if j < i:
            M = np.linalg.inv(M)
        if abs(math.hypot(M[0, 0], M[1, 0]) - 1) < 0.001 and math.hypot(M[0, 2], M[1, 2]) < 0.3 * aw / F.size[0]:
            return None
        return cv2.warpAffine(img, M[:2], (aw, ah), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return align


def refine_with_frames(info, F, S, P, out_dir, log=print):
    aw, ah = F.size
    rw, rh = info.size_for(P.review_side)
    ref_scale = 1080.0 / min(aw, ah)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    for sh in S.shots:
        tasks.append(("shot", sh, [(sh["start"] + sh["end"]) // 2]))
    cut_info = {c: {"frame": c} for c in S.cuts}
    for c in S.cuts:
        tasks.append(("cut", cut_info[c], [c - 1, c]))
    # ズーム・パンは1フレーム毎の小さな動きの積み重ねだと誤差が溜まるので、最初と最後の2枚で測り直す
    for z in S.motion["zooms"]:
        tasks.append(("zoom", z, [max(0, z["start"] - 1), z["end"]]))
    for pn in S.motion["pans"]:
        tasks.append(("pan", pn, [max(0, pn["start"] - 1), pn["end"]]))
    half_sec = max(3, int(round(0.5 * F.fps)))
    for i, ev in enumerate(S.events):
        ev["id"] = i
        if ev["dynamic"]:
            continue
        sh = S.shots[ev["shot"]]
        b = max(sh["start"], ev["start"] - 2)
        if b >= ev["start"]:
            continue
        a = min(sh["end"], ev["end"] + 3)
        later = [o["start"] for o in S.events[i + 1:i + 40]
                 if o["start"] > ev["end"] and (o["cells"] & ev["cells"]).any()]
        limit = later[0] - 1 if later else sh["end"]
        a = max(min(a, limit), ev["end"])
        a2 = min(limit, a + half_sec)
        # アニメの観察用フレームは変化が続いた範囲+数フレームだけ(一瞬の変化なら出た瞬間だけ)
        seq_end = min(a, ev["end"] + 3, ev["start"] + 20) if not ev["instant"] else ev["start"]
        roles = {"bprev": b - 2 if b - 2 >= sh["start"] else None, "b": b,
                 "seq": list(range(ev["start"], seq_end + 1)), "a": a,
                 "a2": a2 if a2 >= a + 3 else None}
        ev["_frames"] = roles
        idxs = [roles["b"], roles["a"], *roles["seq"]] + [roles[k] for k in ("bprev", "a2") if roles[k] is not None]
        tasks.append(("event", ev, sorted(set(idxs))))
    last_use, by_last = {}, defaultdict(list)
    for t in tasks:
        last = max(t[2])
        by_last[last].append(t)
        for i in t[2]:
            last_use[i] = max(last_use.get(i, -1), last)
    persist = vo.PersistAccumulator()
    align = make_aligner(F)
    small = {}
    to_save = defaultdict(list)   # 目視確認用に高解像度で保存するフレーム番号 → 保存先

    def save_later(idx, name):
        if len(to_save) < P.max_saved_frames:
            to_save[idx].append(frames_dir / name)
            return f"frames/{name}"
        return None

    def run(task):
        kind, obj, idxs = task
        if any(i not in small for i in idxs):
            return
        if kind == "shot":
            img = small[idxs[0]]
            obj["image"] = save_later(idxs[0], f"shot_{obj['index']:04d}.jpg")
            persist.add(img)
            hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
            obj["luma"] = round(float(cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).mean()), 1)
            obj["sat"] = round(float(hsv[..., 1].mean()), 1)
        elif kind in ("zoom", "pan"):
            sim = orb_similarity(small[idxs[0]], small[idxs[1]], min_inliers=20)
            if sim is not None and sim["inlier_ratio"] >= 0.3:
                if kind == "zoom":
                    r = math.log(sim["scale"]) / math.log(obj["scale"])
                    if 0.4 <= r <= 2.5:
                        obj["scale_frames"], obj["scale"] = obj["scale"], sim["scale"]
                        if "center" in sim:
                            obj["center"] = sim["center"]
                else:
                    v0, v1 = np.array([obj["dx"], obj["dy"]]), np.array([sim["dx"], sim["dy"]])
                    r = float(v1 @ v0) / max(float(v0 @ v0), 1e-9)
                    if 0.6 <= r <= 1.6 and np.linalg.norm(v1 - r * v0) < 0.3 * np.linalg.norm(v0):
                        obj["dx_frames"], obj["dy_frames"] = obj["dx"], obj["dy"]
                        obj["dx"], obj["dy"] = sim["dx"], sim["dy"]
        elif kind == "cut":
            obj.update(analyze_cut(small[idxs[0]], small[idxs[1]]))
            if obj["type"] != "cut":
                pair = [save_later(i, f"cut_{obj['frame']:06d}_{tag}.jpg")
                        for tag, i in (("before", idxs[0]), ("after", idxs[1]))]
                if all(pair):
                    obj["pair"] = pair
        else:
            ev, fr = obj, obj["_frames"]
            b, a = fr["b"], fr["a"]
            B, A = small[b], small[a]
            region = vo.cells_to_mask(ev["cells"], (aw, ah), grow=1)
            A2 = small[fr["a2"]] if fr["a2"] is not None else None
            Bp = small[fr["bprev"]] if fr["bprev"] is not None else None
            # カメラが動いている時は比べる相手を重ねてから比べる(静止時はNone)
            B_on = {i: align(B, b, i) for i in set(fr["seq"]) | {a}}
            Aw2 = align(A, a, fr["a2"]) if A2 is not None else None
            Bpw = align(Bp, fr["bprev"], b) if Bp is not None else None
            peak_frac = None
            if not ev["instant"]:
                count = lambda X, i: int(((vo.motion_diff(X, B, B_on[i]) > 40) & region).sum())
                peak = max(count(small[i], i) for i in fr["seq"])
                peak_frac = count(A, a) / peak if peak > 0 else None
            res = vo.analyze_event(A, B, region, ev["moving_before"], ev["instant"], ref_scale, A2, Bp,
                                   B_on[a], Aw2, Bpw, peak_frac)
            if res is None:
                ev["overlay"] = None
                return
            ov, bbox = res
            if ov["kind"] in ("text", "box", "graphic") and ov["change"] != "disappear":
                ov["animation"] = vo.animation_profile([(i, small[i]) for i in fr["seq"]], B, A, bbox, Bp,
                                                       aligned=B_on, a_idx=a, Bprev_aligned=Bpw)
            if ov["kind"] in ("text", "box", "graphic", "jump", "ui_text"):
                ov["image"] = save_later(fr["a"] if ov["change"] != "disappear" else fr["b"],
                                         f"event_{ev['id']:05d}.jpg")
            ev["overlay"] = ov

    # 解析は走査と同じ解像度で行う(高解像度のまま大量のフレームを受け渡すと遅い)
    need = sorted(last_use)
    t0 = last = time.time()
    for k, (idx, frame) in enumerate(video_io.grab_frames(info, need, (aw, ah))):
        small[idx] = frame
        for t in by_last.get(idx, []):
            run(t)
        for i in [i for i in small if last_use[i] <= idx]:
            del small[i]
        now = time.time()
        if now - last >= 10:
            last = now
            log(f"  フレーム精査 {100 * (k + 1) / len(need):5.1f}% ({now - t0:.0f}秒経過)")
    log(f"  フレーム精査 完了: {len(need)}フレーム ({time.time() - t0:.0f}秒)")
    # 高解像度パス: 目視確認用の画像を保存し、テロップの色・縁・高さを元の解像度で測り直す
    hw, hh = info.size_for(P.hires_side)
    align_hi = make_aligner(F, (hw, hh))
    hi_tasks = []
    for ev in S.events:
        ov = ev.get("overlay")
        if ov and ov["kind"] in ("text", "box") and ov["change"] != "disappear" and len(hi_tasks) < P.max_hires_events:
            hi_tasks.append((ev["_frames"]["b"], ev["_frames"]["a"], ev))
    hi_need = defaultdict(list)
    for t in hi_tasks:
        hi_need[t[0]].append(t)
        hi_need[t[1]].append(t)
    hi_last = {}
    for b, a, _ in hi_tasks:
        hi_last[b] = max(hi_last.get(b, -1), a)
        hi_last[a] = max(hi_last.get(a, -1), a)
    hi = {}
    t0 = time.time()
    for idx, frame in video_io.grab_frames(info, sorted(set(to_save) | set(hi_need)), (hw, hh)):
        if idx in to_save:
            img = frame if (hw, hh) == (rw, rh) else cv2.resize(frame, (rw, rh), interpolation=cv2.INTER_AREA)
            for path in to_save[idx]:
                video_io.imwrite_jpg(path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR), 88)
        if idx in hi_need:
            hi[idx] = frame
            for b, a, ev in hi_need[idx]:
                if a == idx and b in hi:
                    m = vo.measure_telop_hires(hi[a], hi[b], ev["overlay"]["bbox"], align_hi(hi[b], b, a))
                    if m:
                        ov = ev["overlay"]
                        ov["lowres"] = {k: ov.get(k) for k in ("colors", "outline_px1080", "line_h_px1080")}
                        # 高さは背景の動きを除いた解析解像度の値を基本にし、高解像度の値が近い時だけ採用
                        lh = ov.get("line_h_px1080") or 0
                        if not (lh and abs(m["line_h_px1080"] - lh) <= 0.15 * lh):
                            m["line_h_px1080"] = lh
                        ov.update({k: v for k, v in m.items() if v is not None})
            for i in [i for i in hi if hi_last[i] <= idx]:
                del hi[i]
    log(f"  確認用画像を保存: {sum(len(v) for v in to_save.values())}枚 / テロップを高解像度で測定: {len(hi_tasks)}件"
        f" ({time.time() - t0:.0f}秒)")
    for c in S.cuts:
        cut_info[c].setdefault("type", "cut")
    S.cut_info = [cut_info[c] for c in S.cuts]
    S.persistent = persist.result()
    return S


# ---------------------------------------------------------------- まとめ

def _sec(frame, fps):
    return round(frame / fps, 3)


def summarize(info, meta, F, S, audio):
    fps = F.fps
    dur = F.n / fps
    ev_ok = [ev for ev in S.events if ev.get("overlay")]
    tel_all = [ev for ev in ev_ok if ev["overlay"]["kind"] in ("text", "box")]
    texts = [ev for ev in tel_all if ev["overlay"]["change"] != "disappear"]
    jumps_local = [ev for ev in ev_ok if ev["overlay"]["kind"] == "jump"]

    # 編集点 = ハードカット + トランジション + (人物部分だけ飛ぶ)ジャンプカット
    edit_points = sorted(set(S.cuts) | {t["start"] for t in S.transitions if t["kind"] not in ("effect",)
                                        and not (t["kind"] == "flash" and not t.get("cut"))}
                         | {ev["start"] for ev in jumps_local})
    intervals = np.diff([0] + edit_points + [F.n]) / fps
    shot_secs = np.array([sh["sec"] for sh in S.shots])
    hook_n = int(min(F.n, 30 * fps))
    hook_pts = [p for p in edit_points if p < hook_n]

    cut_types = defaultdict(int)
    for ci in S.cut_info:
        cut_types[ci.get("type", "cut")] += 1
    trans_types = defaultdict(int)
    for t in S.transitions:
        trans_types[t["kind"]] += 1
    zoom_cuts = [ci for ci in S.cut_info if ci.get("type", "").startswith("zoom")]

    styles = vo.cluster_styles(texts)
    # 消えたイベントも位置の近いスタイルに割り当てる(表示時間の終わりを知るため)
    for ev in tel_all:
        if ev["overlay"]["change"] != "disappear":
            continue
        cx, cy = ev["overlay"]["center"]
        near = [st for st in styles if abs(st["cy"] - cy) < 0.08 and abs(st["cx"] - cx) < 0.25]
        if near:
            min(near, key=lambda st: abs(st["cy"] - cy))["ends"].append(ev)
    med = lambda xs: round(float(np.median(xs)), 3) if len(xs) else None
    style_out = []
    for k, st in enumerate(styles):
        mem = st["members"]
        ovs = [m["overlay"] for m in mem]
        # 色は「何もない所に出た」イベントを優先(差し替えは前のテロップの跡が混ざる)
        clean = [o for o in ovs if o["change"] == "appear" and o.get("colors")] or [o for o in ovs if o.get("colors")]
        anim = defaultdict(int)
        for o in ovs:
            anim[(o.get("animation") or {"type": "cut"})["type"]] += 1
        main_anim = max(anim, key=anim.get) if anim else "cut"
        anim_frames = [o["animation"]["frames"] for o in ovs
                       if (o.get("animation") or {}).get("type") == main_anim and main_anim not in ("cut", "unknown")]
        fills, outlines = defaultdict(int), defaultdict(int)
        for o in clean:
            fills[o["colors"][0]["color"]] += 1
            if len(o["colors"]) >= 2:
                outlines[o["colors"][-1]["color"]] += 1
        # 表示時間: 出た(差し替わった)時から、同じ場所の次のイベント(差し替え・消え)まで。15秒超は除く
        marks = sorted([(m["start"], "in") for m in mem] + [(m["start"], "out") for m in st["ends"]])
        disp = [(b[0] - a[0]) / fps for a, b in zip(marks[:-1], marks[1:])
                if a[1] == "in" and 0 < (b[0] - a[0]) / fps <= 15]
        style_out.append({
            "id": chr(ord("A") + k) if k < 26 else f"S{k}",
            "count": len(mem),
            "position": vo.position_label(st["cx"], st["cy"]),
            "center": [med([o["center"][0] for o in ovs]), med([o["center"][1] for o in ovs])],
            "bbox_median": [med([o["bbox"][i] for o in ovs]) for i in range(4)],
            "line_h_px1080": med([o["line_h_px1080"] for o in ovs]),
            "lines_median": med([o["lines"] for o in ovs]),
            "fill": max(fills, key=fills.get) if fills else None,
            "outline": max(outlines, key=outlines.get) if outlines else None,
            "outline_px1080": med([o["outline_px1080"] for o in clean if o.get("outline_px1080")]),
            "box": sum(1 for o in ovs if o.get("box_color")) > len(ovs) / 2,
            "box_color": next((o["box_color"] for o in ovs if o.get("box_color")), None),
            "in_animation": main_anim,
            "in_animation_counts": dict(anim),
            "in_frames_median": med(anim_frames),
            "display_sec_median": med(disp),
            "examples": [m["id"] for m in mem[:8]],
            "member_ids": [m["id"] for m in mem],
            "first_sec": _sec(mem[0]["start"], fps),
        })

    ls = np.where(F.m_ok, np.log(np.clip(F.m_scale, 1e-3, None)), 0.0)
    result = {
        "source": {**{k: v for k, v in (meta or {}).items() if v is not None}, "path": str(info.path)},
        "format": {"width": info.width, "height": info.height, "fps": round(info.src_fps, 3),
                   "analysis_fps": round(fps, 3), "duration_sec": round(dur, 2), "frames": F.n,
                   "vcodec": info.vcodec, "aspect": f"{info.width}:{info.height}",
                   "orientation": "landscape" if info.width >= info.height else "portrait"},
        "pacing": {
            "edit_points": len(edit_points),
            "edit_points_per_min": round(len(edit_points) / (dur / 60), 2),
            "interval_mean_sec": round(float(intervals.mean()), 3),
            "interval_median_sec": round(float(np.median(intervals)), 3),
            "interval_p10_sec": round(float(np.percentile(intervals, 10)), 3),
            "interval_p90_sec": round(float(np.percentile(intervals, 90)), 3),
            "shots": len(S.shots),
            "shot_mean_sec": round(float(shot_secs.mean()), 3),
            "shot_median_sec": round(float(np.median(shot_secs)), 3),
            "first_30s_edit_points": len(hook_pts),
            "per_minute": [int(np.sum((np.array(edit_points) >= m * 60 * fps) & (np.array(edit_points) < (m + 1) * 60 * fps)))
                           for m in range(int(math.ceil(dur / 60)))],
        },
        "cuts": {"total": len(S.cuts), "by_type": dict(cut_types), "jump_cuts_partial": len(jumps_local),
                 "zoom_cut_scales": [ci["scale"] for ci in zoom_cuts],
                 "zoom_cut_scale_median": round(float(np.median([ci["scale"] for ci in zoom_cuts])), 3) if zoom_cuts else None,
                 "zoom_cut_centers": [ci.get("center") for ci in zoom_cuts]},
        "transitions": {"by_type": dict(trans_types),
                        "items": [{**t, "sec": _sec(t["start"], fps)} for t in S.transitions]},
        "camera": {
            "zooms": [{**z, "t": _sec(z["start"], fps)} for z in S.motion["zooms"]],
            "pans": [{**p, "t": _sec(p["start"], fps)} for p in S.motion["pans"]],
            "shakes": [{**s, "t": _sec(s["start"], fps)} for s in S.motion["shakes"]],
            "zoomed_time_share": round(float((np.abs(ls) > 0.02 / fps).mean()), 3),
        },
        "telop": {
            "events": len(texts),
            "events_per_min": round(len(texts) / (dur / 60), 2),
            "styles": style_out,
            "graphics": sum(1 for ev in ev_ok if ev["overlay"]["kind"] == "graphic"),
            "other_changes": {k: sum(1 for ev in ev_ok if ev["overlay"]["kind"] == k)
                              for k in ("ui_text", "motion", "minor")},
        },
        "persistent_overlays": S.persistent,
        "color": {
            "luma_mean": round(float(F.luma.mean()), 1),
            "saturation_mean": round(float(F.sat.mean()), 1),
            "contrast_mean": round(float(F.luma_std.mean()), 1),
            "rgb_mean": [round(float(v), 1) for v in F.rgb.mean(axis=0)],
        },
        "audio": {k: v for k, v in audio.items() if k not in ("loudness_curve", "speech_segments", "gaps")},
        "edit_point_frames": edit_points,
    }
    return result


def is_url(s):
    return bool(re.match(r"^https?://", s)) or s.startswith(("youtu.be/", "www.youtube.com/", "youtube.com/"))


def video_id_from_url(url):
    m = re.search(r"(?:youtu\.be/|v=|shorts/|embed/|live/)([\w-]{11})", url)
    return m.group(1) if m else "download"


def analyze(video_path, out_dir, meta=None, P=None, audio=True, log=print):
    import video_report
    P = P or Params()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    info = video_io.probe(video_path)
    log(f"[info] {info.width}x{info.height} / {info.src_fps:.3f}fps / {info.duration:.1f}秒 / {info.vcodec}")
    log("[1/4] 全フレームを走査しています(カット・動き・部分変化)")
    F = scan_frames(info, P, log)
    S = detect_structure(F, P)
    log(f"  ショット{len(S.shots)} / カット{len(S.cuts)} / トランジション{len(S.transitions)} / 部分変化{len(S.events)}")
    log("[2/4] 必要なフレームだけ取り出して精査しています(テロップ・ズームカット・常駐ロゴ)")
    refine_with_frames(info, F, S, P, out_dir, log)
    fps = F.fps
    vis = {
        "cut": [c / fps for c in S.cuts],
        "overlay_in": [ev["start"] / fps for ev in S.events
                       if ev.get("overlay") and ev["overlay"]["kind"] in ("text", "box", "graphic")
                       and ev["overlay"]["change"] != "disappear"],
        "zoom_cut": [ci["frame"] / fps for ci in S.cut_info if ci.get("type", "").startswith("zoom")],
        "transition": [t["start"] / fps for t in S.transitions],
    }
    log("[3/4] 音声を解析しています(音量・間・効果音・BGM)")
    A = video_audio.analyze(info, vis, fps) if audio else {"present": False}
    result = summarize(info, meta, F, S, A)
    log("[4/4] レポートを書き出しています")
    video_report.write_all(out_dir, result, F, S, A)
    log(f"完了: {out_dir / 'report.md'}")
    return result, F, S, A


def main():
    ap = argparse.ArgumentParser(description="動画の編集をフレーム単位で解析して編集レシピを作る")
    ap.add_argument("source", help="YouTubeのURLか動画ファイルのパス")
    ap.add_argument("-o", "--out", type=Path, help="出力フォルダ (既定: output/video_analysis/<動画ID>)")
    ap.add_argument("--download-only", action="store_true", help="ダウンロードだけして終わる")
    ap.add_argument("--cookies-from-browser", help="YouTubeにbot判定された時に使うブラウザ名 (firefox等)")
    ap.add_argument("--max-height", type=int, default=1080, help="ダウンロードする最大の縦解像度")
    ap.add_argument("--no-audio", action="store_true", help="音声解析をしない")
    args = ap.parse_args()

    if is_url(args.source):
        url = args.source if args.source.startswith("http") else "https://" + args.source
        out = args.out or DEFAULT_OUT / video_id_from_url(url)
        print(f"[download] {url}")
        try:
            path, meta = video_io.download(url, out, args.max_height, args.cookies_from_browser)
        except Exception as e:  # yt-dlpの長いエラーを、次に何をすればいいかの説明に直す
            msg = str(e)
            if "403" in msg or "proxy" in msg.lower() or "Unable to connect" in msg:
                hint = ("この環境からYouTubeに接続できません(ネットワーク設定でブロック)。\n"
                        "  → 自分のPCで実行するか、クラウド環境の許可ドメインにYouTubeを追加する(docs/06 のB)")
            elif "confirm you" in msg.lower() or "bot" in msg.lower():
                hint = ("YouTubeにbot判定されました。\n"
                        "  → Firefoxで一度YouTubeにログインしてから --cookies-from-browser firefox を付けて再実行")
            else:
                hint = "  → yt-dlpを更新して再実行: pip install -U \"yt-dlp[default]\" deno (docs/06 の「困ったとき」)"
            sys.exit(f"[download] 失敗しました: {msg.splitlines()[0][:300]}\n{hint}")
        print(f"[download] 保存: {path}")
    else:
        path = Path(args.source)
        if not path.exists():
            sys.exit(f"ファイルがありません: {path}")
        out = args.out or DEFAULT_OUT / path.stem
        meta = {"title": path.name}
    if args.download_only:
        return
    analyze(path, out, meta, audio=not args.no_audio)


if __name__ == "__main__":
    main()
