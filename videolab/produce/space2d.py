"""宇宙シーンの2.5D描画エンジン(numpy/OpenCVのみ。Blender・GPU不要)。

窓口は space.make_space_source(engine="2d")。テンプレートとパラメータの定義は space.py。
(互換のため make_space_source はこのモジュールからも import できる)

仕組み(毎フレームの処理を最小にする):
  初期化時  : 惑星の地表テクスチャ(正距円筒図法)を球面上の3次元バリューノイズの
              多重オクターブで生成(同じ種・解像度ならプロセス内で再利用) /
              星空レイヤー(遠・近の2枚。上下左右がつながる)と明るい星のリストを描画 /
              惑星スプライト(天体を中心に置いた小画像)上の球の幾何
              (緯度経度・法線・接ベクトル)と大気・環を計算
  毎フレーム: 自転分だけ経度をずらして cv2.remap でテクスチャを貼る → 陰影・雲・夜景・大気を合成 →
              カメラワークに合わせてスプライトを拡大/移動して星空に重ねる

座標の約束: カメラ座標は x右 / y上 / z手前。惑星座標は y=北極、経度0が z方向、東=+x。
"""

import math
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import cv2
import numpy as np

from .sources import CameraMove, FrameSource, load_image_rgb
from .space import (CAMERA_VFOV_DEG, PRESETS, STAR_PARALLAX, camera_state, compare_layout,
                    hex_rgb, make_space_source, resolve_params, sun_vector)

__all__ = ["Space2DSource", "make_space_source"]

F32 = np.float32
TAU = 2.0 * math.pi

# numpyの演算は大きな配列ではGILを離すので、画素を分割してスレッドで並列に計算する
_WORKERS = max(1, min(8, os.cpu_count() or 1))
_POOL = None


def _parallel(fn, n: int, min_chunk: int = 40000):
    """0〜n を分割して fn(slice) をスレッドで並列実行する(各要素は独立に計算=結果は決定的)。"""
    global _POOL
    k = min(_WORKERS, max(1, n // min_chunk))
    if k <= 1:
        fn(slice(0, n))
        return
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="space2d")
    b = np.linspace(0, n, k + 1).astype(np.int64)
    futs = [_POOL.submit(fn, slice(int(b[j]), int(b[j + 1]))) for j in range(k)]
    for f in futs:
        f.result()


# ================================================================ 基本部品

def _smooth(e0, e1, x):
    """smoothstep(e0→e1)。e0>e1 なら逆向き。"""
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _mix(a, b, t):
    return a + (b - a) * t


def _rgb(c) -> np.ndarray:
    return np.asarray(hex_rgb(c), F32)


def _ramp(x: np.ndarray, stops) -> np.ndarray:
    """値xを色の段階 [(位置, '#rrggbb'), ...] で色に変える → (..., 3)。"""
    pos = np.array([s[0] for s in stops], np.float64)
    cols = np.array([hex_rgb(s[1]) for s in stops], np.float64)
    out = np.empty(x.shape + (3,), F32)
    for c in range(3):
        out[..., c] = np.interp(x, pos, cols[:, c])
    return out


def _tonemap(x: np.ndarray) -> np.ndarray:
    """0.75までは素通し、それより明るい所はなめらかに1へ寄せる(白飛びを柔らかく)。"""
    k = 0.75
    over = np.maximum(x - k, 0.0)
    return np.minimum(x, k) + (1.0 - k) * (1.0 - np.exp(-over / (1.0 - k)))


def _rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], F32)


def _rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], F32)


def _rand_rot(rng, y_only=False) -> np.ndarray:
    if y_only:
        a = rng.uniform(0, TAU)
        c, s = math.cos(a), math.sin(a)
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], F32)
    q, r = np.linalg.qr(rng.normal(size=(3, 3)))
    q = q * np.sign(np.diag(r))
    if np.linalg.det(q) < 0:
        q[:, 0] = -q[:, 0]
    return q.astype(F32)


# ---------------------------------------------------------------- 3次元バリューノイズ

_TN = 64                     # 格子ハッシュ表の一辺(周期)。オクターブ毎に回転させるので繰り返しは目立たない
_TMASK = _TN - 1


@lru_cache(maxsize=64)
def _lattice(seed: int) -> np.ndarray:
    rng = np.random.default_rng([seed & 0x7FFFFFFF, 90210])
    return rng.random(_TN ** 3, dtype=F32)


def vnoise3(p: np.ndarray, seed: int) -> np.ndarray:
    """3次元バリューノイズ(0〜1, 平均0.5)。p: (..., 3) float32。"""
    tab = _lattice(int(seed))
    shape = p.shape[:-1]
    flat = p.reshape(-1, 3)
    out = np.empty(len(flat), F32)

    def work(sl):
        out[sl] = _vnoise3_core(flat[sl], tab)

    _parallel(work, len(flat), 65536)
    return out.reshape(shape)


def _vnoise3_core(p: np.ndarray, tab: np.ndarray) -> np.ndarray:
    fl = np.floor(p)
    f = (p - fl).astype(F32)
    f = f * f * (3.0 - 2.0 * f)
    i = fl.astype(np.int32) & _TMASK
    x0 = i[..., 0] * (_TN * _TN)
    x1 = ((i[..., 0] + 1) & _TMASK) * (_TN * _TN)
    y0 = i[..., 1] * _TN
    y1 = ((i[..., 1] + 1) & _TMASK) * _TN
    z0 = i[..., 2]
    z1 = (z0 + 1) & _TMASK
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    a = x0 + y0
    b = x1 + y0
    c = x0 + y1
    d = x1 + y1
    v00 = _mix(tab[a + z0], tab[b + z0], fx)
    v10 = _mix(tab[c + z0], tab[d + z0], fx)
    v01 = _mix(tab[a + z1], tab[b + z1], fx)
    v11 = _mix(tab[c + z1], tab[d + z1], fx)
    return _mix(_mix(v00, v10, fy), _mix(v01, v11, fy), fz)


@lru_cache(maxsize=12)
def _sphere_dirs(w: int) -> np.ndarray:
    """正距円筒図法(横w×縦w/2)の各画素中心の球面上の方向 (h, w, 3)。"""
    h = w // 2
    lon = ((np.arange(w, dtype=np.float64) + 0.5) / w) * TAU - math.pi
    lat = math.pi / 2 - ((np.arange(h, dtype=np.float64) + 0.5) / h) * math.pi
    d = np.empty((h, w, 3), F32)
    cl = np.cos(lat)[:, None]
    d[..., 0] = cl * np.sin(lon)[None, :]
    d[..., 1] = np.sin(lat)[:, None]
    d[..., 2] = cl * np.cos(lon)[None, :]
    d.setflags(write=False)
    return d


def _lat_grid(tw: int) -> np.ndarray:
    """テクスチャ各行の緯度(ラジアン) (th, 1)。"""
    th = tw // 2
    return (math.pi / 2 - ((np.arange(th, dtype=F32) + 0.5) / th) * math.pi)[:, None]


def _lon_grid(tw: int) -> np.ndarray:
    return (((np.arange(tw, dtype=F32) + 0.5) / tw) * TAU - math.pi)[None, :]


def _resize_wrap(v: np.ndarray, tw: int) -> np.ndarray:
    """正距円筒の画像を横tw×縦tw/2へ。左右の継ぎ目がつながるように拡大する。"""
    hs, ws = v.shape[:2]
    if ws == tw:
        return v
    if ws > tw:
        return cv2.resize(v, (tw, tw // 2), interpolation=cv2.INTER_AREA)
    k = tw // ws
    pad = 2
    vp = np.concatenate([v[:, -pad:], v, v[:, :pad]], axis=1)
    big = cv2.resize(vp, ((ws + 2 * pad) * k, tw // 2), interpolation=cv2.INTER_CUBIC)
    return big[:, pad * k: pad * k + tw]


def sphere_fbm(tw: int, freq: float, octaves: int, seed: int, gain: float = 0.5,
               lacunarity: float = 2.0, stretch=(1.0, 1.0, 1.0), warp: np.ndarray = None,
               ridged: bool = False, y_only_rot: bool = False, base: np.ndarray = None) -> np.ndarray:
    """球面上のフラクタルノイズを正距円筒(横tw)で返す(0〜1, 平均0.5付近)。

    低い周波数のオクターブは低解像度で計算して拡大するので、細かい所だけが重い。
    stretch: 惑星座標(x, y=北極, z)ごとの周波数倍率。y>1で横縞(木星の帯)になる
    warp   : 方向ベクトルに足すゆがみ場 (h, w, 3)。大陸・雲の渦の見た目になる
    """
    rng = np.random.default_rng([seed & 0x7FFFFFFF, 777])
    th = tw // 2
    total = np.zeros((th, tw), F32) if base is None else base
    amp, norm = 1.0, 0.0
    st = np.asarray(stretch, F32)
    smax = float(st.max())
    for o in range(octaves):
        f = freq * lacunarity ** o
        if o > 0 and TAU * f * smax > tw / 2.5:       # テクスチャで表せない細かさは省く
            break
        res = 64
        while res < tw and res < 8.0 * math.pi * f * smax:
            res *= 2
        res = min(res, tw)
        d = _sphere_dirs(res)
        if warp is not None:
            d = d + _resize_wrap(warp, res)
        rot = _rand_rot(rng, y_only_rot)
        p = (d.reshape(-1, 3) @ rot.T) * (st * F32(f)) + rng.uniform(0, 64, 3).astype(F32)
        v = vnoise3(p.reshape(d.shape), seed * 131 + o * 7919 + 1).reshape(res // 2, res)
        if ridged:
            v = 1.0 - np.abs(2.0 * v - 1.0)
        total += F32(amp) * _resize_wrap(v, tw)
        norm += amp
        amp *= gain
    return total / F32(norm)


def _warp_field(seed: int, freq: float, strength: float, res: int = 128, octaves: int = 4):
    return np.stack([sphere_fbm(res, freq, octaves, seed + k) - 0.5 for k in range(3)],
                    axis=-1) * F32(strength * 2.0)


def _stamp_craters(tw: int, rng, n: int, rmin: float, rmax: float, height: np.ndarray,
                   bright: np.ndarray, depth: float = 0.18, fresh: float = 0.0, rays: int = 0,
                   lat_limit: float = None):
    """クレーター(椀状の凹み+縁の盛り上がり)を高さマップに刻む。r はラジアン。"""
    th = tw // 2
    lat_rows = (math.pi / 2 - ((np.arange(th) + 0.5) / th) * math.pi)
    lon_cols = (((np.arange(tw) + 0.5) / tw) * TAU - math.pi)
    order = np.arange(n)
    for k in order:
        z = rng.uniform(-1, 1)
        lat0 = math.asin(z)
        if lat_limit is not None and abs(lat0) > lat_limit:
            continue
        lon0 = rng.uniform(-math.pi, math.pi)
        r = rmin * (rmax / rmin) ** (rng.random() ** 2.4)
        is_ray = k < rays
        ext = r * (6.0 if is_ray else 2.4)
        r0 = int(max(0, math.floor((math.pi / 2 - (lat0 + ext)) / math.pi * th)))
        r1 = int(min(th, math.ceil((math.pi / 2 - (lat0 - ext)) / math.pi * th) + 1))
        if r1 <= r0:
            continue
        rows = np.arange(r0, r1)
        lat = lat_rows[rows]
        cmin = max(float(np.cos(lat).min()), 1e-3)
        dl = ext / cmin
        if dl >= math.pi:
            cols = np.arange(tw)
        else:
            c0 = int(math.floor((lon0 - dl + math.pi) / TAU * tw))
            c1 = int(math.ceil((lon0 + dl + math.pi) / TAU * tw)) + 1
            cols = np.arange(c0, c1) % tw
        lon = lon_cols[cols]
        cosd = (math.sin(lat0) * np.sin(lat))[:, None] + \
            (math.cos(lat0) * np.cos(lat))[:, None] * np.cos(lon[None, :] - lon0)
        x = (np.arccos(np.clip(cosd, -1.0, 1.0)) / r).astype(F32)
        prof = np.where(x < 1.0, np.maximum(x * x - 1.0, -0.75), 0.0)
        prof += 0.30 * np.exp(-((x - 1.0) / 0.17) ** 2)
        prof += np.where(x > 1.0, 0.07 * np.exp(-(x - 1.0) / 0.5), 0.0)
        if r > 0.35 * rmax:
            prof += 0.30 * np.exp(-(x / 0.13) ** 2)            # 中央丘
        height[np.ix_(rows, cols)] += (prof * r * depth).astype(F32)
        if bright is not None:
            b = fresh * (0.5 * np.exp(-((x - 1.0) / 0.25) ** 2) + 0.25 * (x < 1.0))
            if is_ray:
                ang = np.arctan2((np.cos(lat)[:, None] * np.sin(lon[None, :] - lon0)),
                                 (np.sin(lat)[:, None] - math.sin(lat0) * cosd))
                rr = np.random.default_rng(int(k) + 5).random(64)
                ray = np.interp((ang + math.pi) / TAU * 64, np.arange(65), np.r_[rr, rr[:1]])
                b = b + 0.55 * (ray ** 4) * np.exp(-np.maximum(x - 1.0, 0) / 1.8) * (x > 0.9)
            bright[np.ix_(rows, cols)] += b.astype(F32)


def _height_to_bump(height: np.ndarray) -> np.ndarray:
    """高さマップ → 東向き・北向きの傾き (th, tw, 2)(単位: 半径あたり)。"""
    th, tw = height.shape
    lat = _lat_grid(tw)
    dl = TAU / tw
    dp = math.pi / th
    ge = (np.roll(height, -1, axis=1) - np.roll(height, 1, axis=1)) / (2 * dl)
    ge /= np.maximum(np.cos(lat), 0.05)
    gn = np.zeros_like(height)
    gn[1:-1] = (height[:-2] - height[2:]) / (2 * dp)
    return np.stack([ge, gn], axis=-1).astype(F32)


def _u8(x: np.ndarray) -> np.ndarray:
    return np.clip(x * 255.0 + 0.5, 0, 255).astype(np.uint8)


_REMAP_COLS = 4096      # cv2.remap は一辺32767px未満の制限があるので、1次元の座標列は折り返して渡す


def remap_flat(tex: np.ndarray, mx: np.ndarray, my: np.ndarray, interp=cv2.INTER_LINEAR,
               border=cv2.BORDER_WRAP) -> np.ndarray:
    """1次元に並べた座標列 (mx, my) でテクスチャを標本化 → (N, C)。"""
    n = len(mx)
    rows = max(1, -(-n // _REMAP_COLS))
    pad = rows * _REMAP_COLS - n
    mx2 = np.concatenate([mx.astype(F32), np.zeros(pad, F32)]).reshape(rows, _REMAP_COLS)
    my2 = np.concatenate([my.astype(F32), np.zeros(pad, F32)]).reshape(rows, _REMAP_COLS)
    out = cv2.remap(tex, mx2, my2, interp, borderMode=border)
    return out.reshape(rows * _REMAP_COLS, -1)[:n]


# ================================================================ 惑星テクスチャ

class PlanetTexture:
    """正距円筒の地表データ一式。surface=RGB反射率+A鏡面度 / bump / clouds / emission。"""

    def __init__(self, tw, surface, bump=None, clouds=None, emission=None, emission_day=0.0):
        self.tw = tw
        self.surface = surface
        self.bump = bump
        self.clouds = clouds
        self.emission = emission
        self.emission_day = emission_day


def _band_lut(bands, colors: dict, rng, n: int = 2048, sigma_deg: float = 1.1, jitter=0.05):
    """緯度→色の表。bands: [(南端の緯度, 色キー), ...] を北から順に。"""
    lat = np.linspace(90, -90, n)
    out = np.zeros((n, 3), np.float64)
    top = 90.0
    for lower, key in bands:
        m = (lat <= top) & (lat > lower)
        col = np.array(hex_rgb(colors[key])) * (1.0 + rng.uniform(-jitter, jitter))
        out[m] = col
        top = lower
    out[lat <= top] = out[lat > top][-1] if (lat > top).any() else 0.5
    k = int(sigma_deg / (180.0 / n) * 3) * 2 + 1
    out = cv2.GaussianBlur(out.astype(F32)[:, None, :], (1, k), 0, sigmaY=sigma_deg / (180.0 / n))[:, 0]
    fine = np.zeros(n)
    for fr in (37, 71, 131):
        fine += np.sin(np.radians(lat) * fr + rng.uniform(0, TAU)) / 3
    return np.clip(out * (1.0 + 0.035 * fine[:, None]), 0, 1).astype(F32)


def _lut_sample(lut: np.ndarray, lat_deg: np.ndarray) -> np.ndarray:
    n = len(lut)
    idx = np.clip((90.0 - lat_deg) / 180.0 * (n - 1), 0, n - 1)
    i0 = np.floor(idx).astype(np.int32)
    i1 = np.minimum(i0 + 1, n - 1)
    f = (idx - i0)[..., None].astype(F32)
    return lut[i0] * (1 - f) + lut[i1] * f


def _oval(lat, lon, lat0, lon0, dlat, dlon):
    """楕円の嵐(緯度経度で)の中心からの正規化距離。"""
    dl = np.angle(np.exp(1j * (lon - lon0)))
    return np.sqrt(((lat - lat0) / dlat) ** 2 + (dl / dlon) ** 2).astype(F32)


def _tex_earth(tw, seed, c, pr, clouds=True, night_lights=True):
    lat = np.degrees(_lat_grid(tw))
    alat = np.abs(lat)
    warp = _warp_field(seed + 11, 1.3, 0.32)
    h = sphere_fbm(tw, 1.05, 9, seed + 1, gain=0.53, warp=warp)
    sl = float(np.quantile(h[::6, ::6], 0.67))
    hmax = float(np.quantile(h[::6, ::6], 0.998))
    land = _smooth(sl - 0.002, sl + 0.002, h)
    depth = np.clip((sl - h) / 0.1, 0, 1)
    ocean = _mix(_rgb(c["ocean_shallow"]), _rgb(c["ocean"]), _smooth(0.0, 0.25, depth)[..., None])
    ocean = _mix(ocean, _rgb(c["ocean_deep"]), _smooth(0.25, 1.0, depth)[..., None])
    elev = np.clip((h - sl) / (hmax - sl), 0, 1)
    moist = sphere_fbm(tw, 2.1, 6, seed + 3, warp=warp * 0.6)
    detail = sphere_fbm(tw, 14.0, 4, seed + 4)
    col = _mix(_rgb(c["land_low"]), _rgb(c["land_forest"]),
               _smooth(0.5, 0.62, moist + 0.1 * _smooth(18, 5, alat))[..., None])
    dry = _smooth(0.48, 0.38, moist) * np.exp(-((alat - 24) / 13) ** 2)
    dry = np.maximum(dry, _smooth(0.42, 0.32, moist) * 0.6)
    col = _mix(col, _rgb(c["desert"]), np.clip(dry, 0, 1)[..., None])
    col = _mix(col, _rgb(c["land_high"]), _smooth(0.3, 0.75, elev)[..., None])
    col = _mix(col, _rgb(c["tundra"]), _smooth(52, 66, alat + 10 * (detail - 0.5))[..., None])
    col *= (0.88 + 0.24 * detail)[..., None]
    alb = _mix(ocean, col, land[..., None])
    ice_line = alat + 9 * (moist - 0.5) + 5 * (detail - 0.5)
    ice = np.maximum(_smooth(70, 76, ice_line), _smooth(-62, -68, lat + 4 * (detail - 0.5)))
    ice = np.maximum(ice, land * _smooth(0.82, 0.95, elev) * 0.8)
    alb = _mix(alb, _rgb(c["ice"]), ice[..., None])
    spec = (1.0 - land) * (1.0 - ice)
    height = (np.maximum(h - sl, 0) * 0.05 + (detail - 0.5) * 0.004 * land).astype(F32)
    surface = np.dstack([_u8(alb), _u8(spec)])
    cl = None
    if clouds:
        warp2 = _warp_field(seed + 21, 1.8, 0.55)
        cv = sphere_fbm(tw, 2.4, 8, seed + 5, gain=0.55, warp=warp2)
        bias = (0.05 * np.exp(-(lat / 7) ** 2) - 0.07 * np.exp(-((alat - 24) / 9) ** 2)
                + 0.05 * np.exp(-((alat - 55) / 12) ** 2))
        cl = _u8(_smooth(0.5, 0.72, cv + bias) * 0.95)
    em = None
    if night_lights:
        cities = sphere_fbm(tw, 45.0, 3, seed + 7)
        coast = np.exp(-np.maximum(h - sl, 0) / 0.02)
        pop = land * (1 - ice) * _smooth(62, 48, alat) * (1 - np.clip(dry * 1.3, 0, 1))
        lights = _smooth(0.6, 0.78, cities + 0.12 * coast) * pop * (0.35 + 0.65 * coast)
        em = _u8(lights[..., None] * _rgb(c["city"]) * 0.9)
    return PlanetTexture(tw, surface, _height_to_bump(height), cl, em, 0.0)


def _tex_rocky(tw, seed, c, pr):
    rng = np.random.default_rng(seed + 100)
    lat = np.degrees(_lat_grid(tw))
    dirs_z = _sphere_dirs(tw)[..., 2]
    warp = _warp_field(seed + 31, 1.0, 0.25)
    big = sphere_fbm(tw, 0.9, 5, seed + 1, warp=warp)
    detail = sphere_fbm(tw, 5.0, 6, seed + 2, gain=0.55)
    fine = sphere_fbm(tw, 40.0, 3, seed + 3)
    moon = pr["craters"] >= 0.9
    if moon:
        mare = _smooth(0.57, 0.63, big + 0.13 * dirs_z)
        col = _mix(_rgb(c["base"]), _rgb(c["light"]), _smooth(0.4, 0.7, detail)[..., None])
        col = _mix(col, _rgb(c["dark"]), (mare * 0.9)[..., None])
    else:
        dark = _smooth(0.46, 0.36, big)
        light = _smooth(0.6, 0.72, big)
        col = _mix(_rgb(c["base"]), _rgb(c["light"]), (light * 0.8)[..., None])
        col = _mix(col, _rgb(c["dark"]), (dark * 0.85)[..., None])
        col = _mix(col, _rgb(c["dark"]), (_smooth(0.62, 0.8, detail) * 0.3)[..., None])
        mare = dark
    col *= (0.86 + 0.28 * detail + 0.1 * (fine - 0.5))[..., None]
    height = ((detail - 0.5) * 0.012 + (fine - 0.5) * 0.0012).astype(F32)
    bright = np.zeros_like(height)
    n = int(1300 * pr["craters"] * (tw / 2048) ** 0.5)
    _stamp_craters(tw, rng, n, 0.004 * 2048 / tw * 0.7 + 0.003, 0.11, height, bright,
                   depth=0.2, fresh=0.35 if moon else 0.12, rays=3 if moon else 0)
    col *= (1.0 + bright * (1.0 - 0.6 * mare))[..., None]
    if pr.get("polar_caps"):
        cap = _smooth(76, 82, np.abs(lat) + 5 * (detail - 0.5))
        col = _mix(col, _rgb(c["ice"]), cap[..., None])
    col = np.clip(col, 0, 1)
    surface = np.dstack([_u8(col), np.zeros(col.shape[:2], np.uint8)])
    return PlanetTexture(tw, surface, _height_to_bump(height))


_GAS_BANDS = {
    "jupiter": [(62, "pole"), (48, "zone2"), (41, "belt"), (35, "zone"), (29, "belt"),
                (20, "zone"), (7, "belt_dark"), (-6, "zone2"), (-19, "belt"), (-27, "zone"),
                (-33, "belt"), (-40, "zone2"), (-47, "belt_dark"), (-60, "zone2"), (-90, "pole")],
    "saturn": [(70, "pole"), (55, "zone2"), (45, "belt"), (38, "zone"), (30, "belt_dark"),
               (18, "zone2"), (-18, "zone"), (-30, "belt"), (-38, "zone2"), (-46, "belt_dark"),
               (-56, "zone2"), (-70, "belt"), (-90, "pole")],
    "neptune": [(70, "pole"), (55, "zone2"), (40, "zone"), (28, "belt"), (10, "zone2"),
                (-12, "zone"), (-28, "belt"), (-45, "zone2"), (-62, "belt_dark"), (-90, "pole")],
}


def _tex_gas(tw, seed, c, pr, name):
    rng = np.random.default_rng(seed + 200)
    lat = np.degrees(_lat_grid(tw))
    lon = _lon_grid(tw)
    lut = _band_lut(_GAS_BANDS.get(name, _GAS_BANDS["jupiter"]), c, rng,
                    sigma_deg=0.9 if name == "jupiter" else 1.6,
                    jitter=0.06 if name == "jupiter" else 0.03)
    turb = 4.0 if name == "jupiter" else 1.6
    pert = sphere_fbm(tw, 2.2, 7, seed + 1, stretch=(1.0, 4.5, 1.0), y_only_rot=True, gain=0.55)
    lat_p = lat + turb * 2.0 * (pert - 0.5)
    col = _lut_sample(lut, lat_p)
    streak = sphere_fbm(tw, 6.0, 5, seed + 2, stretch=(1.0, 7.0, 1.0), y_only_rot=True)
    col *= (0.9 + 0.2 * streak)[..., None]
    lat2 = np.broadcast_to(lat, (tw // 2, tw))
    lon2 = np.broadcast_to(lon, (tw // 2, tw))
    if pr.get("storm") and name == "jupiter":
        d = _oval(lat2, lon2, -22.0, 0.0, 5.2, math.radians(11.0))
        swirl = sphere_fbm(tw, 18.0, 3, seed + 3)
        core = _smooth(1.0, 0.55, d + 0.25 * (swirl - 0.5))
        spot = _mix(_rgb(c["storm"]), _rgb(c["storm"]) * 1.25, _smooth(0.2, 0.7, swirl)[..., None])
        col = _mix(col, _rgb(c["zone"]), (_smooth(1.6, 1.05, d) * (1 - core) * 0.7)[..., None])
        col = _mix(col, spot, core[..., None])
        for k in range(6):
            lo = math.radians(rng.uniform(-180, 180))
            la = rng.choice([-39.0, -41.5, 33.0, -52.0])
            dd = _oval(lat2, lon2, la, lo, 1.4, math.radians(2.6 * rng.uniform(0.7, 1.3)))
            col = _mix(col, _rgb(c["oval"]), (_smooth(1.0, 0.5, dd) * 0.9)[..., None])
    if name == "neptune":
        cirrus = sphere_fbm(tw, 3.5, 5, seed + 4, stretch=(1.0, 9.0, 1.0), y_only_rot=True)
        band = np.exp(-((np.abs(lat) - 30) / 10) ** 2) + 0.5 * np.exp(-((lat + 55) / 6) ** 2)
        col = _mix(col, _rgb(c["oval"]), (_smooth(0.62, 0.78, cirrus) * band * 0.85)[..., None])
        if pr.get("storm"):
            d = _oval(lat2, lon2, -20.0, 0.0, 6.0, math.radians(13.0))
            col = _mix(col, _rgb(c["storm"]), (_smooth(1.0, 0.6, d) * 0.85)[..., None])
            dd = _oval(lat2, lon2, -26.0, math.radians(4.0), 2.0, math.radians(8.0))
            col = _mix(col, _rgb(c["oval"]), (_smooth(1.0, 0.4, dd) * 0.8)[..., None])
    col = np.clip(col, 0, 1)
    surface = np.dstack([_u8(col), np.zeros(col.shape[:2], np.uint8)])
    return PlanetTexture(tw, surface)


def _tex_venus(tw, seed, c, pr):
    lat = np.degrees(_lat_grid(tw))
    warp = _warp_field(seed + 41, 1.4, 0.45)
    v = sphere_fbm(tw, 1.6, 7, seed + 1, stretch=(1.0, 2.2, 1.0), warp=warp, gain=0.55)
    col = _ramp(v, [(0.25, c["dark"]), (0.5, c["base"]), (0.72, c["light"])])
    col *= (0.93 + 0.1 * np.cos(np.radians(lat) * 3.0))[..., None]
    surface = np.dstack([_u8(np.clip(col, 0, 1)), np.zeros(col.shape[:2], np.uint8)])
    return PlanetTexture(tw, surface)


def _tex_ice(tw, seed, c, pr):
    rng = np.random.default_rng(seed + 300)
    base = sphere_fbm(tw, 2.0, 6, seed + 1)
    col = _ramp(base, [(0.3, c["dark"]), (0.5, c["base"]), (0.7, c["light"])])
    col = _mix(col, _rgb(c["blue"]), (_smooth(0.55, 0.7, sphere_fbm(tw, 1.5, 4, seed + 2)) * 0.5)[..., None])
    height = np.zeros(col.shape[:2], F32)
    lines = np.zeros(col.shape[:2], F32)
    for k, (fr, w) in enumerate(((1.6, 0.975), (3.2, 0.972), (6.0, 0.965))):
        r = sphere_fbm(tw, fr, 3, seed + 10 + k, ridged=True, gain=0.4)
        ln = _smooth(w - 0.03, w, r)
        lines = np.maximum(lines, ln * (1.0 - 0.25 * k))
        height += ln * 0.002
    chaos = _smooth(0.66, 0.72, sphere_fbm(tw, 3.0, 5, seed + 5))
    col = _mix(col, _rgb(c["line"]), (lines * 0.75)[..., None])
    col = _mix(col, _rgb(c["line"]) * 1.1, (chaos * 0.45)[..., None])
    bright = np.zeros_like(height)
    _stamp_craters(tw, rng, 25, 0.005, 0.03, height, bright, depth=0.1, fresh=0.2)
    col = np.clip(col * (1 + bright)[..., None], 0, 1)
    surface = np.dstack([_u8(col), _u8(0.25 * (1 - lines))])
    return PlanetTexture(tw, surface, _height_to_bump(height))


def _tex_lava(tw, seed, c, pr):
    rng = np.random.default_rng(seed + 400)
    base = sphere_fbm(tw, 2.5, 6, seed + 1)
    col = _ramp(base, [(0.3, c["crust"]), (0.55, c["crust2"]), (0.75, c["ash"])])
    r1 = sphere_fbm(tw, 1.8, 4, seed + 2, ridged=True, gain=0.45)
    r2 = sphere_fbm(tw, 6.5, 3, seed + 3, ridged=True, gain=0.45)
    crack = np.maximum(_smooth(0.9, 0.975, r1), 0.7 * _smooth(0.92, 0.98, r2))
    hot = _smooth(0.975, 0.995, r1)
    lakes = np.zeros_like(base)
    _stamp_craters(tw, rng, 18, 0.01, 0.05, lakes, None, depth=1.0)
    lake = _smooth(-0.004, -0.012, lakes)
    glow = np.clip(crack + lake, 0, 1)
    col = _mix(col, _rgb(c["crust"]) * 0.6, glow[..., None])
    em = glow[..., None] * _rgb(c["glow"]) + (hot + lake * 0.6)[..., None] * _rgb(c["hot"]) * 0.8
    height = ((base - 0.5) * 0.01 - glow * 0.004).astype(F32)
    surface = np.dstack([_u8(np.clip(col, 0, 1)), _u8(0.15 * glow)])
    return PlanetTexture(tw, surface, _height_to_bump(height), None, _u8(np.clip(em, 0, 1)), 0.55)


@lru_cache(maxsize=6)
def planet_texture(preset: str, seed: int, tw: int, clouds: bool, night_lights: bool,
                   texture_path: str = None) -> PlanetTexture:
    """プリセット(または画像)から地表テクスチャを作る。同じ引数なら使い回す。"""
    pr = PRESETS[preset]
    c = pr["colors"]
    if texture_path:
        img = load_image_rgb(texture_path)
        img = cv2.resize(img, (tw, tw // 2), interpolation=cv2.INTER_AREA
                         if img.shape[1] > tw else cv2.INTER_CUBIC)
        spec = np.zeros(img.shape[:2], np.uint8)
        if pr["kind"] == "earth":   # 青い所(海)を鏡面反射させる
            f = img.astype(F32) / 255
            spec = _u8(_smooth(0.05, 0.12, f[..., 2] - np.maximum(f[..., 0], f[..., 1])))
        tex = PlanetTexture(tw, np.dstack([img, spec]))
        if clouds or night_lights:
            proc = _tex_earth(tw, seed, c, pr, clouds, night_lights)
            tex.clouds, tex.emission = proc.clouds, proc.emission
        return tex
    kind = pr["kind"]
    if kind == "earth":
        return _tex_earth(tw, seed, c, pr, clouds, night_lights)
    if kind == "rocky":
        return _tex_rocky(tw, seed, c, pr)
    if kind == "gas":
        return _tex_gas(tw, seed, c, pr, preset)
    if kind == "cloudy":
        return _tex_venus(tw, seed, c, pr)
    if kind == "ice":
        return _tex_ice(tw, seed, c, pr)
    if kind == "lava":
        return _tex_lava(tw, seed, c, pr)
    raise ValueError(f"未対応のプリセット種別: {kind}")


# ================================================================ 球の幾何(スプライト)

class _SphereGeom:
    """スプライト(中心=天体の中心)上の球の幾何。1回だけ計算して毎フレーム使う。

    R: 半径(px) / half_w, half_h: スプライトの半分の大きさ(px) / M: 惑星→カメラの回転
    円盤内の画素だけを1次元に並べて持つ(idx)。
    """

    def __init__(self, R: float, half_w: int, half_h: int, M: np.ndarray):
        self.R, self.half_w, self.half_h = R, half_w, half_h
        W, H = 2 * half_w, 2 * half_h
        self.W, self.H = W, H
        xs = ((np.arange(W, dtype=F32) + 0.5) - half_w) / F32(R)
        ys = (half_h - (np.arange(H, dtype=F32) + 0.5)) / F32(R)
        self.X, self.Y = np.meshgrid(xs, ys)
        self.D = np.sqrt(self.X * self.X + self.Y * self.Y)
        cover = np.clip((1.0 - self.D) * F32(R) + 0.5, 0.0, 1.0)
        self.idx = np.flatnonzero(cover.ravel() > 0)
        self.cover = cover.ravel()[self.idx]
        x = self.X.ravel()[self.idx]
        y = self.Y.ravel()[self.idx]
        dd = self.D.ravel()[self.idx]
        k = np.where(dd > 1.0, 1.0 / np.maximum(dd, 1e-6), 1.0).astype(F32)
        x, y = x * k, y * k
        z = np.sqrt(np.clip(1.0 - x * x - y * y, 0.0, 1.0))
        self.n = np.stack([x, y, z])                          # (3, N) カメラ座標の法線
        self.nz = z
        npl = M.T @ self.n
        self.npl = npl
        lat = np.arcsin(np.clip(npl[1], -1.0, 1.0))
        lon = np.arctan2(npl[0], npl[2])
        self.u0 = ((lon + math.pi) / TAU).astype(F32)
        self.v0 = ((math.pi / 2 - lat) / math.pi).astype(F32)
        cl = np.sqrt(npl[0] ** 2 + npl[2] ** 2) + F32(1e-6)
        east_p = np.stack([npl[2] / cl, np.zeros_like(cl), -npl[0] / cl])
        north_p = np.stack([-npl[1] * npl[0] / cl, cl, -npl[1] * npl[2] / cl])
        self.east = (M @ east_p).astype(F32)
        self.north = (M @ north_p).astype(F32)

    def sample(self, tex: np.ndarray, lon_offset: float, interp=cv2.INTER_LINEAR) -> np.ndarray:
        """経度をずらしてテクスチャを貼る → (N, C) の配列。"""
        th, tw = tex.shape[:2]
        u = self.u0 + F32(lon_offset / TAU)
        u -= np.floor(u)
        return remap_flat(tex, u * tw - 0.5, self.v0 * th - 0.5, interp)


# ================================================================ 星空

_STAR_T = np.array([2800, 3500, 4500, 5500, 6500, 8000, 10000, 15000, 25000], np.float64)
_STAR_RGB = np.array([[1.0, 0.60, 0.36], [1.0, 0.72, 0.50], [1.0, 0.85, 0.70], [1.0, 0.94, 0.87],
                      [1.0, 1.0, 1.0], [0.88, 0.92, 1.0], [0.78, 0.85, 1.0], [0.68, 0.78, 1.0],
                      [0.62, 0.72, 1.0]], np.float64)


def star_colors(rng, n: int, variation: float) -> np.ndarray:
    """色温度のばらつきを持つ星の色 (n, 3)。"""
    temp = np.exp(rng.normal(math.log(6500), 0.38, n)).clip(2800, 25000)
    rgb = np.stack([np.interp(temp, _STAR_T, _STAR_RGB[:, c]) for c in range(3)], axis=1)
    k = min(1.0, variation * 1.5)
    return (1.0 + (rgb - 1.0) * k).astype(F32)


def _blur_wrap(img: np.ndarray, sigma: float) -> np.ndarray:
    pad = int(math.ceil(3 * sigma)) + 1
    p = np.pad(img, ((pad, pad), (pad, pad), (0, 0)), mode="wrap")
    b = cv2.GaussianBlur(p, (0, 0), sigma)
    return b[pad:-pad, pad:-pad]


def _splat(canvas: np.ndarray, x: np.ndarray, y: np.ndarray, rgb: np.ndarray):
    """点をバイリニアで置く(上下左右はつながる)。"""
    H, W = canvas.shape[:2]
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = (x - x0).astype(F32)[:, None]
    fy = (y - y0).astype(F32)[:, None]
    for dx, dy, wgt in ((0, 0, (1 - fx) * (1 - fy)), (1, 0, fx * (1 - fy)),
                        (0, 1, (1 - fx) * fy), (1, 1, fx * fy)):
        np.add.at(canvas, ((y0 + dy) % H, (x0 + dx) % W), rgb * wgt)


def _periodic_noise2(W: int, H: int, gx: int, gy: int, rng) -> np.ndarray:
    """周期的(上下左右がつながる)な2次元バリューノイズ (H, W)。"""
    g = rng.random((gy, gx)).astype(F32)
    xs = np.arange(W, dtype=F32) / W * gx
    ys = np.arange(H, dtype=F32) / H * gy
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    fx = xs - x0
    fy = ys - y0
    fx = fx * fx * (3 - 2 * fx)
    fy = fy * fy * (3 - 2 * fy)
    x1 = (x0 + 1) % gx
    y1 = (y0 + 1) % gy
    x0 %= gx
    y0 %= gy
    a = g[y0][:, x0]
    b = g[y0][:, x1]
    c = g[y1][:, x0]
    d = g[y1][:, x1]
    top = a + (b - a) * fx[None, :]
    bot = c + (d - c) * fx[None, :]
    return top + (bot - top) * fy[:, None]


def _periodic_fbm2(W, H, cells, octaves, rng, gain=0.5):
    out = np.zeros((H, W), F32)
    amp, norm = 1.0, 0.0
    for o in range(octaves):
        gx = max(2, int(round(cells * 2 ** o)))
        gy = max(2, int(round(gx * H / W)))
        out += F32(amp) * _periodic_noise2(W, H, gx, gy, rng)
        norm += amp
        amp *= gain
    return out / F32(norm)


class StarBackground:
    """星空の背景。遠・近2枚のレイヤー(視差)+ またたく明るい星 + 星雲/天の川。"""

    def __init__(self, w: int, h: int, seed: int, density: float = 1.0, nebula: float = 0.12,
                 color_variation: float = 0.5, margin: float = 1.1, milky_way: bool = True):
        self.w, self.h = w, h
        rs = h / 1080.0
        self.rs = rs
        self.margin = margin
        Wc, Hc = int(math.ceil(w * margin)), int(math.ceil(h * margin))
        self.Wc, self.Hc = Wc, Hc
        area = margin * margin
        rng = np.random.default_rng([seed & 0x7FFFFFFF, 4242])
        far = np.zeros((Hc, Wc, 3), F32)
        near = np.zeros((Hc, Wc, 3), F32)
        self.bright = None
        if density > 0:
            sig_f = max(0.55, 0.6 * rs)
            n_far = int(2600 * density * area)
            xs, ys = rng.random(n_far) * Wc, rng.random(n_far) * Hc
            b = (0.1 + 0.75 * rng.random(n_far) ** 2.6)
            col = star_colors(rng, n_far, color_variation)
            _splat(far, xs, ys, col * (b * TAU * sig_f ** 2)[:, None])
            far = _blur_wrap(far, sig_f)
            sig_n = max(0.7, 0.95 * rs)
            n_near = int(260 * density * area)
            xs, ys = rng.random(n_near) * Wc, rng.random(n_near) * Hc
            b = 0.35 + 0.65 * rng.random(n_near) ** 1.8
            col = star_colors(rng, n_near, color_variation)
            tmp = np.zeros_like(near)
            _splat(tmp, xs, ys, col * (b * TAU * sig_n ** 2)[:, None])
            near = _blur_wrap(tmp, sig_n) + 0.08 * _blur_wrap(tmp, 3.5 * sig_n)
            n_b = max(1, int(30 * density * area))
            self.bright = {
                "x": rng.random(n_b) * Wc, "y": rng.random(n_b) * Hc,
                "b": 0.75 + 0.6 * rng.random(n_b) ** 2,
                "col": star_colors(rng, n_b, color_variation),
                "sig": max(0.8, 1.1 * rs) * (0.8 + 0.6 * rng.random(n_b)),
                "f": 0.3 + 1.0 * rng.random(n_b), "ph": rng.random(n_b) * TAU,
                "ph2": rng.random(n_b) * TAU,
                "spike": np.arange(n_b) < max(1, n_b // 8),
            }
        if nebula > 0:
            far += self._nebula(Wc, Hc, rng, nebula, density, color_variation, milky_way)
        self.far = _u8(np.clip(far, 0, 1))
        self.near = _u8(np.clip(near, 0, 1))

    def _nebula(self, Wc, Hc, rng, strength, density, cvar, milky_way):
        ds = 4
        w4, h4 = max(8, Wc // ds), max(8, Hc // ds)
        n1 = _periodic_fbm2(w4, h4, 3, 5, rng, 0.55)
        n2 = _periodic_fbm2(w4, h4, 5, 4, rng, 0.5)
        n3 = _periodic_fbm2(w4, h4, 2, 3, rng, 0.5)
        yy = np.arange(h4, dtype=F32)[:, None] / h4
        xx = np.arange(w4, dtype=F32)[None, :] / w4
        out = np.zeros((h4, w4, 3), F32)
        if milky_way:
            ph = rng.uniform(0, TAU)
            yc = 0.5 + 0.2 * np.sin(TAU * xx + ph)
            band = np.exp(-((yy - yc) / 0.17) ** 2) * (0.6 + 0.8 * n3)
            dust = _smooth(0.5, 0.68, n2) * np.exp(-((yy - yc) / 0.06) ** 2)
            core = np.clip(band * (0.35 + 0.9 * n1) - 0.7 * dust, 0, None)
            out += core[..., None] * np.array([0.85, 0.8, 0.75], F32) * 0.28
            out += (band * _smooth(0.55, 0.8, n1))[..., None] * np.array([0.35, 0.45, 0.8], F32) * 0.12
        cloud = _smooth(0.45, 0.85, n1) ** 2
        hue = _smooth(0.35, 0.65, n2)[..., None]
        tint = _mix(np.array([0.45, 0.22, 0.75], F32), np.array([0.12, 0.45, 0.65], F32), hue)
        tint = _mix(np.array([0.4, 0.4, 0.45], F32), tint, F32(0.4 + 0.6 * cvar))
        out += cloud[..., None] * tint * 0.22
        warm = _smooth(0.72, 0.9, n3 * n1 * 1.6)
        out += warm[..., None] * np.array([0.8, 0.28, 0.25], F32) * 0.08
        out = cv2.resize(out, (Wc, Hc), interpolation=cv2.INTER_CUBIC) * F32(strength)
        if milky_way and density > 0:           # 天の川に沿って細かい星を増やす
            n = int(5000 * strength * density * (Wc * Hc) / float(self.w * self.h))
            xs = rng.random(n * 3) * Wc
            ys = rng.random(n * 3) * Hc
            yc = Hc * (0.5 + 0.2 * np.sin(TAU * xs / Wc + ph))
            keep = rng.random(n * 3) < np.exp(-((ys - yc) / (0.12 * Hc)) ** 2)
            xs, ys = xs[keep][:n], ys[keep][:n]
            tmp = np.zeros_like(out)
            b = 0.08 + 0.25 * rng.random(len(xs)) ** 3
            _splat(tmp, xs, ys, star_colors(rng, len(xs), cvar) * (b * TAU * 0.36)[:, None])
            out += _blur_wrap(tmp, 0.6)
        return out

    def _warp_layer(self, layer, s, tx, ty):
        cx, cy = self.Wc / 2.0, self.Hc / 2.0
        m = np.array([[s, 0, self.w / 2.0 - s * cx + tx], [0, s, self.h / 2.0 - s * cy + ty]], F32)
        return cv2.warpAffine(layer, m, (self.w, self.h), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_WRAP)

    def layer_transform(self, k: float, s: float, ox: float, oy: float, orbit_px: float):
        sk = 1.0 + (s - 1.0) * k
        return sk, ox * k * self.w + orbit_px, oy * k * self.h

    def render(self, t: float, s: float, ox: float, oy: float, orbit_px: float = 0.0) -> np.ndarray:
        s0, tx0, ty0 = self.layer_transform(STAR_PARALLAX[0], s, ox, oy, orbit_px)
        img = self._warp_layer(self.far, s0, tx0, ty0)
        s1, tx1, ty1 = self.layer_transform(STAR_PARALLAX[1], s, ox, oy, orbit_px)
        img = cv2.add(img, self._warp_layer(self.near, s1, tx1, ty1))
        if self.bright is not None:
            self._draw_bright(img, t, s1, tx1, ty1)
        return img

    def _draw_bright(self, img, t, s, tx, ty):
        B = self.bright
        cx, cy = self.Wc / 2.0, self.Hc / 2.0
        X = s * (B["x"] - cx) + self.w / 2.0 + tx
        Y = s * (B["y"] - cy) + self.h / 2.0 + ty
        pw, ph = self.Wc * s, self.Hc * s
        X = (X + 40) % pw - 40                 # 周期的に並べ直して画面内へ
        Y = (Y + 40) % ph - 40
        tw = 1.0 + 0.13 * np.sin(TAU * B["f"] * t + B["ph"]) + 0.07 * np.sin(TAU * 2.3 * B["f"] * t + B["ph2"])
        rs = self.rs
        for i in range(len(X)):
            x, y = X[i], Y[i]
            sig = B["sig"][i] * s
            r = int(math.ceil(sig * 5 + (14 * rs if B["spike"][i] else 0))) + 1
            x0, y0 = int(math.floor(x)) - r, int(math.floor(y)) - r
            x1, y1 = x0 + 2 * r + 2, y0 + 2 * r + 2
            if x1 <= 0 or y1 <= 0 or x0 >= self.w or y0 >= self.h:
                continue
            gx = np.arange(x0, x1, dtype=F32) + 0.5 - F32(x)
            gy = np.arange(y0, y1, dtype=F32) + 0.5 - F32(y)
            r2 = gx[None, :] ** 2 + gy[:, None] ** 2
            v = np.exp(-r2 / (2 * sig * sig)) + 0.06 * np.exp(-np.sqrt(r2) / (2.2 * sig))
            if B["spike"][i]:
                L = 7.0 * rs * s + 2
                v = v + 0.35 * (np.exp(-np.abs(gx)[None, :] / L) * np.exp(-(gy[:, None] / 0.55) ** 2)
                                + np.exp(-np.abs(gy)[:, None] / L) * np.exp(-(gx[None, :] / 0.55) ** 2))
            add = v[..., None] * (B["col"][i] * B["b"][i] * tw[i] * 255.0)
            cx0, cy0 = max(0, x0), max(0, y0)
            cx1, cy1 = min(self.w, x1), min(self.h, y1)
            patch = img[cy0:cy1, cx0:cx1].astype(F32)
            patch += add[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
            img[cy0:cy1, cx0:cx1] = np.clip(patch, 0, 255).astype(np.uint8)


def _orbit_px(orbit_deg: float, h: int) -> float:
    """orbit角 → 遠くの星が右へ流れる量(px)。"""
    f_px = (h / 2.0) / math.tan(math.radians(CAMERA_VFOV_DEG / 2.0))
    return math.radians(orbit_deg) * f_px


def _place_sprite(img: np.ndarray, rgb: np.ndarray, alpha: np.ndarray, cx: float, cy: float,
                  k: float, half_w: float, half_h: float):
    """前乗算RGB+αのスプライトを、中心(cx, cy)・倍率kで画面に合成する(サブピクセル)。"""
    H, W = img.shape[:2]
    x0 = int(math.floor(cx - k * half_w)) - 1
    y0 = int(math.floor(cy - k * half_h)) - 1
    x1 = int(math.ceil(cx + k * half_w)) + 1
    y1 = int(math.ceil(cy + k * half_h)) + 1
    cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if cx1 <= cx0 or cy1 <= cy0:
        return
    m = np.array([[k, 0, cx - k * half_w - cx0], [0, k, cy - k * half_h - cy0]], F32)
    src = np.dstack([rgb, alpha])
    dst = cv2.warpAffine(src, m, (cx1 - cx0, cy1 - cy0), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    patch = img[cy0:cy1, cx0:cx1].astype(F32) * F32(1 / 255.0)
    a = dst[..., 3:4]
    out = patch * (1.0 - a) + dst[..., :3]
    img[cy0:cy1, cx0:cx1] = _u8(np.clip(out, 0, 1))


def _bloom(img: np.ndarray, box, threshold: float = 0.6, strength: float = 0.7, sigma: float = 0.02):
    """明るい所のにじみ(低解像度でぼかして足す)。box=(x0, y0, x1, y1) の範囲だけ。"""
    H, W = img.shape[:2]
    x0, y0, x1, y1 = (max(0, int(box[0])), max(0, int(box[1])), min(W, int(box[2])), min(H, int(box[3])))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return
    region = img[y0:y1, x0:x1]
    ds = 4
    small = cv2.resize(region, (max(2, (x1 - x0) // ds), max(2, (y1 - y0) // ds)),
                       interpolation=cv2.INTER_AREA).astype(F32) / 255.0
    lum = small.max(axis=2, keepdims=True)
    br = small * np.clip((lum - threshold) / (1 - threshold), 0, 1)
    sg = max(1.0, sigma * H / ds)
    glow = cv2.GaussianBlur(br, (0, 0), sg) * 0.6 + cv2.GaussianBlur(br, (0, 0), sg * 3.5) * 0.5
    glow = cv2.resize(glow, (x1 - x0, y1 - y0), interpolation=cv2.INTER_LINEAR)
    img[y0:y1, x0:x1] = cv2.add(region, _u8(np.clip(glow * strength, 0, 1)))


# ================================================================ 惑星

_KIND_SHADE = {   # 明暗境界のぼかし / 周縁減光 / バンプの強さ / 月面らしい反射(Lommel-Seeliger)の混ぜ具合
    "earth": (0.03, 0.12, 1.0, 0.0),
    "rocky": (0.0, 0.0, 1.0, 0.55),
    "gas": (0.06, 0.45, 0.0, 0.0),
    "cloudy": (0.12, 0.4, 0.0, 0.0),
    "ice": (0.0, 0.05, 1.0, 0.3),
    "lava": (0.02, 0.05, 1.0, 0.2),
}

_RING_IN, _RING_OUT = 1.24, 2.33


def _ring_profile(n: int, seed: int, colors: dict):
    """土星風の環の半径方向の(不透明度, 色) 1次元テーブル。"""
    rng = np.random.default_rng(seed + 900)
    r = np.linspace(_RING_IN, _RING_OUT, n)
    op = np.zeros(n)
    op += 0.18 * ((r >= 1.24) & (r < 1.53))                               # C環
    op += 0.85 * ((r >= 1.53) & (r < 1.95)) * (0.8 + 0.2 * np.sin((r - 1.53) * 40))   # B環
    op += 0.05 * ((r >= 1.95) & (r < 2.03))                               # カッシーニの間隙
    op += 0.55 * ((r >= 2.03) & (r < 2.27))                               # A環
    op *= 1.0 - 0.9 * ((r > 2.205) & (r < 2.22))                          # エンケの間隙
    op += 0.3 * np.exp(-((r - 2.32) / 0.004) ** 2)                        # F環
    fine = np.zeros(n)
    for fr in (60, 150, 400, 900):
        fine += np.interp(r, np.linspace(_RING_IN, _RING_OUT, fr), rng.random(fr)) / 4
    op = np.clip(op * (0.65 + 0.7 * fine), 0, 0.97)
    op = cv2.GaussianBlur(op.astype(F32)[:, None], (1, 5), 0)[:, 0]
    base, dark = np.array(hex_rgb(colors.get("ring", "#d9c7a0"))), np.array(hex_rgb(colors.get("ring_dark", "#8e7d62")))
    t = np.clip((r - 1.3) / 0.6, 0, 1)
    col = dark[None, :] * (1 - t[:, None]) + base[None, :] * t[:, None]
    col *= (0.85 + 0.3 * fine)[:, None]
    col[r > 2.03] *= 0.9
    return op.astype(F32), col.astype(F32)


class PlanetBody:
    """1つの惑星(スプライト方式)。"""

    def __init__(self, preset, texture_path, diameter_px, center, rotation_speed, longitude,
                 sun_angle, sun_elevation, atmosphere, atm_strength, rings, tilt, inclination,
                 clouds, night_lights, seed, s_max, frame_h):
        pr = PRESETS[preset]
        self.pr = pr
        self.kind = pr["kind"]
        self.center = center                                  # 画面比 (x, y)
        self.R0 = diameter_px / 2.0
        self.s_max = s_max
        self.rotation_speed = rotation_speed
        self.longitude = math.radians(longitude)
        self.sun_angle, self.sun_elevation = sun_angle, sun_elevation
        self.atm = _rgb(atmosphere) if atmosphere else None
        self.atm_strength = atm_strength if atmosphere else 0.0
        self.rings = bool(rings)
        self.halo_w = 0.07 if self.atm is not None else 0.0
        Rr = self.R0 * s_max
        ext = 1.0 + self.halo_w
        hw, hh = Rr * ext, Rr * ext
        self.M = (_rot_z(-math.radians(tilt)) @ _rot_x(math.radians(inclination))).astype(F32)
        if self.rings:
            a, b = _RING_OUT, _RING_OUT * abs(math.sin(math.radians(inclination)))
            t = math.radians(tilt)
            hw = max(hw, Rr * math.hypot(a * math.cos(t), b * math.sin(t)))
            hh = max(hh, Rr * math.hypot(a * math.sin(t), b * math.cos(t)))
        limit = 2600.0                                        # 巨大なスプライトは解像度を落とす
        sc = min(1.0, limit / max(hw, hh))
        self.Rs = Rr * sc                                     # スプライト上の半径(px)
        self.half_w = int(math.ceil(hw * sc)) + 2
        self.half_h = int(math.ceil(hh * sc)) + 2
        self.geo = _SphereGeom(self.Rs, self.half_w, self.half_h, self.M)
        need = TAU * self.Rs * 0.8
        tw = 256
        while tw < need and tw < 2048:
            tw *= 2
        self.tex = planet_texture(preset, int(seed), tw, bool(clouds) and not texture_path or
                                  (bool(clouds) and bool(texture_path)), bool(night_lights),
                                  texture_path or None)
        self.cloud_speed = 1.18
        self._lcache = {}
        if self.rings:
            self.ring_op, self.ring_col = _ring_profile(2048, seed, pr["colors"])
        self._frame_h = frame_h

    # ---- 太陽方向に依存するが自転には依存しない部分(大気の外縁・環)はキャッシュ
    def _static_layers(self, L: np.ndarray):
        key = tuple(np.round(L, 5))
        if key in self._lcache:
            return self._lcache[key]
        g = self.geo
        out = {}
        if self.atm is not None:
            D = g.D.ravel()
            sel = np.flatnonzero((D > 1.0 - 1.0 / g.R) & (D < 1.0 + self.halo_w))
            d = D[sel]
            x, y = g.X.ravel()[sel] / d, g.Y.ravel()[sel] / d
            tt = np.clip((d - 1.0) / self.halo_w, 0, 1)
            lit = np.clip((x * L[0] + y * L[1]) * 0.9 + 0.3 + max(0.0, -L[2]) * 0.8, 0, 1)
            fall = np.exp(-tt * 3.2) * (1 - _smooth(0.6, 1.0, tt))
            out["halo_idx"] = sel
            out["halo"] = (self.atm[None, :] * (self.atm_strength * 0.8 * fall * lit)[:, None]).astype(F32)
        if self.rings:
            out.update(self._ring_layer(L))
        if len(self._lcache) > 4:
            self._lcache.clear()
        self._lcache[key] = out
        return out

    def _ring_layer(self, L):
        g = self.geo
        M = self.M
        c0, c2 = M[:, 0], M[:, 2]
        det = c0[0] * c2[1] - c2[0] * c0[1]
        X = (g.X * c2[1] - g.Y * c2[0]) / det
        Z = (c0[0] * g.Y - c0[1] * g.X) / det
        zv = c0[2] * X + c2[2] * Z                       # 環の点の手前方向の深さ
        r = np.sqrt(X * X + Z * Z)
        n = len(self.ring_op)
        ri = (r - _RING_IN) / (_RING_OUT - _RING_IN) * (n - 1)
        inside = (ri >= 0) & (ri <= n - 1)
        ri_c = np.clip(ri, 0, n - 1)
        op = np.interp(ri_c, np.arange(n), self.ring_op).astype(F32) * inside
        col = np.stack([np.interp(ri_c, np.arange(n), self.ring_col[:, c]) for c in range(3)], -1).astype(F32)
        Lp = M.T @ L
        Vp = M.T @ np.array([0, 0, 1], F32)
        light = 0.2 + 0.8 * abs(Lp[1]) ** 0.5
        if Lp[1] * Vp[1] < 0:                            # 太陽と反対側(裏面)から見ている
            light *= 0.35
        # 惑星の影(環の上)
        P = np.stack([g.X, g.Y, zv], axis=-1)
        pl = P @ L
        perp2 = (P * P).sum(-1) - pl * pl
        shadow = np.where((pl < 0) & (perp2 < 1.0), 1.0 - _smooth(1.0, 0.94, perp2) * 0.92, 1.0)
        rgb = col * (light * shadow)[..., None] * op[..., None]
        # 環の影(惑星の上): 地表の点から太陽へ向かう光線が環の面を横切る位置
        npl = g.npl
        Ly = Lp[1] if abs(Lp[1]) > 1e-4 else 1e-4
        tpar = -npl[1] / Ly
        hx = npl[0] + tpar * Lp[0]
        hz = npl[2] + tpar * Lp[2]
        hr = np.sqrt(hx * hx + hz * hz)
        hri = np.clip((hr - _RING_IN) / (_RING_OUT - _RING_IN) * (n - 1), 0, n - 1)
        hop = np.interp(hri, np.arange(n), self.ring_op) * ((hr >= _RING_IN) & (hr <= _RING_OUT)) * (tpar > 0)
        ring_shadow = (1.0 - 0.85 * hop).astype(F32)
        # 惑星より手前か(円盤内の画素のみ判定)
        front = np.ones(g.X.shape, bool)
        front_flat = front.ravel()
        front_flat[g.idx] = zv.ravel()[g.idx] > g.nz
        return {"ring_rgb": rgb.astype(F32), "ring_a": op, "ring_front": front, "ring_shadow": ring_shadow}

    def render_sprite(self, t: float, orbit_deg: float):
        g = self.geo
        L = np.asarray(sun_vector(self.sun_angle, self.sun_elevation, orbit_deg), F32)
        soft, limb, bump_k, lommel = _KIND_SHADE[self.kind]
        lon_off = self.longitude - math.radians(self.rotation_speed * t) + math.radians(orbit_deg)
        tex = self.tex
        surf = g.sample(tex.surface, lon_off).astype(F32) * F32(1 / 255.0)
        alb, spec = surf[:, :3], surf[:, 3]
        n = g.n
        if tex.bump is not None and bump_k > 0:
            gb = g.sample(tex.bump, lon_off)
            n = n - F32(bump_k) * (gb[:, 0][None, :] * g.east + gb[:, 1][None, :] * g.north)
            n = n / np.sqrt((n * n).sum(0, keepdims=True))
        ndl_g = L[0] * g.n[0] + L[1] * g.n[1] + L[2] * g.n[2]
        ndl = L[0] * n[0] + L[1] * n[1] + L[2] * n[2]
        geo_gate = np.clip(ndl_g * 6.0 + 0.4, 0, 1)
        dif = np.clip((ndl + soft) / (1.0 + soft), 0, 1) * geo_gate
        if lommel > 0:
            ls = 2.0 * np.maximum(ndl, 0) / (np.maximum(ndl, 0) + np.maximum(g.nz, 0.05))
            dif = _mix(dif, np.clip(ls, 0, 1.6) * geo_gate * 0.62, F32(lommel))
        light = np.power(np.maximum(dif, 0), F32(1 / 2.2))
        amb = F32(0.012)
        col = alb * (amb + light)[:, None]
        if limb > 0:
            col *= (1.0 - limb + limb * np.power(g.nz, F32(0.45)))[:, None]
        dif_g = np.clip((ndl_g + 0.08) / 1.08, 0, 1)
        light_g = np.power(dif_g, F32(1 / 2.2))
        ca = None
        if tex.clouds is not None:
            ca = g.sample(tex.clouds, lon_off * self.cloud_speed).astype(F32)[:, 0] * F32(1 / 255.0)
            cc = _rgb(self.pr["colors"].get("cloud", "#ffffff"))
            col = col * (1.0 - ca)[:, None] + (ca * (amb + light_g * 1.02))[:, None] * cc[None, :]
        if spec.any():
            H = L + np.array([0, 0, 1], F32)
            H = H / np.linalg.norm(H)
            nh = np.clip(H[0] * n[0] + H[1] * n[1] + H[2] * n[2], 0, 1)
            sp = spec * (0.55 * nh ** 90 + 0.06 * nh ** 12) * (ndl_g > 0)
            if ca is not None:
                sp = sp * (1.0 - ca)
            col += (sp[:, None] * np.array([1.0, 0.95, 0.85], F32))
        if tex.emission is not None:
            em = g.sample(tex.emission, lon_off).astype(F32) * F32(1 / 255.0)
            night = 1.0 - _smooth(-0.1, 0.12, ndl_g)
            vis = night + tex.emission_day * (1.0 - night)
            if ca is not None:
                vis = vis * (1.0 - 0.75 * ca)
            col += em * vis[:, None]
        if self.atm is not None:
            s = self.atm_strength
            rim = np.power(1.0 - g.nz, F32(2.2)) * np.clip(ndl_g * 0.8 + 0.35 + max(0.0, -L[2]) * 0.6, 0, 1)
            haze = s * 0.35 * np.power(1.0 - g.nz, F32(1.5)) * light_g
            col = col * (1.0 - haze)[:, None] + (haze[:, None] * self.atm[None, :])
            col += (rim * s * 0.75)[:, None] * self.atm[None, :]
        col = _tonemap(col)
        Hs, Ws = g.H, g.W
        rgb = np.zeros((Hs * Ws, 3), F32)
        alpha = np.zeros(Hs * Ws, F32)
        cov = g.cover
        layers = self._static_layers(L)
        if self.rings:
            col = col * layers["ring_shadow"][:, None]
        rgb[g.idx] = col * cov[:, None]
        alpha[g.idx] = cov
        if "halo" in layers:
            rgb[layers["halo_idx"]] += layers["halo"]
        rgb = rgb.reshape(Hs, Ws, 3)
        alpha = alpha.reshape(Hs, Ws)
        if self.rings:
            rr, ra, front = layers["ring_rgb"], layers["ring_a"], layers["ring_front"]
            over_rgb = rr + (1 - ra)[..., None] * rgb             # 環が手前
            over_a = ra + (1 - ra) * alpha
            under_rgb = rgb + (1 - alpha)[..., None] * rr          # 惑星が手前
            under_a = alpha + (1 - alpha) * ra
            rgb = np.where(front[..., None], over_rgb, under_rgb)
            alpha = np.where(front, over_a, under_a)
        return rgb, alpha

    def draw(self, img, t, s, ox, oy, orbit_deg):
        H, W = img.shape[:2]
        cx = W / 2.0 + s * (self.center[0] * W - W / 2.0) + ox * W
        cy = H / 2.0 + s * (self.center[1] * H - H / 2.0) + oy * H
        k = (self.R0 * s) / self.Rs
        rgb, alpha = self.render_sprite(t, orbit_deg)
        _place_sprite(img, rgb, alpha, cx, cy, k, self.half_w, self.half_h)


# ================================================================ 太陽

class SunBody:
    def __init__(self, color, activity, diameter_px, center, rotation_speed, seed, s_max, frame_h):
        self.base = _rgb(color)
        self.activity = activity
        self.R0 = diameter_px / 2.0
        self.center = center
        self.rotation_speed = rotation_speed
        self.s_max = s_max
        Rr = min(self.R0 * s_max, 2400.0)
        self.Rs = Rr
        self.half = int(math.ceil(Rr * 1.16)) + 2
        M = (_rot_z(math.radians(-7.25)) @ _rot_x(math.radians(4.0))).astype(F32)
        self.geo = _SphereGeom(Rr, self.half, self.half, M)
        tw = 512
        while tw < TAU * Rr * 0.9 and tw < 2048:
            tw *= 2
        self.tex = self._texture(tw, seed, activity)
        self.center_col = _mix(self.base, np.ones(3, F32), F32(0.62)) * F32(1.3)
        self.limb_col = np.power(self.base, F32(1.8)) * F32(0.95)
        self._prom = self._prominences(seed)
        rng = np.random.default_rng(seed + 55)
        self._streamer = rng.random(48).astype(F32)
        self._streamer2 = rng.random(48).astype(F32)
        self._frame_h = frame_h

    def _texture(self, tw, seed, activity):
        rng = np.random.default_rng(seed + 500)
        ga = sphere_fbm(tw, 60.0, 2, seed + 1, gain=0.45)
        gb = sphere_fbm(tw, 60.0, 2, seed + 2, gain=0.45)
        sup = sphere_fbm(tw, 9.0, 3, seed + 3)
        ga = _smooth(0.3, 0.7, ga) * 0.75 + 0.25 * sup
        gb = _smooth(0.3, 0.7, gb) * 0.75 + 0.25 * sup
        spots = np.zeros((tw // 2, tw), F32)
        fac = np.zeros_like(spots)
        lat_rows = _lat_grid(tw)
        lon_cols = _lon_grid(tw)
        n = int(round(16 * activity))
        for k in range(n):
            lat0 = math.radians(rng.choice([-1, 1]) * rng.uniform(8, 32))
            lon0 = rng.uniform(-math.pi, math.pi)
            for j in range(rng.integers(1, 4)):
                la = lat0 + rng.normal(0, 0.03)
                lo = lon0 + j * rng.uniform(0.04, 0.09)
                r = rng.uniform(0.012, 0.035) * (1.0 if j == 0 else 0.6)
                cosd = (math.sin(la) * np.sin(lat_rows) + math.cos(la) * np.cos(lat_rows) *
                        np.cos(lon_cols - lo))
                x = np.arccos(np.clip(cosd, -1, 1)) / r
                umbra = _smooth(0.5, 0.35, x)
                pen = _smooth(1.0, 0.8, x)
                spots = np.maximum(spots, np.maximum(umbra * 0.92, pen * 0.5))
                fac = np.maximum(fac, _smooth(3.5, 1.2, x) * (1 - pen))
        tex = np.dstack([_u8(ga), _u8(gb), _u8(spots), _u8(fac)])
        return tex

    def _prominences(self, seed):
        """縁から立ちのぼるプロミネンス(極座標のノイズ)。スプライト上に固定で持つ。"""
        g = self.geo
        D = g.D.ravel()
        sel = np.flatnonzero((D > 0.99) & (D < 1.15))
        d = D[sel]
        ang = np.arctan2(g.Y.ravel()[sel], g.X.ravel()[sel])
        p = np.stack([np.cos(ang) * 6.0, np.sin(ang) * 6.0, d * 22.0], -1).astype(F32)
        nz = vnoise3(p, seed + 61) * 0.6 + vnoise3(p * 2.3, seed + 62) * 0.4
        env_p = np.stack([np.cos(ang) * 2.0, np.sin(ang) * 2.0, np.zeros_like(ang)], -1).astype(F32)
        env = _smooth(0.62 - 0.2 * self.activity, 0.8 - 0.15 * self.activity, vnoise3(env_p, seed + 63))
        h = (d - 1.0) / 0.15
        val = _smooth(0.55, 0.75, nz) * env * np.exp(-h * 3.0) * (h > -0.05)
        col = np.array([1.0, 0.36, 0.16], F32) * 1.3
        return sel, (val[:, None] * col[None, :] * min(1.0, 0.3 + self.activity)).astype(F32)

    def render_sprite(self, t, orbit_deg=0.0):
        g = self.geo
        lon_off = -math.radians(self.rotation_speed * t) + math.radians(orbit_deg)
        s = g.sample(self.tex, lon_off).astype(F32) * F32(1 / 255.0)
        s2 = g.sample(self.tex, lon_off * 1.35 + 0.02 * t).astype(F32) * F32(1 / 255.0)
        wv = 0.5 + 0.5 * math.sin(TAU * t / 7.0)
        gran = s2[:, 0] * wv + s2[:, 1] * (1 - wv)
        spot, fac = s[:, 2], s[:, 3]
        mu = g.nz
        limb = 1.0 - 0.62 * (1.0 - mu) - 0.2 * (1.0 - mu * mu)
        c = _mix(self.limb_col[None, :], self.center_col[None, :], np.power(mu, F32(0.55))[:, None])
        inten = limb * (0.8 + 0.36 * gran) * (1.0 - 0.88 * spot) + 0.35 * fac * np.power(1 - mu, F32(0.6))
        col = _tonemap(c * inten[:, None])
        Hs = Ws = 2 * self.half
        rgb = np.zeros((Hs * Ws, 3), F32)
        alpha = np.zeros(Hs * Ws, F32)
        rgb[g.idx] = col * g.cover[:, None]
        alpha[g.idx] = g.cover
        sel, pc = self._prom
        flick = 0.9 + 0.1 * math.sin(t * 1.7)
        rgb[sel] += pc * flick * (1.0 - alpha[sel])[:, None]
        return rgb.reshape(Hs, Ws, 3), alpha.reshape(Hs, Ws)

    def corona(self, img, cx, cy, R, t):
        """コロナ(外側の光)を1/4解像度で計算して足す。"""
        H, W = img.shape[:2]
        ds = 4
        ext = R * 4.5
        x0, y0 = max(0, int(cx - ext)), max(0, int(cy - ext))
        x1, y1 = min(W, int(cx + ext)), min(H, int(cy + ext))
        if x1 - x0 < ds or y1 - y0 < ds:
            return
        sw, sh = max(2, (x1 - x0) // ds), max(2, (y1 - y0) // ds)
        xs = x0 + (np.arange(sw, dtype=F32) + 0.5) * ((x1 - x0) / sw) - cx
        ys = y0 + (np.arange(sh, dtype=F32) + 0.5) * ((y1 - y0) / sh) - cy
        X, Y = np.meshgrid(xs / F32(R), ys / F32(R))
        d = np.maximum(np.sqrt(X * X + Y * Y), F32(1e-3))
        e = np.maximum(d - 1.0, 0)
        ang = (np.arctan2(Y, X) + math.pi) / TAU * 48 + 0.05 * t
        st = np.interp(ang.ravel() % 48, np.arange(49), np.r_[self._streamer, self._streamer[:1]]).reshape(ang.shape)
        st2 = np.interp((ang.ravel() * 2.7 + 13) % 48, np.arange(49), np.r_[self._streamer2, self._streamer2[:1]]).reshape(ang.shape)
        a = self.activity
        streak = 1.0 + a * 1.2 * (st ** 3 - 0.25) * _smooth(0.0, 0.6, e) + 0.35 * a * (st2 ** 4) * _smooth(0.0, 0.3, e)
        glow = 0.75 * np.exp(-e / 0.035) + 0.3 * np.exp(-e / 0.22) * streak + 0.085 * np.exp(-e / 1.1) * streak
        glow *= _smooth(4.5, 3.2, d)                       # 計算範囲の端で切れないよう減衰
        col = _mix(self.base, np.ones(3, F32), F32(0.35))
        layer = glow[..., None] * col[None, None, :]
        layer = cv2.resize(layer, (x1 - x0, y1 - y0), interpolation=cv2.INTER_LINEAR)
        img[y0:y1, x0:x1] = cv2.add(img[y0:y1, x0:x1], _u8(np.clip(layer, 0, 1)))

    def draw(self, img, t, s, ox, oy, orbit_deg):
        H, W = img.shape[:2]
        cx = W / 2.0 + s * (self.center[0] * W - W / 2.0) + ox * W
        cy = H / 2.0 + s * (self.center[1] * H - H / 2.0) + oy * H
        R = self.R0 * s
        self.corona(img, cx, cy, R, t)
        rgb, alpha = self.render_sprite(t, orbit_deg)
        _place_sprite(img, rgb, alpha, cx, cy, R / self.Rs, self.half, self.half)
        _bloom(img, (cx - R * 1.6, cy - R * 1.6, cx + R * 1.6, cy + R * 1.6), 0.8, 0.35)


# ================================================================ ブラックホール

class BlackHoleBody:
    """シャドウ・降着円盤(ドップラー非対称)・光子リング・裏側円盤の重力レンズ像(簡略)。"""

    R_IN, R_OUT = 1.45, 4.3            # 円盤の内縁・外縁(シャドウ半径単位)
    THETA_E = 1.6                      # 背景の重力レンズのアインシュタイン半径(シャドウ半径単位)

    def __init__(self, disk_color, tilt, roll, diameter_px, center, spin_speed, lensing, seed, s_max):
        self.col = _rgb(disk_color)
        self.e = math.radians(max(1.0, min(89.0, tilt)))
        self.roll = math.radians(roll)
        self.R0 = diameter_px / 2.0
        self.center = center
        self.spin = spin_speed
        self.lensing = lensing
        Rr = min(self.R0 * s_max, 900.0)
        self.Rs = Rr
        ext_w = self.R_OUT * 1.02
        ext_h = max(2.25, self.R_OUT * math.sin(self.e) * 1.02)
        cr, sr = abs(math.cos(self.roll)), abs(math.sin(self.roll))
        self.half_w = int(math.ceil(Rr * (ext_w * cr + ext_h * sr))) + 2
        self.half_h = int(math.ceil(Rr * (ext_w * sr + ext_h * cr))) + 2
        W, H = 2 * self.half_w, 2 * self.half_h
        xs = ((np.arange(W, dtype=F32) + 0.5) - self.half_w) / F32(Rr)
        ys = (self.half_h - (np.arange(H, dtype=F32) + 0.5)) / F32(Rr)
        X, Y = np.meshgrid(xs, ys)
        c, s = math.cos(-self.roll), math.sin(-self.roll)
        Xu, Yu = X * c - Y * s, X * s + Y * c                 # 画面内の傾きを戻した座標
        self.W, self.H = W, H
        rho = np.sqrt(Xu * Xu + Yu * Yu)
        self.rho = rho
        self.shadow_a = np.clip((1.0 - rho) * F32(Rr) + 0.5, 0, 1)
        # 円盤(平面)
        Yd = Yu / F32(math.sin(self.e))
        r = np.sqrt(Xu * Xu + Yd * Yd)
        phi = np.arctan2(Yd, Xu)
        dsel = np.flatnonzero(((r > self.R_IN * 0.95) & (r < self.R_OUT)).ravel())
        self.disk_idx = dsel
        self.disk_r = r.ravel()[dsel]
        self.disk_phi = phi.ravel()[dsel]
        self.disk_near = (Yu.ravel()[dsel] < 0)
        # 裏側の円盤のレンズ像(シャドウの上下を回り込む光の輪)
        hsel = np.flatnonzero(((rho > 0.98) & (rho < 2.35)).ravel())
        rh = rho.ravel()[hsel]
        ps = np.arctan2(Yu.ravel()[hsel], Xu.ravel()[hsel])
        top = ps > 0
        rd = np.where(top, self.R_IN + (rh - 1.07) / 0.3, self.R_IN + (rh - 1.04) / 0.17)
        self.halo_idx = hsel
        self.halo_r = rd.astype(F32)
        self.halo_phi = np.abs(ps).astype(F32)
        wt = np.where(top, _smooth(0.0, 0.55, np.sin(ps)), 0.6 * _smooth(0.0, 0.55, -np.sin(ps)))
        self.halo_w = (wt * _smooth(0.99, 1.08, rh)).astype(F32)
        self.halo_cos = np.cos(ps).astype(F32)
        # 光子リング
        psel = np.flatnonzero(((rho > 0.96) & (rho < 1.1)).ravel())
        self.ring_idx = psel
        pr = rho.ravel()[psel]
        pcos = Xu.ravel()[psel] / np.maximum(pr, 1e-6)
        self.ring_val = (np.exp(-((pr - 1.025) / 0.014) ** 2) * (1.0 - 0.5 * pcos)).astype(F32)
        self.tex = self._disk_texture(seed)

    def _disk_texture(self, seed, nr=384, nphi=2048):
        r = np.linspace(self.R_IN * 0.95, self.R_OUT, nr, dtype=F32)[:, None]
        ph = (np.arange(nphi, dtype=F32) / nphi * TAU)[None, :]
        out = np.zeros((nr, nphi), F32)
        norm = 0
        for k, (A, B, amp) in enumerate(((2.0, 14.0, 1.0), (4.0, 36.0, 0.7), (9.0, 90.0, 0.45),
                                         (14.0, 14.0, 0.35))):
            p = np.stack(np.broadcast_arrays(np.cos(ph) * A, np.sin(ph) * A, r * B), -1).astype(F32)
            out += amp * vnoise3(p, seed + 70 + k)
            norm += amp
        out /= norm
        return np.clip(0.5 + 1.6 * (out - 0.5), 0.05, 1.0).astype(F32)

    def _disk_sample(self, r, phi, t, orbit_deg):
        nr, nphi = self.tex.shape
        om = 0.55 * self.spin * (self.R_IN / np.maximum(r, self.R_IN)) ** 1.5
        ph = (phi - om * t + math.radians(orbit_deg)) / TAU
        ph -= np.floor(ph)
        mx = ph * nphi - 0.5
        my = np.clip((r - self.R_IN * 0.95) / (self.R_OUT - self.R_IN * 0.95) * (nr - 1), 0, nr - 1)
        v = remap_flat(self.tex, mx, my)[:, 0]
        prof = np.power(self.R_IN / np.maximum(r, 1e-3), F32(1.5)) * _smooth(self.R_IN * 0.96, self.R_IN * 1.12, r) \
            * (1.0 - _smooth(self.R_OUT * 0.7, self.R_OUT, r))
        temp = np.clip(np.power(self.R_IN / np.maximum(r, 1e-3), F32(0.9)), 0, 1)
        return v * prof, temp

    def _colorize(self, inten, temp, cosv):
        """強さ・温度・ドップラー(cosv: 画面の右=+1, 左=-1)から色を作る。左が近づく側で明るい。"""
        dop = np.power(1.0 + 0.55 * (-cosv), F32(2.0))
        hot = _mix(self.col, np.ones(3, F32), F32(0.7)) * F32(1.5)
        cold = np.power(self.col, F32(1.4)) * F32(0.9)
        c = _mix(cold[None, :], hot[None, :], temp[:, None])
        c = _mix(c, np.array([0.85, 0.9, 1.0], F32)[None, :] * c.max(1, keepdims=True),
                 (np.clip(-cosv, 0, 1) * 0.25)[:, None])
        return c * (inten * dop)[:, None] * F32(1.25)

    def render_sprite(self, t, orbit_deg):
        W, H = self.W, self.H
        rgb = np.zeros((H * W, 3), F32)
        a = np.zeros(H * W, F32)
        # 円盤(奥半分)
        inten, temp = self._disk_sample(self.disk_r, self.disk_phi, t, orbit_deg)
        cosv = np.cos(self.disk_phi).astype(F32)
        dcol = self._colorize(inten, temp, cosv)
        da = np.clip(inten * 2.2, 0, 0.93)
        far = ~self.disk_near
        fi = self.disk_idx[far]
        rgb[fi] = dcol[far] * da[far][:, None] / np.maximum(da[far], 1e-3)[:, None]
        a[fi] = da[far]
        # レンズ像の輪(加算)
        hi, hth = self._disk_sample(self.halo_r, self.halo_phi, t, orbit_deg)
        hc = self._colorize(hi * self.halo_w, hth, self.halo_cos)
        rgb[self.halo_idx] += hc
        # シャドウ(黒で覆う)
        sa = self.shadow_a.ravel()
        rgb *= (1.0 - sa)[:, None]
        a = sa + (1.0 - sa) * a
        # 光子リング
        base = self._colorize(np.full(len(self.ring_idx), 1.0, F32), np.full(len(self.ring_idx), 1.0, F32),
                              np.zeros(len(self.ring_idx), F32))
        rgb[self.ring_idx] += base * self.ring_val[:, None] * F32(0.9)
        # 円盤(手前半分)を上に
        ni = self.disk_idx[self.disk_near]
        na = da[self.disk_near]
        rgb[ni] = dcol[self.disk_near] + (1.0 - na)[:, None] * rgb[ni]
        a[ni] = na + (1.0 - na) * a[ni]
        rgb = _tonemap(rgb)
        return rgb.reshape(H, W, 3), a.reshape(H, W)

    def lens(self, img, cx, cy, R):
        """背景の星空を点質量レンズで歪ませる(シャドウ周りの星が輪のように回り込む)。"""
        H, W = img.shape[:2]
        ext = R * 7.0
        x0, y0 = max(0, int(cx - ext)), max(0, int(cy - ext))
        x1, y1 = min(W, int(cx + ext) + 1), min(H, int(cy + ext) + 1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return
        xs = np.arange(x0, x1, dtype=F32) + 0.5 - F32(cx)
        ys = np.arange(y0, y1, dtype=F32) + 0.5 - F32(cy)
        dx, dy = np.meshgrid(xs, ys)
        r2 = np.maximum(dx * dx + dy * dy, F32(1.0))
        rr = np.sqrt(r2)
        te2 = F32((self.THETA_E * R) ** 2)
        win = _smooth(ext, ext * 0.45, rr)
        f = np.minimum(te2 / r2, F32(4.0)) * win
        mx = (cx + dx * (1.0 - f) - 0.5).astype(F32)
        my = (cy + dy * (1.0 - f) - 0.5).astype(F32)
        src = img.copy()
        img[y0:y1, x0:x1] = cv2.remap(src, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

    def draw(self, img, t, s, ox, oy, orbit_deg):
        H, W = img.shape[:2]
        cx = W / 2.0 + s * (self.center[0] * W - W / 2.0) + ox * W
        cy = H / 2.0 + s * (self.center[1] * H - H / 2.0) + oy * H
        R = self.R0 * s
        if self.lensing:
            self.lens(img, cx, cy, R)
        rgb, a = self.render_sprite(t, orbit_deg)
        k = R / self.Rs
        _place_sprite(img, rgb, a, cx, cy, k, self.half_w, self.half_h)
        ext = R * self.R_OUT * 1.2
        _bloom(img, (cx - ext, cy - ext * 0.8, cx + ext, cy + ext * 0.8), 0.55, 0.8)


# ================================================================ 星空の前進飛行(ワープ)

class WarpStars:
    """3次元の点を透視投影し、前フレームからの移動を筋として描く。決定的(時刻の関数)。"""

    Z_NEAR, Z_FAR = 0.35, 14.0
    VFOV = 62.0

    def __init__(self, w, h, seed, density, speed, color_variation, fps):
        rng = np.random.default_rng([seed & 0x7FFFFFFF, 5151])
        self.w, self.h, self.speed, self.fps = w, h, speed, fps
        n = int(1800 * max(0.05, density))
        self.f = (h / 2.0) / math.tan(math.radians(self.VFOV / 2.0))
        spread = math.tan(math.radians(self.VFOV / 2.0)) * self.Z_FAR * (w / h) * 1.15
        self.x = rng.uniform(-spread, spread, n).astype(F32)
        self.y = rng.uniform(-spread * h / w * 1.3, spread * h / w * 1.3, n).astype(F32)
        self.z0 = rng.uniform(self.Z_NEAR, self.Z_FAR, n).astype(F32)
        self.b = (0.35 + 0.65 * rng.random(n) ** 1.5).astype(F32)
        self.col = star_colors(rng, n, color_variation)
        self.rs = h / 1080.0

    def _z(self, t):
        span = self.Z_FAR - self.Z_NEAR
        return self.Z_NEAR + np.mod(self.z0 - self.speed * t - self.Z_NEAR, span)

    def _project(self, z, s, ox, oy, orbit_deg):
        th = math.radians(orbit_deg)
        piv = self.Z_FAR * 0.5
        x, zz = self.x, z
        if th:
            c, sn = math.cos(th), math.sin(th)
            xr = x * c - (zz - piv) * sn
            zr = x * sn + (zz - piv) * c + piv
            x, zz = xr, np.maximum(zr, 0.05)
        X = self.w / 2.0 + s * (self.f * x / zz) + ox * self.w
        Y = self.h / 2.0 - s * (self.f * self.y / zz) + oy * self.h
        return X, Y, zz

    def draw(self, img, t, s, ox, oy, orbit_deg):
        if self.speed <= 0:
            return
        z = self._z(t)
        tail_t = min(0.12, 2.2 / self.fps) * min(1.0, 0.35 + 0.25 * self.speed)
        zp = z + self.speed * tail_t
        wrapped = zp > self.Z_FAR
        zp = np.minimum(zp, self.Z_FAR)
        X1, Y1, z1 = self._project(z, s, ox, oy, orbit_deg)
        X0, Y0, _ = self._project(zp, s, ox, oy, orbit_deg)
        X0 = np.where(wrapped, X1, X0)
        Y0 = np.where(wrapped, Y1, Y0)
        fade = _smooth(self.Z_FAR, self.Z_FAR * 0.65, z1) * _smooth(self.Z_NEAR, self.Z_NEAR * 2.5, z1)
        br = np.clip(self.b * fade * np.minimum(3.0 / z1, 2.5) * 0.6, 0, 1)
        vis = (br > 0.02) & (np.maximum(X0, X1) > -50) & (np.minimum(X0, X1) < self.w + 50) & \
              (np.maximum(Y0, Y1) > -50) & (np.minimum(Y0, Y1) < self.h + 50)
        layer = np.zeros_like(img)
        thick = np.where(z1 < 1.8, 2, 1) * max(1, int(round(self.rs * 1.3)))
        sh = 3
        mul = 1 << sh
        for tk in np.unique(thick[vis]):
            for lv in range(1, 9):
                lo, hi = (lv - 1) / 8.0, lv / 8.0
                m = vis & (thick == tk) & (br > lo) & (br <= hi)
                if not m.any():
                    continue
                cols = self.col[m].mean(axis=0) * ((lo + hi) / 2) * 255.0
                pts = np.stack([np.stack([X0[m], Y0[m]], -1), np.stack([X1[m], Y1[m]], -1)], 1)
                pts = np.round(pts * mul).astype(np.int32)
                cv2.polylines(layer, list(pts), False, tuple(float(v) for v in cols), int(tk),
                              cv2.LINE_AA, sh)
        glow = cv2.GaussianBlur(cv2.resize(layer, (self.w // 4, self.h // 4), interpolation=cv2.INTER_AREA),
                                (0, 0), 1.5)
        glow = cv2.resize(glow, (self.w, self.h), interpolation=cv2.INTER_LINEAR)
        out = cv2.add(img, layer)
        out = cv2.addWeighted(out, 1.0, glow, 0.8, 0)
        if self.speed >= 2.5:                              # ワープの中心の青白い光
            yy, xx = np.ogrid[:self.h, :self.w]
            cx, cy = self.w / 2.0 + ox * self.w, self.h / 2.0 + oy * self.h
            d = np.sqrt(((xx - cx) / self.h) ** 2 + ((yy - cy) / self.h) ** 2)
            k = min(1.0, (self.speed - 2.5) / 3.0)
            tint = (np.exp(-d / 0.18) * 60 * k).astype(F32)
            add = np.dstack([tint * 0.6, tint * 0.8, tint]).astype(np.uint8)
            out = cv2.add(out, add)
        img[:] = out


# ================================================================ ソース本体

class Space2DSource(FrameSource):
    """宇宙テンプレートの2.5D描画ソース(FrameSource)。同じ引数なら同じ画を返す(決定的)。"""

    def __init__(self, template: str, params: dict, n_frames: int, w: int, h: int, fps: float,
                 cam: CameraMove = None, seed: int = 0):
        self.template = template
        self.p = p = resolve_params(template, params)
        self.n_frames, self.w, self.h, self.fps = n_frames, w, h, fps
        self.cam = cam or CameraMove()
        self.seed = int(seed)
        us = np.linspace(0, 1, max(2, min(n_frames, 64)))
        states = [camera_state(self.cam, u) for u in us]
        s_max = max(1.0, max(st[0] for st in states))
        s_min = min(1.0, min(st[0] for st in states))
        k0 = STAR_PARALLAX[0]
        margin = max(1.05, 1.02 / (1.0 + (s_min - 1.0) * k0))
        density = p.get("stars", p.get("density", 1.0))
        cvar = p.get("color_variation", 0.5)
        self.bg = StarBackground(w, h, self.seed, density, p.get("nebula", 0.12), cvar, margin,
                                 milky_way=True)
        self.objs = []
        if template == "planet":
            self.objs.append(self._planet(p["preset"], p["texture"], p["size"] * h,
                                          tuple(p["position"]), p, p["atmosphere"], p["atm_strength"],
                                          p["rings"], p["tilt"], p["inclination"], p["clouds"],
                                          p["night_lights"], p["longitude"], s_max, self.seed))
        elif template == "planet_compare":
            for k, (cx, cy, dia) in enumerate(compare_layout(p, w, h)):
                name = p["presets"][k]
                pr = PRESETS[name]
                tex = p["textures"][k]
                self.objs.append(self._planet(
                    name, tex, dia * h, (cx, cy), p, pr["atmosphere"],
                    pr["atm_strength"] if pr["atmosphere"] else 0.0, pr["rings"], pr["tilt"],
                    pr["inclination"], pr["clouds"] and not tex, pr["night_lights"] and not tex,
                    pr.get("longitude", 0.0), s_max, self.seed + 17 * k))
        elif template == "sun":
            self.objs.append(SunBody(p["color"], p["activity"], p["size"] * h, tuple(p["position"]),
                                     p["rotation_speed"], self.seed, s_max, h))
        elif template == "black_hole":
            self.objs.append(BlackHoleBody(p["disk_color"], p["tilt"], p["roll"], p["size"] * h,
                                           tuple(p["position"]), p["spin_speed"], p["lensing"],
                                           self.seed, s_max))
        elif template == "starfield":
            if p["speed"] > 0:
                self.objs.append(WarpStars(w, h, self.seed, p["density"], p["speed"],
                                           p["color_variation"], fps))

    def _planet(self, preset, texture, dia_px, center, p, atm, atm_s, rings, tilt, incl, clouds,
                night, longitude, s_max, seed):
        return PlanetBody(preset, texture, dia_px, center, p["rotation_speed"], longitude,
                          p["sun_angle"], p["sun_elevation"], atm, atm_s, rings, tilt, incl,
                          clouds, night, seed, s_max, self.h)

    def frame(self, i: int) -> np.ndarray:
        i = min(max(0, int(i)), max(0, self.n_frames - 1))
        t = i / float(self.fps)
        u = i / max(1, self.n_frames - 1)
        s, ox, oy, orb = camera_state(self.cam, u)
        img = self.bg.render(t, s, ox, oy, _orbit_px(orb, self.h))
        for o in self.objs:
            o.draw(img, t, s, ox, oy, orb)
        return img
