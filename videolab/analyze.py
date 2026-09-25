"""動画のフレーム単位計測とショット(カット)分割。

1回のデコードで全フレームを計測する(--step 1 が既定 = 文字通りフレーム単位)。

  フレーム毎 : 明るさ・コントラスト・彩度・カラフルさ・色相・エッジ量・
               前フレームとの差分(カット検出用)・動き量(オプティカルフロー)
  5Hz標本    : カメラワーク(拡大率・パン・回転をLK特徴点追跡+相似変換で推定)・
               テロップ(文字領域)推定・キーフレーム(JPEG)
  事後処理   : ハードカット / フェード(黒) / ディゾルブ(線形ブレンド検証) の検出 →
               ショット毎の集計(尺・カメラワーク分類・イージング・配色・テロップ率)
"""

import csv
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import ffmpeg_util as ff

DECODE_W = 640          # デコード解像度(テロップ検出・キーフレーム用)
METRIC_W = 320          # 色・エッジ計測
FLOW_W = 160            # 動き量(Farneback)
THUMB_W, THUMB_H = 64, 36   # カット/ディゾルブ判定用の縮小グレー
KEYFRAME_W = 480
SAMPLE_HZ = 5.0         # カメラワーク・テロップ・キーフレームの標本化レート
CUT_IGNORE_BOTTOM = 0.25  # カット判定の構図比較で無視する下端の割合(テロップ帯)

HIST_BINS = (16, 4, 8)  # HSV


@dataclass
class Transition:
    frame: int              # 次ショットの先頭フレーム
    kind: str               # cut / dissolve / fade_black / flash
    length: int = 0         # 遷移に要したフレーム数(cutは0)
    strength: float = 0.0


@dataclass
class Shot:
    index: int
    start: int              # 先頭フレーム(含む)
    end: int                # 末尾フレーム(含まない)
    fps: float
    transition_in: str = "start"
    transition_len: float = 0.0
    kind: str = "normal"    # normal / black
    stats: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return (self.end - self.start) / self.fps

    def to_dict(self) -> dict:
        d = {
            "index": self.index, "start_frame": self.start, "end_frame": self.end,
            "start": round(self.start / self.fps, 3), "end": round(self.end / self.fps, 3),
            "duration": round(self.duration, 3), "transition_in": self.transition_in,
            "transition_len": round(self.transition_len, 3), "kind": self.kind,
        }
        d.update(self.stats)
        return d


# ---------------------------------------------------------------- フレーム計測

def _colorfulness(rgb_f: np.ndarray) -> float:
    """Hasler & Süsstrunk (2003) のカラフルさ指標(0〜約150)。rgb_fは0-255 float。"""
    r, g, b = rgb_f[..., 0], rgb_f[..., 1], rgb_f[..., 2]
    rg = r - g
    yb = 0.5 * (r + g) - b
    return float(math.hypot(rg.std(), yb.std()) + 0.3 * math.hypot(rg.mean(), yb.mean()))


def detect_text_boxes(rgb: np.ndarray) -> list:
    """テロップらしい横長の高コントラスト文字領域を返す [(x, y, w, h), ...](ピクセル)。

    モルフォロジー勾配 → 二値化 → 横方向クロージングで文字列を連結 → 形状と
    内部の筆画密度・コントラストでふるい分ける古典的手法。OCRではないので
    「文字がありそうな領域」の推定に留まる。
    """
    h_img, w_img = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    _, bw = cv2.threshold(grad, 70, 255, cv2.THRESH_BINARY)
    kw = max(9, w_img // 45)
    closed = cv2.morphologyEx(bw, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (kw, 3)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    boxes = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (0.035 * h_img <= h <= 0.2 * h_img):
            continue
        if w < 2.2 * h or w > 0.98 * w_img:
            continue
        if area < 0.5 * w * h:
            continue
        roi_bw = bw[y:y + h, x:x + w]
        density = float(roi_bw.mean()) / 255.0
        if not (0.18 <= density <= 0.75):
            continue
        roi = gray[y:y + h, x:x + w]
        lo, hi = np.percentile(roi, (5, 95))
        if hi - lo < 110:
            continue
        # 文字は縦方向にも筆画が分布する: 行毎のエッジ占有率が一様すぎる(縞模様)ものを除外
        col_profile = (roi_bw > 0).mean(axis=0)
        if (col_profile > 0).mean() < 0.35:
            continue
        boxes.append((int(x), int(y), int(w), int(h)))
    return boxes


def _fit_similarity(p0: np.ndarray, p1: np.ndarray):
    """RANSACで相似変換を当てはめる。返り値: (2x3行列, inlierマスク) または None。"""
    if len(p0) < 12:
        return None
    m, inl = cv2.estimateAffinePartial2D(p0, p1, method=cv2.RANSAC,
                                         ransacReprojThreshold=1.0, maxIters=2000,
                                         confidence=0.99)
    if m is None or inl is None or inl.sum() < 10:
        return None
    scale = math.hypot(m[0, 0], m[1, 0])
    if not (0.5 < scale < 2.0):
        return None
    return m, inl.ravel().astype(bool)


def _decompose(m: np.ndarray, w: int, h: int):
    """相似変換 → (ln拡大率, 画面中心のdx/幅, dy/高さ, 回転deg)。"""
    a, b = m[0, 0], m[1, 0]
    cx, cy = w / 2.0, h / 2.0
    ncx = m[0, 0] * cx + m[0, 1] * cy + m[0, 2]
    ncy = m[1, 0] * cx + m[1, 1] * cy + m[1, 2]
    return (math.log(math.hypot(a, b)), (ncx - cx) / w, (ncy - cy) / h,
            math.degrees(math.atan2(b, a)))


def _is_still(params, eps: float = 0.0015) -> bool:
    z, dx, dy, rot = params
    return abs(z) < eps and abs(dx) < eps and abs(dy) < eps and abs(rot) < 0.05


def _similarity_from_pairs(p0: np.ndarray, p1: np.ndarray, w: int, h: int):
    """点対応から画面全体の動きを推定し (ln拡大率, dx/幅, dy/高さ, 回転deg, inlier率) を返す。

    テロップ・ロゴ・フェード中のタイトルなど「止まった重ね物」に引っ張られないよう、
    最初のモデルがほぼ静止で、外れ点側に画面の広範囲へ散らばった別の動きがあれば、
    そちら(=背景 = カメラの動き)を採用する。狭い範囲だけ動く場合は被写体の動きとみなす。
    """
    first = _fit_similarity(p0, p1)
    if first is None:
        return None
    m, inl = first
    params = _decompose(m, w, h)
    ratio = float(inl.mean())
    out = ~inl
    if _is_still(params) and out.sum() >= max(15, 0.3 * len(p0)):
        second = _fit_similarity(p0[out], p1[out])
        if second is not None:
            m2, inl2 = second
            params2 = _decompose(m2, w, h)
            pts = p0[out][inl2]
            spread_x = (pts[:, 0].max() - pts[:, 0].min()) / w
            spread_y = (pts[:, 1].max() - pts[:, 1].min()) / h
            if (not _is_still(params2) and inl2.sum() >= 12 and spread_x > 0.5 and spread_y > 0.4):
                return params2 + (float(inl2.sum() / len(p0)),)
    return params + (ratio,)


def estimate_camera(prev_gray: np.ndarray, gray: np.ndarray, mask: np.ndarray = None):
    """2枚のグレー画像間のカメラ(画面全体)の動きを相似変換で推定する。

    mask: 特徴点を取らない領域を0にしたuint8画像。テロップ等の静止オーバーレイを
    除外しないと、背景がズームしていても「静止」と判定されてしまう。
    """
    h, w = gray.shape
    p0 = cv2.goodFeaturesToTrack(prev_gray, maxCorners=400, qualityLevel=0.001,
                                 minDistance=5, blockSize=5, mask=mask)
    if p0 is None or len(p0) < 12:
        return None
    p1, st, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, p0, None, winSize=(21, 21), maxLevel=3)
    if p1 is None:
        return None
    p0r, st2, _ = cv2.calcOpticalFlowPyrLK(gray, prev_gray, p1, None, winSize=(21, 21), maxLevel=3)
    fb = np.linalg.norm((p0 - p0r).reshape(-1, 2), axis=1)
    good = (st.ravel() == 1) & (st2.ravel() == 1) & (fb < 0.5)
    return _similarity_from_pairs(p0[good].reshape(-1, 2), p1[good].reshape(-1, 2), w, h)


class FrameAnalyzer:
    """1フレームずつ流し込んで計測値を蓄積する。"""

    def __init__(self, info: ff.VideoInfo, step: int = 1):
        self.info = info
        self.fps = info.fps or 30.0
        self.step = max(1, step)
        self.dw, self.dh = DECODE_W, ff.even(DECODE_W * info.height / info.width)
        self.mw, self.mh = METRIC_W, ff.even(METRIC_W * info.height / info.width)
        self.fw, self.fh = FLOW_W, ff.even(FLOW_W * info.height / info.width)
        # 標本間隔は「解析するフレーム」(stepの倍数)で数える。そうしないと --step で5Hzが崩れる
        self.sample_every = self.step * max(1, int(round(self.fps / (self.step * SAMPLE_HZ))))
        self.cols = {k: [] for k in (
            "frame", "luma", "contrast", "luma_max", "sat", "colorfulness", "hue_deg",
            "hue_strength", "edge", "motion")}
        self.hists, self.thumbs = [], []
        self.cam_pairs = []      # (prev_frame, frame, ln_scale, dx, dy, rot, inlier)
        self.samples = []        # (frame, jpeg_bytes, text_boxes)
        self.telop_pixels = []
        self._prev_flow = None
        self._prev_cam = None    # (frame, gray)
        hue_centers = (np.arange(12) + 0.5) * 15.0   # OpenCVのHは0-180
        self._hue_centers = hue_centers

    def feed(self, idx: int, rgb: np.ndarray):
        small = cv2.resize(rgb, (self.mw, self.mh), interpolation=cv2.INTER_AREA)
        f = small.astype(np.float32)
        luma = (0.2126 * f[..., 0] + 0.7152 * f[..., 1] + 0.0722 * f[..., 2]) / 255.0
        hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
        s = hsv[..., 1].astype(np.float32) / 255.0
        v = hsv[..., 2].astype(np.float32) / 255.0
        weight = (s * v).ravel()
        hue_hist = np.bincount((hsv[..., 0].ravel() // 15).clip(0, 11), weights=weight,
                               minlength=12)
        total_w = weight.sum()
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 80, 160)
        c = self.cols
        c["frame"].append(idx)
        c["luma"].append(float(luma.mean()))
        c["contrast"].append(float(luma.std()))
        # 暗転判定用の「最も明るい所」は縮小前(640px)の8番目に明るい画素で測る。
        # 縮小すると点のような星が平均されて消え、まばらな星空が「真っ黒」に見えてしまう
        g640 = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).ravel()
        c["luma_max"].append(float(np.partition(g640, -8)[-8]) / 255.0)
        c["sat"].append(float(s.mean()))
        c["colorfulness"].append(_colorfulness(f))
        c["hue_deg"].append(float(self._hue_centers[int(hue_hist.argmax())] * 2.0))
        c["hue_strength"].append(float(hue_hist.max() / total_w) if total_w > 1e-6 else 0.0)
        c["edge"].append(float((edges > 0).mean()))

        hist = cv2.calcHist([hsv], [0, 1, 2], None, list(HIST_BINS), [0, 180, 0, 256, 0, 256])
        hist = hist.ravel().astype(np.float32)
        self.hists.append(hist / max(hist.sum(), 1.0))
        # 構図比較用の縮小画は下端のテロップ帯を除いて作る(同じショット内でのテロップ
        # 切替をカットと誤認しないため)
        # 暗い画(星空)の微妙な濃淡を潰さないよう小数で持つ(メモリ節約のためfloat16)
        top = gray[: int(self.mh * (1.0 - CUT_IGNORE_BOTTOM))].astype(np.float32)
        self.thumbs.append(cv2.resize(top, (THUMB_W, THUMB_H),
                                      interpolation=cv2.INTER_AREA).astype(np.float16))

        fgray = cv2.resize(gray, (self.fw, self.fh), interpolation=cv2.INTER_AREA)
        if self._prev_flow is not None:
            flow = cv2.calcOpticalFlowFarneback(self._prev_flow, fgray, None, 0.5, 3, 9, 2, 5, 1.1, 0)
            c["motion"].append(float(np.linalg.norm(flow, axis=2).mean() / self.fw / self.step))
        else:
            c["motion"].append(0.0)
        self._prev_flow = fgray

        if idx % self.sample_every == 0:
            boxes = detect_text_boxes(rgb)
            mask = self._overlay_mask(boxes)
            if self._prev_cam is not None:
                prev_idx, prev_gray, prev_mask = self._prev_cam
                est = estimate_camera(prev_gray, gray, cv2.bitwise_and(mask, prev_mask))
                if est is not None:
                    self.cam_pairs.append((prev_idx, idx) + est)
            self._prev_cam = (idx, gray, mask)
            kf = cv2.resize(rgb, (KEYFRAME_W, ff.even(KEYFRAME_W * rgb.shape[0] / rgb.shape[1])),
                            interpolation=cv2.INTER_AREA)
            ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(kf, cv2.COLOR_RGB2BGR),
                                   [cv2.IMWRITE_JPEG_QUALITY, 82])
            self.samples.append((idx, jpg.tobytes() if ok else b"", boxes))
            # テロップの色推定用に下半分の文字領域から画素を集める(上限あり)
            if boxes and len(self.telop_pixels) < 400:
                h_img = rgb.shape[0]
                x, y, w, h = max(boxes, key=lambda b: b[1] + b[3])
                if y + h / 2 > 0.5 * h_img:
                    crop = rgb[y:y + h, x:x + w]
                    crop = cv2.resize(crop, (max(8, int(24 * w / max(h, 1))), 24),
                                      interpolation=cv2.INTER_AREA)
                    self.telop_pixels.append(crop.reshape(-1, 3)[::3])

    def _overlay_mask(self, boxes: list) -> np.ndarray:
        """テロップ領域(少し膨らませる)を0にしたMETRIC解像度のマスク。"""
        mask = np.full((self.mh, self.mw), 255, np.uint8)
        sx, sy = self.mw / self.dw, self.mh / self.dh
        for x, y, w, h in boxes:
            pad = int(0.4 * h)
            x0, y0 = int((x - pad) * sx), int((y - pad) * sy)
            x1, y1 = int((x + w + pad) * sx) + 1, int((y + h + pad) * sy) + 1
            mask[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 0
        return mask

    def arrays(self) -> dict:
        out = {k: np.asarray(v, dtype=np.float64) for k, v in self.cols.items()}
        out["frame"] = out["frame"].astype(np.int64)
        return out


# ---------------------------------------------------------------- カット検出

def _bhattacharyya_seq(h: np.ndarray) -> np.ndarray:
    """連続フレーム間のBhattacharyya距離(0=同一, 1=完全に異なる)。先頭は0。"""
    sq = np.sqrt(h)
    bc = (sq[1:] * sq[:-1]).sum(axis=1)
    d = np.sqrt(np.clip(1.0 - bc, 0.0, 1.0))
    return np.concatenate([[0.0], d])


def _bhatt(a: np.ndarray, b: np.ndarray) -> float:
    return float(math.sqrt(max(0.0, 1.0 - float(np.sqrt(a * b).sum()))))


def _ncc_dist(a: np.ndarray, b: np.ndarray) -> float:
    """1 - 正規化相互相関。平坦画像同士は平均輝度差で代替する。"""
    a = a.astype(np.float32).ravel()
    b = b.astype(np.float32).ravel()
    sa, sb = a.std(), b.std()
    if sa < 0.2 or sb < 0.2:      # 本当に一様な画(黒・白一色)だけ平均輝度差で代替
        return min(1.0, abs(a.mean() - b.mean()) / 40.0)
    ncc = float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))
    return float(np.clip(1.0 - ncc, 0.0, 1.0))


def _local_median(x: np.ndarray, radius: int) -> np.ndarray:
    n = len(x)
    out = np.empty(n)
    for i in range(n):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        window = np.concatenate([x[lo:i], x[i + 1:hi]])
        out[i] = np.median(window) if len(window) else 0.0
    return out


def frame_change_scores(hists: np.ndarray, thumbs: np.ndarray) -> np.ndarray:
    """フレーム毎の「前フレームからの変化量」(0〜1)。色分布と構図の両方を見る。"""
    dh = _bhattacharyya_seq(hists)
    dn = np.zeros(len(thumbs))
    for i in range(1, len(thumbs)):
        dn[i] = _ncc_dist(thumbs[i - 1], thumbs[i])
    score = 0.5 * dh + 0.5 * dn
    # 構図がほぼ同一(dn極小)なのに色分布だけ跳ねるのは、無地背景のディゾルブ開始や
    # 色調変化であってカットではない(平坦な画はHSVのビン境界をまたぎやすい)
    return np.where(dn < 0.02, np.minimum(score, 0.15), score)


def is_black(luma_mean: float, luma_max: float) -> bool:
    return luma_mean < 0.03 and luma_max < 0.12


def _dissolve_fit(thumbs: np.ndarray, s: int, e: int):
    """frames s..e-1 が thumbs[s-1] → thumbs[e] の線形ブレンドで説明できるか。

    返り値: (適合度OK?, 平均残差比)
    """
    a = thumbs[s - 1].astype(np.float32).ravel()
    b = thumbs[e].astype(np.float32).ravel()
    d = b - a
    dn2 = float(d @ d)
    if dn2 / len(d) < 12.0 ** 2:         # 端点がほぼ同じ画 → 遷移ではない
        return False, 1.0
    alphas, resid = [], []
    for t in range(s, e):
        x = thumbs[t].astype(np.float32).ravel() - a
        al = float(x @ d) / dn2
        r = x - al * d
        alphas.append(al)
        resid.append(math.sqrt(float(r @ r) / dn2))
    alphas = np.asarray(alphas)
    if len(alphas) >= 3:
        mono = np.corrcoef(np.arange(len(alphas)), alphas)[0, 1]
    else:
        mono = 1.0 if np.all(np.diff(alphas) > 0) else 0.0
    mean_r = float(np.mean(resid))
    ok = (mono > 0.9 and alphas[0] < 0.45 and alphas[-1] > 0.55 and mean_r < 0.3)
    return ok, mean_r


def detect_transitions(scores: np.ndarray, thumbs: np.ndarray, hists: np.ndarray,
                       luma: np.ndarray, luma_max: np.ndarray, fps: float,
                       contrast: np.ndarray = None) -> list:
    n = len(scores)
    trans = []
    if n < 3:
        return trans
    radius = max(5, int(fps))
    med = _local_median(scores, radius)
    black = np.array([is_black(luma[i], luma_max[i]) for i in range(n)])
    flat = (np.asarray(contrast) < 0.02) if contrast is not None else None

    # 1) 黒フレーム区間(フェードアウト/イン、暗転)
    taken = np.zeros(n, dtype=bool)
    i = 0
    while i < n:
        if black[i]:
            j = i
            while j < n and black[j]:
                j += 1
            if j - i <= int(1.5 * fps) and 0 < i and j < n:
                trans.append(Transition(frame=j, kind="fade_black", length=j - i, strength=1.0))
                trans.append(Transition(frame=i, kind="_black_start", length=0))
                taken[max(0, i - 2):min(n, j + 2)] = True
            i = j
        else:
            i += 1

    # 2) ハードカット
    min_gap = max(2, int(0.1 * fps))
    last_cut = -10 ** 9
    for t in range(1, n):
        if taken[t]:
            continue
        sc = scores[t]
        if sc > 0.28 and sc > 2.5 * med[t] + 0.04:
            # フラッシュ判定: 数フレーム後に元の画に戻るなら切替ではない
            back = False
            for k in (2, 3, 4):
                if t + k < n and _bhatt(hists[t - 1], hists[t + k]) < 0.15 and \
                        _ncc_dist(thumbs[t - 1], thumbs[t + k]) < 0.1:
                    back = True
                    break
            if back:
                trans.append(Transition(frame=t, kind="flash", length=0, strength=float(sc)))
                taken[t:t + 5] = True
                continue
            if t - last_cut < min_gap:
                continue
            # 大きな変化が数フレーム続く切替(ホイップパン・白飛び・グリッチ)は1つのカットにまとめる
            e = t
            lim = min(n - 1, t + max(3, int(0.5 * fps)))
            while e + 1 <= lim and not taken[e + 1] and scores[e + 1] > 0.1:
                e += 1
            if e >= lim:          # 0.5秒以内に落ち着かない = 動きの速いショットへの普通のカット
                e = t
            if flat is not None and flat[e]:
                # 白一色などの平坦な画を挟む切替: 平坦な区間(≦0.3秒)の直後にもう一度大きく変わる
                m = e + 1
                while m < n and m - e <= int(0.3 * fps) and flat[m] and scores[m] <= 0.1:
                    m += 1
                if m < n and scores[m] > 0.28 and not taken[m]:
                    e = m
            trans.append(Transition(frame=e, kind="cut", length=e - t, strength=float(sc)))
            last_cut = e
            taken[max(0, t - 1):e + 1] = True

    # 3) ディゾルブ(ツインコンパリゾン + 線形ブレンド検証)
    #    ゆっくりしたディゾルブも拾えるよう、0.4/1.0/1.6秒離れたフレームを短い方から比べる
    ks = sorted({max(4, int(round(c * fps))) for c in (0.4, 1.0, 1.6)})
    cap = 4 * fps
    t = ks[0] + 1
    while t < n - 1:
        far = k = None
        for kk in ks:
            if t - kk < 1 or taken[t - kk:t + 1].any():
                break                      # それより長い基線も重なるので打ち切り
            d = _ncc_dist(thumbs[t - kk], thumbs[t])
            if d >= 0.35:
                far, k = d, kk
                break
        if far is None:
            t += 1
            continue
        # 変化が続いている範囲を広げて端点を探す
        s = t - k
        while s > 1 and not taken[s - 1] and scores[s - 1] > 0.012 and t - s < cap:
            s -= 1
        e = t
        while e < n - 1 and not taken[e + 1] and scores[e + 1] > 0.012 and e - s < cap:
            e += 1
        if s >= 1 and e < n and e - s >= 3:
            ok, _ = _dissolve_fit(thumbs, s, e)
            if ok:
                # 実際に混ざっているフレームだけに絞って長さを出す
                a0 = thumbs[s - 1].astype(np.float32).ravel()
                dv = thumbs[e].astype(np.float32).ravel() - a0
                den = float(dv @ dv) or 1.0
                al = np.array([float((thumbs[i].astype(np.float32).ravel() - a0) @ dv) / den
                               for i in range(s, e)])
                ins = np.nonzero((al > 0.03) & (al < 0.97))[0]
                s2, e2 = (s + int(ins[0]), s + int(ins[-1]) + 1) if len(ins) else (s, e)
                trans.append(Transition(frame=(s2 + e2) // 2, kind="dissolve", length=e2 - s2,
                                        strength=float(far)))
                taken[s - 1:e + 1] = True
                t = e + 1
                continue
        t += 1

    trans.sort(key=lambda x: x.frame)
    return trans


def build_shots(transitions: list, n: int, fps: float) -> list:
    """遷移リストからショット区間を作る。暗転中のフレームはショットに含めない。"""
    shots = []
    cur_start, cur_trans, cur_len = 0, "start", 0.0
    pending_black_start = None
    for tr in transitions:
        if tr.kind == "flash":
            continue
        if tr.kind == "_black_start":
            pending_black_start = tr.frame
            continue
        if tr.kind == "fade_black" and pending_black_start is not None:
            end = pending_black_start
        elif tr.kind == "cut" and tr.length > 0:
            end = tr.frame - tr.length     # ホイップ等の切替中のフレームはどちらのショットにも入れない
        else:
            end = tr.frame
        if end - cur_start >= 1:
            shots.append(Shot(len(shots), cur_start, end, fps, cur_trans, cur_len))
        cur_start, cur_trans, cur_len = tr.frame, tr.kind, tr.length / fps
        pending_black_start = None
    if n - cur_start >= 1:
        shots.append(Shot(len(shots), cur_start, n, fps, cur_trans, cur_len))
    return shots


# ---------------------------------------------------------------- ショット集計

EASING_CURVES = {
    "linear": lambda x: x,
    "ease_in_out": lambda x: x * x * (3 - 2 * x),
    "ease_in": lambda x: x * x,
    "ease_out": lambda x: 1 - (1 - x) ** 2,
}

ZOOM_T, PAN_T, ROT_T = 0.02, 0.02, 1.0


def camera_track(cam_pairs: list, start: int, end: int):
    """ショット内の標本ペアを連結して累積カメラ軌跡を返す。

    返り値: frames(list), cum(ndarray: [ln拡大, x, y, rot]), steps(ndarray)
    """
    steps = [p for p in cam_pairs if p[0] >= start and p[1] < end]
    if not steps:
        return [], np.zeros((0, 4)), np.zeros((0, 4))
    arr = np.array([[p[2], p[3], p[4], p[5]] for p in steps])
    cum = np.vstack([np.zeros(4), np.cumsum(arr, axis=0)])
    frames = [steps[0][0]] + [p[1] for p in steps]
    return frames, cum, arr


def classify_camera(cum: np.ndarray, steps: np.ndarray, duration: float, motion: float) -> dict:
    if len(cum) < 2:
        return {"camera": "unknown", "zoom_total": None, "pan_x_total": None,
                "pan_y_total": None, "rot_total": None, "easing": None, "shake": None}
    total = cum[-1]
    zoom, px, py, rot = total
    # 画面内容が左に流れる(dx<0) = カメラは右へパン
    cand = {
        "zoom_in" if zoom > 0 else "zoom_out": abs(zoom) / ZOOM_T,
        "pan_right" if px < 0 else "pan_left": abs(px) / PAN_T,
        "tilt_down" if py < 0 else "tilt_up": abs(py) / PAN_T,
        "rotate": abs(rot) / ROT_T,
    }
    best, score = max(cand.items(), key=lambda kv: kv[1])
    if score < 1.0:
        best = "static_action" if motion > 0.004 else "static"
    easing = None
    if score >= 1.0 and len(cum) >= 5:
        comp = {"zoom_in": 0, "zoom_out": 0, "pan_right": 1, "pan_left": 1,
                "tilt_down": 2, "tilt_up": 2, "rotate": 3}[best]
        prog = cum[:, comp] / (cum[-1, comp] if abs(cum[-1, comp]) > 1e-9 else 1.0)
        x = np.linspace(0, 1, len(prog))
        errs = {name: float(np.sqrt(np.mean((prog - fn(x)) ** 2)))
                for name, fn in EASING_CURVES.items()}
        easing = min(errs, key=errs.get)
    shake = None
    if len(steps) >= 4:
        detr = steps[:, 1:3] - steps[:, 1:3].mean(axis=0)
        shake = float(np.sqrt((detr ** 2).sum(axis=1).mean()))
    d = max(duration, 1e-6)
    return {
        "camera": best,
        "zoom_total": round(float(math.exp(zoom)), 4),
        "pan_x_total": round(float(px), 4), "pan_y_total": round(float(py), 4),
        "rot_total": round(float(rot), 3),
        "zoom_speed": round(float(abs(math.exp(zoom) - 1) / d), 4),
        "pan_speed": round(float(math.hypot(px, py) / d), 4),
        "easing": easing, "shake": None if shake is None else round(shake, 5),
    }


def palette_from_image(rgb: np.ndarray, k: int = 5) -> list:
    """k-meansで主要色を返す [(hex, 比率), ...](比率降順)。"""
    small = cv2.resize(rgb, (80, max(1, int(80 * rgb.shape[0] / rgb.shape[1]))),
                       interpolation=cv2.INTER_AREA)
    data = small.reshape(-1, 3).astype(np.float32)
    k = min(k, len(np.unique(data, axis=0)))
    if k < 1:
        return []
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    cv2.setRNGSeed(0)
    _, labels, centers = cv2.kmeans(data, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k) / len(labels)
    order = np.argsort(-counts)
    return [("#%02x%02x%02x" % tuple(int(c) for c in centers[i]), round(float(counts[i]), 3))
            for i in order]


def summarize_shots(shots: list, arrays: dict, fa: FrameAnalyzer, video_path) -> None:
    frames = arrays["frame"]
    idx_of = {int(f): i for i, f in enumerate(frames)}
    sample_frames = np.array([s[0] for s in fa.samples]) if fa.samples else np.zeros(0)
    for sh in shots:
        rows = [idx_of[f] for f in range(sh.start, sh.end) if f in idx_of]
        if not rows:
            continue
        # 切替直後のフレームは動き量が跳ねるので除外
        inner = rows[1:] if len(rows) > 2 else rows
        sel = lambda k: arrays[k][inner]  # noqa: E731
        luma_m = float(np.mean(arrays["luma"][rows]))
        lmax = float(np.mean(arrays["luma_max"][rows]))
        if is_black(luma_m, lmax):
            sh.kind = "black"
        motion = float(np.median(sel("motion")))
        _, cum, steps = camera_track(fa.cam_pairs, sh.start, sh.end)
        cam = classify_camera(cum, steps, sh.duration, motion)
        # キーフレーム = ショット中央に最も近い標本
        mid = (sh.start + sh.end) // 2
        kf_bytes = None
        text_hits, text_n, telop_y = 0, 0, []
        for fidx, jpg, boxes in fa.samples:
            if sh.start <= fidx < sh.end:
                text_n += 1
                if boxes:
                    text_hits += 1
                    b = max(boxes, key=lambda bb: bb[1] + bb[3])
                    telop_y.append((b[1] + b[3] / 2) / fa.dh)
        if len(sample_frames):
            inside = [(abs(f - mid), j) for j, f in enumerate(sample_frames) if sh.start <= f < sh.end]
            if inside:
                kf_bytes = fa.samples[min(inside)[1]][1]
        kf = None
        if kf_bytes:
            kf = cv2.cvtColor(cv2.imdecode(np.frombuffer(kf_bytes, np.uint8), cv2.IMREAD_COLOR),
                              cv2.COLOR_BGR2RGB)
        else:
            try:
                kf = ff.grab_frame(video_path, mid / sh.fps, KEYFRAME_W,
                                   ff.even(KEYFRAME_W * fa.info.height / fa.info.width))
            except Exception:
                kf = None
        sh.stats.update({
            "luma": round(luma_m, 4),
            "contrast": round(float(np.mean(arrays["contrast"][rows])), 4),
            "sat": round(float(np.mean(arrays["sat"][rows])), 4),
            "colorfulness": round(float(np.mean(arrays["colorfulness"][rows])), 2),
            "edge": round(float(np.mean(arrays["edge"][rows])), 4),
            "motion": round(motion, 5),
            **cam,
            "text_ratio": round(text_hits / text_n, 3) if text_n else None,
            "telop_y": round(float(np.median(telop_y)), 3) if telop_y else None,
            "palette": palette_from_image(kf) if kf is not None else [],
        })
        sh._keyframe = kf  # 保存用(to_dictには含めない)


# ---------------------------------------------------------------- 全体処理

def _progress(done: int, total: int, t0: float):
    if total <= 0:
        return
    pct = min(100.0, 100.0 * done / total)
    el = time.time() - t0
    eta = el / max(done, 1) * max(total - done, 0)
    sys.stderr.write(f"\r  フレーム解析 {done}/{total} ({pct:5.1f}%)  残り約{eta:5.0f}秒 ")
    sys.stderr.flush()


def analyze_frames(video_path, step: int = 1, max_seconds: float = None, quiet: bool = False):
    info = ff.probe(video_path)
    fa = FrameAnalyzer(info, step=step)
    total = info.n_frames if not max_seconds else min(info.n_frames, int(max_seconds * info.fps))
    t0 = time.time()
    n_decoded = 0
    for idx, rgb in enumerate(ff.iter_frames(video_path, fa.dw, fa.dh,
                                             duration=max_seconds)):
        n_decoded = idx + 1
        if idx % fa.step == 0:
            fa.feed(idx, rgb)
        if not quiet and idx % 200 == 0:
            _progress(idx, total, t0)
    if not quiet:
        _progress(n_decoded, n_decoded, t0)
        sys.stderr.write("\n")
    if n_decoded == 0:
        raise ValueError(f"フレームを1枚もデコードできませんでした: {video_path}")
    info.n_frames = n_decoded
    arrays = fa.arrays()
    hists = np.stack(fa.hists)
    thumbs = np.stack(fa.thumbs)
    scores = frame_change_scores(hists, thumbs)
    eff_fps = fa.fps / fa.step
    transitions = detect_transitions(scores, thumbs, hists, arrays["luma"], arrays["luma_max"],
                                     eff_fps, arrays["contrast"])
    # stepを間引いた場合は行番号→元フレーム番号に戻す
    for tr in transitions:
        tr.frame = int(arrays["frame"][min(tr.frame, len(arrays["frame"]) - 1)])
        tr.length *= fa.step
    shots = build_shots(transitions, n_decoded, fa.fps)
    summarize_shots(shots, arrays, fa, video_path)
    arrays["cut_score"] = scores
    # テロップ推定は5Hz標本のみ(それ以外のフレームはNaN)
    text = np.full(len(arrays["frame"]), np.nan)
    row_of = {int(f): i for i, f in enumerate(arrays["frame"])}
    for fidx, _, boxes in fa.samples:
        if fidx in row_of:
            text[row_of[fidx]] = 1.0 if boxes else 0.0
    arrays["text"] = text
    return info, fa, arrays, transitions, shots


def per_frame_camera(fa: FrameAnalyzer, shots: list, frames: np.ndarray) -> np.ndarray:
    """ショット内の累積カメラ軌跡を全フレームに補間する (N, 3): ln拡大, x, y。"""
    out = np.full((len(frames), 3), np.nan)
    pos = {int(f): i for i, f in enumerate(frames)}
    for sh in shots:
        kfs, cum, _ = camera_track(fa.cam_pairs, sh.start, sh.end)
        if len(kfs) < 2:
            continue
        rng = [f for f in range(sh.start, sh.end) if f in pos]
        for comp in range(3):
            vals = np.interp(rng, kfs, cum[:, comp])
            for f, v in zip(rng, vals):
                out[pos[f], comp] = v
    return out


def write_outputs(out_dir: Path, info, fa, arrays, transitions, shots):
    out_dir.mkdir(parents=True, exist_ok=True)
    kdir = out_dir / "keyframes"
    kdir.mkdir(exist_ok=True)
    frames = arrays["frame"]
    shot_of = np.full(len(frames), -1)
    pos = {int(f): i for i, f in enumerate(frames)}
    for sh in shots:
        for f in range(sh.start, sh.end):
            if f in pos:
                shot_of[pos[f]] = sh.index
    cam = per_frame_camera(fa, shots, frames)
    cols = ["frame", "t", "shot", "luma", "contrast", "sat", "colorfulness", "hue_deg",
            "hue_strength", "edge", "motion", "cut_score", "cam_zoom", "cam_x", "cam_y", "text"]
    with open(out_dir / "frames.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for i, f in enumerate(frames):
            w.writerow([
                int(f), f"{f / fa.fps:.3f}", int(shot_of[i]),
                *(f"{arrays[k][i]:.4f}" for k in ("luma", "contrast", "sat")),
                f"{arrays['colorfulness'][i]:.2f}", f"{arrays['hue_deg'][i]:.0f}",
                f"{arrays['hue_strength'][i]:.3f}", f"{arrays['edge'][i]:.4f}",
                f"{arrays['motion'][i]:.5f}", f"{arrays['cut_score'][i]:.4f}",
                *("" if np.isnan(v) else f"{v:.4f}" for v in cam[i]),
                "" if np.isnan(arrays["text"][i]) else int(arrays["text"][i]),
            ])
    shot_dicts = []
    for sh in shots:
        d = sh.to_dict()
        kf = getattr(sh, "_keyframe", None)
        if kf is not None:
            name = f"shot_{sh.index:04d}.jpg"
            ff.imwrite(kdir / name, cv2.cvtColor(kf, cv2.COLOR_RGB2BGR),
                       [cv2.IMWRITE_JPEG_QUALITY, 85])
            d["keyframe"] = f"keyframes/{name}"
        shot_dicts.append(d)
    with open(out_dir / "shots.json", "w", encoding="utf-8") as fh:
        json.dump(shot_dicts, fh, ensure_ascii=False, indent=1)
    flat_cols = ["index", "start", "end", "duration", "transition_in", "transition_len", "kind",
                 "camera", "zoom_total", "pan_x_total", "pan_y_total", "zoom_speed", "pan_speed",
                 "easing", "motion", "luma", "sat", "colorfulness", "text_ratio", "telop_y",
                 "palette"]
    with open(out_dir / "shots.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(flat_cols)
        for d in shot_dicts:
            w.writerow([" ".join(c for c, _ in d.get("palette", [])) if k == "palette"
                        else d.get(k, "") for k in flat_cols])
    telop_colors = []
    if fa.telop_pixels:
        px = np.concatenate(fa.telop_pixels).astype(np.float32)
        if len(px) >= 30:
            crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1.0)
            cv2.setRNGSeed(0)
            _, lab, cen = cv2.kmeans(px, 3, None, crit, 3, cv2.KMEANS_PP_CENTERS)
            cnt = np.bincount(lab.ravel(), minlength=3) / len(lab)
            telop_colors = [("#%02x%02x%02x" % tuple(int(v) for v in cen[i]), round(float(cnt[i]), 3))
                            for i in np.argsort(-cnt)]
    meta = {
        "video": info.to_dict(), "step": fa.step, "sample_hz": SAMPLE_HZ,
        "n_frames_analyzed": int(len(frames)),
        "transitions": [{"frame": t.frame, "t": round(t.frame / fa.fps, 3), "kind": t.kind,
                         "length": t.length} for t in transitions if not t.kind.startswith("_")],
        "telop_colors": telop_colors,
    }
    with open(out_dir / "meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    return shot_dicts, meta
