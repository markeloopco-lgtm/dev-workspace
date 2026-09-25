#!/usr/bin/env python3
"""テロップ・オーバーレイ(画面の一部だけが出たり消えたりする要素)の解析。

analyze_video.py が見つけた「部分変化イベント」について、前後フレームの差分から
出現した要素の位置・大きさ・色(塗り/縁)・出方(アニメーション)を測る。
画像はすべてRGB uint8 (解析解像度) を受け取る。
"""

import math

import cv2
import numpy as np
from scipy import ndimage

K3 = np.ones((3, 3), np.uint8)


def dist_to_outside(mask):
    """マスク内の各画素から外(マスク外または画像の縁)までの距離。
    切り抜きいっぱいのマスクでも無限大にならないよう、周囲1画素を外として扱う"""
    pad = np.pad(mask.astype(np.uint8), 1)
    return cv2.distanceTransform(pad, cv2.DIST_L2, 3)[1:-1, 1:-1]


def cells_to_mask(cells: np.ndarray, size: tuple, grow: int = 1) -> np.ndarray:
    """(gy, gx)のセル単位マスクを画素マスク(h, w)に広げる。growセル分だけ周囲も含める"""
    w, h = size
    c = cells.astype(np.uint8)
    if grow:
        c = cv2.dilate(c, np.ones((2 * grow + 1, 2 * grow + 1), np.uint8))
    return cv2.resize(c, (w, h), interpolation=cv2.INTER_NEAREST) > 0


def absdiff_max(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return cv2.absdiff(a, b).max(axis=2)


def motion_diff(x, y, y_aligned=None):
    """画素ごとの差。カメラが動いている時は y をxに重ねた版(y_aligned)との差と小さい方を取る:
    止まっているテロップも、ズーム・スクロールする背景も「変化なし」になり、本当に出た物だけ残る"""
    d = absdiff_max(x, y)
    return d if y_aligned is None else np.minimum(d, absdiff_max(x, y_aligned))


def changed_mask(A, B, region, thr=40, B_aligned=None):
    """前後の差が大きい画素。(生マスク, 隙間を埋めたマスク, 差分) を返す。
    生マスクは文字の形や色を測るのに使い、埋めたマスクは範囲(bbox)を決めるのに使う"""
    diff = motion_diff(A, B, B_aligned)
    raw = ((diff > thr) & region).astype(np.uint8)
    raw = cv2.morphologyEx(raw, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    closed = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return raw > 0, closed > 0, diff


def components_bbox(mask, min_area):
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    keep = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_area]
    if not keep:
        return None, np.zeros_like(mask)
    kmask = np.isin(lab, keep)
    ys, xs = np.nonzero(kmask)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1), kmask


def contrast_density(rgb):
    """強い局所コントラスト(縁取り文字の特徴)を持つ画素の割合"""
    if rgb.size == 0:
        return 0.0
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    rng = cv2.dilate(g, K3).astype(np.int16) - cv2.erode(g, K3)
    return float((rng >= 100).mean())


def _runs(flags):
    out, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def line_bands(mask):
    """横書きの行の帯: (行数, 行の高さの中央値, [(上, 下)...])"""
    h = mask.shape[0]
    rows = mask.mean(axis=1) if mask.size else np.zeros(0)
    bands = [(a, b) for a, b in _runs(rows >= 0.03) if b - a + 1 >= 3]
    lh = float(np.median([b - a + 1 for a, b in bands])) if bands else float(h)
    return len(bands), lh, bands


def count_glyphs(mask, bands):
    """行ごとに列方向の投影を取り、文字の間の隙間で区切られた塊を数える。
    (縁取り文字は縁どうしがつながるため連結成分では数えられない。塗りの画素なら字間が空く)"""
    n = 0
    for a, b in bands:
        lh = b - a + 1
        cols = mask[a:b + 1].mean(axis=0)
        for c0, c1 in _runs(cols > 0.05):
            if 0.08 * lh <= c1 - c0 + 1 <= 1.6 * lh:
                n += 1
    return n


def is_box(mask):
    """座布団(文字の後ろの塗りつぶし矩形): 変化した画素が範囲をほぼ埋め尽くしている"""
    h, w = mask.shape
    if h < 4 or w < 4:
        return False
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    return any(stats[i, cv2.CC_STAT_AREA] >= 0.7 * h * w and
               stats[i, cv2.CC_STAT_AREA] >= 0.85 * stats[i, cv2.CC_STAT_WIDTH] * stats[i, cv2.CC_STAT_HEIGHT]
               for i in range(1, n))


def _luma_gap(h1, h2):
    lum = lambda h: 0.299 * int(h[1:3], 16) + 0.587 * int(h[3:5], 16) + 0.114 * int(h[5:7], 16)
    return abs(lum(h1) - lum(h2))


def _hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*[int(round(v)) for v in rgb])


def _is_mix(c, a, b, tol=18.0):
    """色cがaとbの中間色(アンチエイリアス)か"""
    d = b - a
    dd = float(d @ d)
    if dd < 1e-6:
        return False
    t = float((c - a) @ d) / dd
    return 0.1 < t < 0.9 and np.linalg.norm(c - (a + t * d)) < tol


def _close_kernel(px_scale):
    k = max(3, int(round(5 * px_scale)) | 1)
    return np.ones((k, k), np.uint8)


def telop_colors(rgb, mask, k=4, px_scale=1.0):
    """テロップの色を [塗り, 縁] の順に返す(各: 色, 割合, 外側に接している割合)。

    変化した画素の隙間を埋めた範囲で色を分ける: 塗りが背景と同じ色(白い画面に白文字)で変化していなくても、
    縁と縁に挟まれた塗りは隙間埋めで拾える。縁は文字の外形の外側に接し、塗りは接しない。
    px_scale: 解析解像度(長辺640)に対する倍率(隙間埋めの大きさを解像度に合わせる)"""
    closed = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, _close_kernel(px_scale)) > 0
    closed |= ndimage.binary_fill_holes(mask) & ~mask
    whole = ndimage.binary_fill_holes(closed)
    px = rgb[closed].astype(np.float32)
    if len(px) < 40:
        return []
    k = int(min(k, max(1, len(px) // 20)))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1.0)
    _, lab, centers = cv2.kmeans(px, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    lab = lab.ravel()
    r = max(1, int(round(2 * px_scale)))
    outside = cv2.dilate(np.pad((~whole).astype(np.uint8), 1, constant_values=1),
                         np.ones((2 * r + 1, 2 * r + 1), np.uint8))[1:-1, 1:-1] > 0
    touch = outside[closed]
    layers = []
    for j in range(k):
        sel = lab == j
        if sel.mean() >= 0.08:
            layers.append({"rgb": centers[j], "share": float(sel.mean()), "touch": float(touch[sel].mean())})
    # 塗り = 外側に一番接していない色(同程度なら多い方)
    layers.sort(key=lambda L: (round(L["touch"], 1), -L["share"]))
    if len(layers) >= 2:
        # 縁 = 残りのうち一番多い色(塗りと縁の中間色=アンチエイリアスは除く)
        main = max(layers[1:], key=lambda q: q["share"])
        rest = [L for L in layers[1:] if L is main or not _is_mix(L["rgb"], layers[0]["rgb"], main["rgb"])]
        layers = [layers[0], max(rest, key=lambda q: q["share"])]
        # 背景と同じ色の縁は差分に写らないはず。「縁」が背景色で「塗り」が違うなら、白地に白文字のように
        # 塗りが背景と同じ色のケースなので入れ替える
        around = ~(cv2.dilate(whole.astype(np.uint8), K3) > 0)
        if around.sum() >= 30:
            bg = np.median(rgb[around].reshape(-1, 3).astype(np.float32), axis=0)
            dist = lambda L: float(np.abs(L["rgb"] - bg).max())
            if dist(layers[1]) < 35 and dist(layers[0]) > 60:
                layers = [layers[1], layers[0]]
    return [{"color": _hex(L["rgb"]), "share": round(L["share"], 3), "touch": round(L["touch"], 3)}
            for L in layers]


def outline_width(rgb, mask, layers):
    """縁取りの太さ(画素): 縁の色の帯の「芯」(中心線)での太さ。
    帯の中心から外までの距離rの2倍が幅。縁どうしがつながって太く見える所に引っ張られないよう下位30%点。
    (合成テストで正解との差は1割前後)"""
    if len(layers) < 2:
        return None
    col = np.array([int(layers[1]["color"][i:i + 2], 16) for i in (1, 3, 5)], np.float32)
    om = mask & (np.abs(rgb.astype(np.float32) - col).max(axis=2) < 40)
    if om.sum() < 20:
        return None
    dt = dist_to_outside(om)
    ridge = om & (dt >= cv2.dilate(dt, K3)) & (dt > 0.5)
    vals = dt[ridge]
    # 縁の色のしきい値で両側のアンチエイリアスが約0.5画素ずつ削られるので1画素足す
    return float(2 * np.percentile(vals, 30) + 1) if len(vals) >= 5 else None


def box_inner_mask(rgb, box_mask):
    """座布団の上の文字: 矩形の地色と違う画素。地色が矩形の6割以上を占めなければ座布団ではない"""
    px = rgb[box_mask].reshape(-1, 3).astype(np.float32)
    fill = np.median(px, axis=0)
    if float((np.abs(px - fill).max(axis=1) < 30).mean()) < 0.6:
        return None, None
    d = np.abs(rgb.astype(np.float32) - fill).max(axis=2)
    inner = cv2.erode(box_mask.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    return (d > 60) & inner, _hex(fill)


def animation_profile(seq, B, A, bbox, Bprev=None, pad=0.25, aligned=None, a_idx=None, Bprev_aligned=None):
    """出現アニメーション: 各フレームの見え方を最終状態と比べる。
    seq: [(番号, RGB)] 変化の始まりから落ち着くまで。Bprev: Bより前のフレーム(背景の動きの除外用)。
    aligned: {フレーム番号: そのフレームに重ねたB} (カメラが動いている時だけ)。
    戻り値は種類・所要フレーム数(表示し切るまで、そのフレームを含む)・フレーム毎の値"""
    aligned = aligned or {}
    ref = lambda idx: None if aligned.get(idx) is None else crop(aligned[idx])
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    H, W = A.shape[:2]
    ex0, ey0 = max(0, int(x0 - pad * bw)), max(0, int(y0 - pad * bh))
    ex1, ey1 = min(W, int(x1 + pad * bw)), min(H, int(y1 + pad * bh))
    crop = lambda img: img[ey0:ey1, ex0:ex1]
    dA = motion_diff(crop(A), crop(B), ref(a_idx)).astype(np.float32)
    noise = np.zeros(dA.shape, bool)
    if Bprev is not None:  # テロップの下で元々動いていた所(人物の口元など)は見ない
        bpa = None if Bprev_aligned is None else crop(Bprev_aligned)
        noise = cv2.dilate((motion_diff(crop(B), crop(Bprev), bpa) > 15).astype(np.uint8), K3) > 0
    fin = (dA > 40) & ~noise
    if fin.sum() < 20:
        return {"type": "unknown", "frames": 0, "profile": []}
    pct = lambda v: np.percentile(v, [5, 50, 95])
    fy, fx = np.nonzero(fin)
    fx5, fxm, fx95 = pct(fx)
    fy5, fym, fy95 = pct(fy)
    fw, fh = fx95 - fx5 + 1, fy95 - fy5 + 1
    prof = []
    peak = 0.0
    for idx, F in seq:
        d = motion_diff(crop(F), crop(B), ref(idx)).astype(np.float32)
        m = (d > 40) & ~noise
        peak = max(peak, float(m.sum()))
        area = float(m.sum() / fin.sum())
        op = float(np.median(np.clip(d[fin] / np.maximum(dA[fin], 1), 0, 1.5)))
        if m.sum() >= 10:
            my, mx = np.nonzero(m)
            x5, xm, x95 = pct(mx)
            y5, ym, y95 = pct(my)
            size = ((x95 - x5 + 1) / fw, (y95 - y5 + 1) / fh)
            shift = ((xm - fxm) / fw, (ym - fym) / fh)
            left = (x5 - fx5) / fw
        else:
            size, shift, left = (0.0, 0.0), (0.0, 0.0), 0.0
        prof.append({"frame": int(idx), "area": round(area, 3), "opacity": round(op, 3),
                     "w": round(float(size[0]), 3), "h": round(float(size[1]), 3),
                     "dx": round(float(shift[0]), 3), "dy": round(float(shift[1]), 3),
                     "left": round(float(left), 3)})
    started = [q for q in prof if q["area"] > 0.05]
    if not started:
        return {"type": "unknown", "frames": 0, "profile": prof}

    def settled(q):
        return (abs(q["area"] - 1) < 0.12 and abs(q["opacity"] - 1) < 0.12 and abs(q["w"] - 1) < 0.05
                and abs(q["h"] - 1) < 0.05 and abs(q["dx"]) < 0.05 and abs(q["dy"]) < 0.08)

    first = started[0]
    settle = next((q for q in started if settled(q) and all(settled(r) for r in started if r["frame"] > q["frame"])),
                  started[-1])
    anim = [q for q in started if q["frame"] < settle["frame"]]
    if not anim:
        return {"type": "cut", "frames": 1, "profile": prof}
    n = settle["frame"] - first["frame"] + 1
    sizes = [q["w"] * q["h"] for q in started]
    head = anim[0]
    if max(sizes) > 1.08 ** 2 and head["w"] * head["h"] < 0.9:
        kind = "pop_overshoot"
    elif head["w"] * head["h"] < 0.7 and abs(head["dx"]) < 0.15 and abs(head["dy"]) < 0.15:
        kind = "pop"
    elif abs(head["dx"]) >= 0.15 or abs(head["dy"]) >= 0.15:
        dx, dy = head["dx"], head["dy"]
        kind = ("slide_from_" + ("left" if dx < 0 else "right")) if abs(dx) >= abs(dy) else \
               ("slide_from_" + ("top" if dy < 0 else "bottom"))
    elif head["w"] < 0.7 and head["left"] < 0.1 and head["h"] > 0.7:
        kind = "wipe_left_to_right"
    elif head["opacity"] < 0.7 and head["w"] > 0.7 and head["h"] > 0.7:
        kind = "fade"
    else:
        kind = "animated"
    return {"type": kind, "frames": int(n), "profile": prof}


def _best_shift(src, dst, bbox, mask, max_frac):
    """srcのbbox内の絵(maskの画素)がdstのどこに一番よく重なるか: (dx, dy, 最良の誤差/ずれなしの誤差, 最良の平均二乗誤差)"""
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    H, W = dst.shape[:2]
    tpl = src[y0:y1, x0:x1].astype(np.float32)
    mx, my = int(max_frac * w) + 2, int(max_frac * h) + 2
    sx0, sy0, sx1, sy1 = max(0, x0 - mx), max(0, y0 - my), min(W, x1 + mx), min(H, y1 + my)
    search = dst[sy0:sy1, sx0:sx1].astype(np.float32)
    res = cv2.matchTemplate(search, tpl, cv2.TM_SQDIFF, mask=mask.astype(np.float32))
    best, _, loc, _ = cv2.minMaxLoc(res)
    zero = float(res[y0 - sy0, x0 - sx0])
    npx = max(float(mask.sum()), 1.0)
    return sx0 + loc[0] - x0, sy0 + loc[1] - y0, best / (zero + 1e-6), best / npx


def displaced_match(A, B, bbox, mask, max_frac=0.25):
    """人物やアバターが「飛んだ」か: Bの絵がAのずれた位置に、Aの絵がBの逆にずれた位置に見つかる。
    新しく出たもの(テロップ等)は、Aの絵がBのどこにも無いので当てはまらない"""
    x0, y0, x1, y1 = bbox
    if x1 - x0 < 16 or y1 - y0 < 16 or mask.sum() < 50:
        return None
    gA = cv2.cvtColor(A, cv2.COLOR_RGB2GRAY)
    gB = cv2.cvtColor(B, cv2.COLOR_RGB2GRAY)
    var = float(gB[y0:y1, x0:x1][mask].astype(np.float32).var())
    if var < 64:
        return None  # 模様のない所はずれを測れない
    f = _best_shift(gB, gA, bbox, mask, max_frac)
    r = _best_shift(gA, gB, bbox, mask, max_frac)
    return {"shift": [int(f[0]), int(f[1])], "ratio": round(max(f[2], r[2]), 3),
            "consistent": abs(f[0] + r[0]) <= 4 and abs(f[1] + r[1]) <= 4,
            "err": round(max(f[3], r[3]) / var, 3)}


def analyze_event(A, B, region, moving_before, instant, ref_scale, A2=None, Bprev=None,
                  B_aligned=None, A_aligned2=None, Bprev_aligned=None, peak_frac=None):
    """1つの部分変化イベントを分類し、テロップなら見た目を測る。

    A/B: 変化後/前のフレーム, region: 変化のあったセル範囲の画素マスク,
    moving_before: 直前もその場所が動いていたか(人物・アバターなど),
    ref_scale: 解析解像度の画素を1080p換算に直す倍率,
    A2: 変化の約0.5秒後のフレーム(出たものが居座るか=テロップか、すぐ変わるか=動きか の判定用),
    Bprev: Bより少し前のフレーム(テロップの下で元々動いていた画素=口元などを除くため),
    *_aligned: カメラが動いている時、比べる相手のフレームに重ねた版(静止時はNone),
    peak_frac: 変化の途中で一番変わった時の変化量に対する、最終的な変化量の比(瞬きなど行って戻る変化の判定)"""
    H, W = A.shape[:2]
    raw, closed, _ = changed_mask(A, B, region, 40, B_aligned)
    bbox, kmask = components_bbox(closed, 12)
    if bbox is None:
        raw, closed, _ = changed_mask(A, B, region, 20, B_aligned)
        bbox, kmask = components_bbox(closed, 12)
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    raw = raw & kmask
    area_frac = float(kmask.sum()) / (H * W)
    cA = contrast_density(A[y0:y1, x0:x1])
    cB = contrast_density(B[y0:y1, x0:x1])
    if cA >= 1.4 * cB + 0.01:
        change = "appear"
    elif cB >= 1.4 * cA + 0.01:
        change = "disappear"
    else:
        change = "change"
    shown = A if change != "disappear" else B
    out = {
        "change": change,
        "bbox": [round(x0 / W, 4), round(y0 / H, 4), round(x1 / W, 4), round(y1 / H, 4)],
        "center": [round((x0 + x1) / 2 / W, 4), round((y0 + y1) / 2 / H, 4)],
        "area_frac": round(area_frac, 5),
    }

    # ジャンプカット: 同じ絵がずれた位置にある(新しい物が出たのではなく、元の物が飛んだ)
    if instant and area_frac >= 0.01 and B_aligned is None:
        dm = displaced_match(A, B, bbox, kmask[y0:y1, x0:x1])
        if dm is not None:
            out["displacement"] = dm
            dx, dy = dm["shift"]
            if math.hypot(dx, dy) >= 3 and dm["consistent"] and dm["ratio"] <= 0.4 and dm["err"] <= 1.0:
                out.update(kind="jump", glyphs=0, lines=0, line_h_px1080=0.0, box_color=None)
                return out, bbox

    if Bprev is not None:
        moving = cv2.dilate((motion_diff(B, Bprev, Bprev_aligned) > 15).astype(np.uint8), K3) > 0
        raw2 = raw & ~moving
        if raw2.sum() >= 0.3 * raw.sum():
            closed2 = cv2.morphologyEx(raw2.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)) > 0
            bb2, km2 = components_bbox(closed2, 12)
            if bb2 is not None:
                raw, kmask, bbox = raw2 & km2, km2, bb2
                x0, y0, x1, y1 = bbox
                out["bbox"] = [round(x0 / W, 4), round(y0 / H, 4), round(x1 / W, 4), round(y1 / H, 4)]
                out["center"] = [round((x0 + x1) / 2 / W, 4), round((y0 + y1) / 2 / H, 4)]
    rc, kc = raw[y0:y1, x0:x1], kmask[y0:y1, x0:x1]
    sc = shown[y0:y1, x0:x1]
    my0, mx0, my1, mx1 = max(0, y0 - 4), max(0, x0 - 4), min(H, y1 + 4), min(W, x1 + 4)
    layers = telop_colors(shown[my0:my1, mx0:mx1], raw[my0:my1, mx0:mx1])
    n_lines, line_h, bands = line_bands(kc)
    if n_lines <= 1:
        line_h = float(y1 - y0)  # 1行なら縁まで含めた高さ = 範囲の高さ
    else:  # 複数の帯: 一番画素の多い帯の高さ(下に紛れ込んだ小さな変化に引っ張られない)
        main = max(bands, key=lambda ab: int(kc[ab[0]:ab[1] + 1].sum()))
        line_h = float(main[1] - main[0] + 1)
    glyphs = count_glyphs(rc, bands)
    if layers:
        # 縁取り文字は縁どうしがつながるので、塗りの画素で文字の粒を数える。塗りは
        # 変化した画素の中(普通)・縁に囲まれた穴(白地に白文字)・縁の隙間(細い文字) のどれかにある
        fill = np.array([int(layers[0]["color"][i:i + 2], 16) for i in (1, 3, 5)], np.float32)
        near = np.abs(sc.astype(np.float32) - fill).max(axis=2) < 40
        rcc = cv2.morphologyEx(rc.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)) > 0
        holes = ndimage.binary_fill_holes(rc) & ~rc
        for cand in (rc & near, holes, rcc & near):
            glyphs = max(glyphs, count_glyphs(cand, line_bands(cand)[2]))
    box_color = None
    if is_box(rc):
        inner, box_color = box_inner_mask(sc, kc)
        if inner is not None:
            glyphs = max(glyphs, count_glyphs(inner, line_bands(inner)[2]))
    bw, bh = x1 - x0, y1 - y0
    texty = glyphs >= 3 or (glyphs >= 2 and bw >= 1.5 * bh)

    transient = None
    if A2 is not None and change != "disappear" and rc.sum() >= 20:
        a2 = None if A_aligned2 is None else A_aligned2[y0:y1, x0:x1]
        d2 = motion_diff(A2[y0:y1, x0:x1], A[y0:y1, x0:x1], a2)
        transient = float((d2[rc] > 40).mean())
        out["transient"] = round(transient, 3)
    if moving_before and instant and not texty and area_frac >= 0.02:
        kind = "jump"          # 動いていた人物/アバター部分だけが不連続に飛んだ
    elif transient is not None and transient >= 0.4:
        kind = "motion"        # 出たものがすぐ変わる = 口パク・動き等(テロップではない)
    elif not texty and (moving_before or (peak_frac is not None and peak_frac < 0.35)):
        kind = "motion"        # 元々動いている所の変化 / 瞬きのように変わって戻った変化
    elif texty:
        kind = "text"
    elif box_color is not None:
        kind = "box"
    elif area_frac >= 0.0015:
        kind = "graphic"
    else:
        kind = "minor"         # ごく小さな変化(瞬き・カーソル等)
    out.update({
        "kind": kind, "glyphs": glyphs, "lines": max(1, n_lines),
        "line_h_px1080": round(line_h * ref_scale, 1),
        "box_color": box_color,
    })
    if kind in ("text", "box", "graphic"):
        out["colors"] = layers
        ow = outline_width(sc, rc, layers)
        if ow is not None:
            out["outline_px1080"] = round(ow * ref_scale, 1)
    if kind == "text":
        # テロップか、画面内のUIの文字(チャットの出力など)か: テロップは大きいか、縁取り・座布団がある
        h1080 = out["line_h_px1080"] / max(1, out["lines"])
        outlined = (len(layers) >= 2 and (out.get("outline_px1080") or 0) >= 4
                    and _luma_gap(layers[0]["color"], layers[1]["color"]) >= 80)
        if not (h1080 >= 45 or (h1080 >= 30 and (outlined or box_color))):
            out["kind"] = "ui_text"
    return out, bbox


def measure_telop_hires(A, B, bbox_norm, B_aligned=None, pad=0.12):
    """テロップの見た目(色・縁の太さ・高さ)を高解像度のフレームで測り直す。
    解析解像度(長辺640)では細い文字の塗りが縁と混ざって灰色に見えるため。戻り値の寸法は1080p換算"""
    H, W = A.shape[:2]
    bx0, by0, bx1, by1 = bbox_norm
    bw, bh = bx1 - bx0, by1 - by0
    x0, y0 = max(0, int((bx0 - pad * bw) * W)), max(0, int((by0 - pad * bh) * H))
    x1, y1 = min(W, int((bx1 + pad * bw) * W) + 1), min(H, int((by1 + pad * bh) * H) + 1)
    region = np.zeros((H, W), bool)
    region[y0:y1, x0:x1] = True
    raw, closed, _ = changed_mask(A, B, region, 40, B_aligned)
    bbox, kmask = components_bbox(closed, 30)
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    rc, kc = (raw & kmask)[y0:y1, x0:x1], kmask[y0:y1, x0:x1]
    m = max(4, int(round(8 * max(W, H) / 1920)))  # 背景の色を知るための余白
    mx0, my0, mx1, my1 = max(0, x0 - m), max(0, y0 - m), min(W, x1 + m), min(H, y1 + m)
    layers = telop_colors(A[my0:my1, mx0:mx1], (raw & kmask)[my0:my1, mx0:mx1], px_scale=max(W, H) / 640.0)
    if not layers:
        return None
    scale = 1080.0 / min(W, H)
    n_lines, line_h, bands = line_bands(kc)
    if n_lines <= 1:
        line_h = float(y1 - y0)
    else:
        main = max(bands, key=lambda ab: int(kc[ab[0]:ab[1] + 1].sum()))
        line_h = float(main[1] - main[0] + 1)
    ow = outline_width(A[y0:y1, x0:x1], rc, layers)
    return {"colors": layers, "line_h_px1080": round(line_h * scale, 1),
            "outline_px1080": None if ow is None else round(ow * scale, 1)}


def position_label(cx, cy):
    v = "上" if cy < 0.34 else ("中" if cy < 0.66 else "下")
    hz = "左" if cx < 0.34 else ("中央" if cx < 0.66 else "右")
    return f"{v}・{hz}"


def _lab(hexcol):
    rgb = np.uint8([[[int(hexcol[i:i + 2], 16) for i in (1, 3, 5)]]])
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)[0, 0].astype(np.float32)


def cluster_styles(events):
    """テロップイベントを見た目(位置・文字の高さ・塗り色)で「スタイル」にまとめる"""
    styles = []
    for ev in events:
        o = ev["overlay"]
        # 差し替えイベントの色は前のテロップの跡が混ざるので、色での区別は「出現」だけで行う
        fill = o["colors"][0]["color"] if o.get("colors") and o["change"] == "appear" else None
        for st in styles:
            h0, h1 = st["line_h"], o["line_h_px1080"]
            same_pos = abs(st["cy"] - o["center"][1]) < 0.08 and abs(st["cx"] - o["center"][0]) < 0.25
            same_size = h0 > 0 and 0.7 <= h1 / h0 <= 1.45
            same_color = (fill is None or st["fill"] is None
                          or float(np.linalg.norm(_lab(fill) - _lab(st["fill"]))) < 45)
            if same_pos and same_size and same_color:
                st["members"].append(ev)
                st["fill"] = st["fill"] or fill
                break
        else:
            styles.append({"cy": o["center"][1], "cx": o["center"][0], "line_h": o["line_h_px1080"],
                           "fill": fill, "members": [ev], "ends": []})
    styles.sort(key=lambda s: -len(s["members"]))
    return styles


class PersistAccumulator:
    """ショットをまたいで同じ場所に出続ける要素(ロゴ・常設の見出し帯など)を探す"""

    def __init__(self, size=(320, 180)):
        self.size = size
        self.n = 0
        w, h = size
        self.sum = np.zeros((h, w, 3), np.float64)
        self.sq = np.zeros((h, w, 3), np.float64)
        self.edges = np.zeros((h, w), np.float64)

    def add(self, rgb):
        img = cv2.resize(rgb, self.size, interpolation=cv2.INTER_AREA).astype(np.float64)
        self.sum += img
        self.sq += img * img
        self.edges += cv2.Canny(img.astype(np.uint8).mean(axis=2).astype(np.uint8), 60, 150) > 0
        self.n += 1

    def result(self, min_frames=6):
        if self.n < min_frames:
            return []
        w, h = self.size
        mean = self.sum / self.n
        std = np.sqrt(np.maximum(self.sq / self.n - mean ** 2, 0)).max(axis=2)
        structure = cv2.dilate((self.edges / self.n >= 0.7).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        m = ((std < 10) & structure).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        out = []
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if area < 0.001 * w * h:
                continue
            color = mean[lab == i].mean(axis=0)
            out.append({"bbox": [round(float(v), 4) for v in (x / w, y / h, (x + bw) / w, (y + bh) / h)],
                        "area_frac": round(float(area) / (w * h), 4), "mean_color": _hex(color),
                        "position": position_label((x + bw / 2) / w, (y + bh / 2) / h)})
        return sorted(out, key=lambda o: -o["area_frac"])
