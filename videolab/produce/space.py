"""宇宙シーン(惑星・太陽・ブラックホール・星空)の映像ソースの窓口とパラメータ定義。

2つの描画エンジンを同じAPI・同じパラメータで切り替えられる:

  make_space_source(template, params, n_frames, w, h, fps, cam, engine="auto", seed=0,
                    cache_dir=Path("renders/cache")) -> FrameSource

  engine
    "2d"       space2d.py: numpy/OpenCVの2.5D描画。速い(1080pで1フレーム約0.1秒以下)。
               Blender不要。下書き・テスト・Blenderを入れていない人向け
    "blender"  blender_space.py をBlender(EEVEE)で実行して3D描画 → 連番PNGを
               renders/cache/<ハッシュ>/ に保存して読む。同じ指定なら2回目以降は即再利用
    "eevee"    "blender" と同じ(描画エンジンを明示)
    "cycles"   BlenderのCycles(CPU)で描画。遅いがGPUに依存しない
    "auto"     Blenderが見つかればEEVEE、見つからなければ2d

テンプレートとパラメータ(全て省略可。未知のパラメータ名はValueError)は TEMPLATES を参照。
  python -m videolab.produce.space                       一覧を表示
  python -m videolab.produce.space preview planet preset=mars --size 1920x1080
  python -m videolab.produce.space preview sun --camera zoom_in --seconds 4   (mp4)

カメラワークの向きは解析側(analyze.classify_camera)と同じ定義:
  zoom_in = 中身が大きくなる / pan_right = 中身が左へ流れる(カメラが右へ)
  orbit   = カメラが天体の周りを右へ回り込む。角度は amount×360度(amount=0.08 → 約29度)。
            天体の模様は左へ、遠くの星空は右へ流れ(視差)、太陽光の当たる向きも変わる
2.5D表現のため、ズーム・パンは背景の星空にも弱めに(視差つきで)かかる。
  2d      : 遠い星 STAR_PARALLAX[0] 倍 / 近い星 STAR_PARALLAX[1] 倍
  Blender : 変化量の BLENDER_BG_SHARE を焦点距離/レンズシフト(画面全体)で、
            残りをドリー/トラック(カメラ移動=天体だけが動く)で表現する
"""

import dataclasses
import math
import sys
from pathlib import Path

from .sources import EASINGS, CameraMove

CAMERA_VFOV_DEG = 30.0          # 両エンジン共通の縦画角(度)。orbit時の星の流れ量もこれで決まる
STAR_PARALLAX = (0.4, 0.7)      # 2d: ズーム/パンを星空レイヤー(遠・近)にかける割合
BLENDER_BG_SHARE = 0.5          # blender: ズーム/パンのうち画面全体(背景込み)にかける割合

ENGINES = ("auto", "2d", "blender", "eevee", "cycles")

# ---------------------------------------------------------------- プリセット(両エンジン共通)
# 色はsRGBの16進。kind は描き方の系統。atmosphere は大気の縁の光(Noneで無し)。
PRESETS = {
    "earth": {
        "kind": "earth", "atmosphere": "#5ea4ff", "atm_strength": 1.0, "rings": False,
        "tilt": 23.4, "inclination": 12.0, "clouds": True, "night_lights": True,
        "diameter_km": 12742, "longitude": None,
        "colors": {"ocean_deep": "#07214d", "ocean": "#12497e", "ocean_shallow": "#1f6f9e",
                   "land_low": "#3d6a2e", "land_forest": "#2a4d22", "land_high": "#6f6247",
                   "desert": "#c2a36b", "tundra": "#8d8a74", "ice": "#f1f5f8",
                   "cloud": "#ffffff", "city": "#ffb35a"},
    },
    "mars": {
        "kind": "rocky", "atmosphere": "#e8a47a", "atm_strength": 0.35, "rings": False,
        "tilt": 25.2, "inclination": 12.0, "clouds": False, "night_lights": False,
        "diameter_km": 6779, "craters": 0.45, "polar_caps": True,
        "colors": {"base": "#b85a34", "light": "#d99462", "dark": "#6b2f1d",
                   "crater": "#8f4428", "ice": "#f3eee8"},
    },
    "jupiter": {
        "kind": "gas", "atmosphere": "#efdcb8", "atm_strength": 0.3, "rings": False,
        "tilt": 3.1, "inclination": 6.0, "clouds": False, "night_lights": False,
        "diameter_km": 139820, "storm": True, "longitude": 28.0,
        # 帯: (南端の緯度, 色キー) を北から順に
        "bands": [(66, "pole"), (58, "zone2"), (53, "belt"), (48, "zone2"), (43, "belt"),
                  (37, "zone"), (31, "belt"), (26, "zone2"), (20, "zone"), (15, "belt_dark"),
                  (8, "belt"), (-5, "zone2"), (-11, "belt"), (-19, "belt_dark"), (-27, "zone"),
                  (-33, "belt"), (-38, "zone2"), (-44, "belt"), (-50, "zone2"), (-57, "belt"),
                  (-66, "zone2"), (-90, "pole")],
        "colors": {"zone": "#ebe1cd", "zone2": "#dcc7a3", "belt": "#bb916c",
                   "belt_dark": "#9a6d4f", "pole": "#9c9486", "storm": "#c8694a",
                   "oval": "#f6f1e8"},
    },
    "saturn": {
        "kind": "gas", "atmosphere": "#f0dcae", "atm_strength": 0.25, "rings": True,
        "tilt": 26.7, "inclination": 24.0, "clouds": False, "night_lights": False,
        "diameter_km": 116460, "storm": False,
        "bands": [(70, "pole"), (55, "zone2"), (45, "belt"), (38, "zone"), (30, "belt_dark"),
                  (18, "zone2"), (-18, "zone"), (-30, "belt"), (-38, "zone2"), (-46, "belt_dark"),
                  (-56, "zone2"), (-70, "belt"), (-90, "pole")],
        "colors": {"zone": "#eadcb2", "zone2": "#dcc690", "belt": "#c7a56c",
                   "belt_dark": "#a98a58", "pole": "#a6a78e", "storm": "#e0cfa0",
                   "oval": "#f3ead0", "ring": "#d9c7a0", "ring_dark": "#8e7d62"},
    },
    "moon": {
        "kind": "rocky", "atmosphere": None, "atm_strength": 0.0, "rings": False,
        "tilt": 6.7, "inclination": 6.0, "clouds": False, "night_lights": False,
        "diameter_km": 3474.8, "craters": 1.0, "polar_caps": False,
        "colors": {"base": "#a8a69f", "light": "#c4c2bb", "dark": "#5b5955",
                   "crater": "#8a8883", "ice": "#d8d8d8"},
    },
    "venus": {
        "kind": "cloudy", "atmosphere": "#ffe0a0", "atm_strength": 0.9, "rings": False,
        "tilt": 2.6, "inclination": 6.0, "clouds": False, "night_lights": False,
        "diameter_km": 12104,
        "colors": {"base": "#e2c48a", "light": "#f4e4bc", "dark": "#b89058"},
    },
    "neptune": {
        "kind": "gas", "atmosphere": "#86b6ff", "atm_strength": 0.8, "rings": False,
        "tilt": 28.3, "inclination": 8.0, "clouds": False, "night_lights": False,
        "diameter_km": 49244, "storm": True, "longitude": 28.0,
        "bands": [(70, "pole"), (55, "zone2"), (40, "zone"), (28, "belt"), (10, "zone2"),
                  (-12, "zone"), (-28, "belt"), (-45, "zone2"), (-62, "belt_dark"), (-90, "pole")],
        "colors": {"zone": "#4b74e0", "zone2": "#3c63cf", "belt": "#3355bb",
                   "belt_dark": "#253f96", "pole": "#2f4fb0", "storm": "#1c2c70",
                   "oval": "#e6efff"},
    },
    "ice": {
        "kind": "ice", "atmosphere": None, "atm_strength": 0.0, "rings": False,
        "tilt": 3.0, "inclination": 8.0, "clouds": False, "night_lights": False,
        "diameter_km": 3121.6,
        "colors": {"base": "#d9d2c6", "light": "#f4f2ee", "blue": "#b5cfe0",
                   "line": "#8a4a2c", "dark": "#a89886"},
    },
    "lava": {
        "kind": "lava", "atmosphere": "#ff6a30", "atm_strength": 0.45, "rings": False,
        "tilt": 10.0, "inclination": 8.0, "clouds": False, "night_lights": False,
        "diameter_km": 3643,
        "colors": {"crust": "#2a1d17", "crust2": "#4a3427", "ash": "#6b5a4e",
                   "glow": "#ff4a08", "hot": "#ffcc55"},
    },
}

# ---------------------------------------------------------------- テンプレートとパラメータ
# 名前: (既定値, 型, 説明)。既定値 "auto" はプリセットに従う。
_COMMON_BG = {
    "stars": (1.0, "float", "背景の星の量(0で無し)"),
    "nebula": (0.12, "float", "背景の星雲・天の川の濃さ(0〜1)"),
}

TEMPLATES = {
    "planet": {
        "preset": ("earth", "preset", f"惑星の種類({'|'.join(PRESETS)})"),
        "texture": (None, "path", "正距円筒図法(横2:縦1)の地表画像。指定するとpresetの模様の代わりに使う"),
        "size": (0.7, "float", "惑星の直径(画面の高さに対する比)"),
        "position": ([0.5, 0.5], "xy", "惑星の中心 [x, y](画面比。0.5,0.5で中央)"),
        "rotation_speed": (6.0, "float", "自転の見かけの速さ(度/秒)。正=模様が右へ流れる(実際の自転の向き)"),
        "longitude": ("auto", "float", "最初に正面に来る経度(度)。earthの既定は陸が多い面"),
        "sun_angle": (35.0, "float", "太陽の向き(度)。0=カメラの後ろ(満月状) / 90=左から(半月) / "
                                     "180=惑星の裏(逆光) / 負の値=右から"),
        "sun_elevation": (15.0, "float", "太陽の高さ(度)。正=上から照らす"),
        "atmosphere": ("auto", "color_or_none", "大気の縁の光の色(#rrggbb / null=無し)"),
        "rings": ("auto", "bool", "環を付ける(既定はsaturnのみ)"),
        "tilt": ("auto", "float", "自転軸の傾き(画面内の回転, 度)"),
        "inclination": ("auto", "float", "北極をカメラ側へ倒す角度(度)。環の開き具合も決まる"),
        "clouds": ("auto", "bool", "雲(earthのみ)"),
        "night_lights": ("auto", "bool", "夜側の街明かり(earthのみ)"),
        **_COMMON_BG,
    },
    "planet_compare": {
        "presets": (["earth", "moon"], "presets2", "左右に並べる2つの惑星 [a, b]"),
        "textures": ([None, None], "paths2", "それぞれの地表画像 [a, b](null=プリセットの模様)"),
        "size_ratio": ("auto", "float", "直径の比 b/a(既定は実際の直径比)"),
        "separation": (0.06, "float", "2つの惑星の縁の間隔(画面の幅に対する比)"),
        "size": (0.62, "float", "大きい方の直径(画面の高さに対する比)"),
        "rotation_speed": (6.0, "float", "自転の見かけの速さ(度/秒)"),
        "sun_angle": (35.0, "float", "太陽の向き(度)。planetと同じ"),
        "sun_elevation": (15.0, "float", "太陽の高さ(度)"),
        **_COMMON_BG,
    },
    "sun": {
        "color": ("#ffb347", "color", "太陽(恒星)の色"),
        "activity": (0.5, "float", "活動度(0〜1)。黒点・コロナの筋・プロミネンスの量"),
        "size": (0.6, "float", "直径(画面の高さに対する比)"),
        "position": ([0.5, 0.5], "xy", "中心 [x, y](画面比)"),
        "rotation_speed": (2.0, "float", "自転の見かけの速さ(度/秒)"),
        **_COMMON_BG,
    },
    "black_hole": {
        "disk_color": ("#ffae5c", "color", "降着円盤の色"),
        "tilt": (9.0, "float", "円盤を見下ろす角度(度)。0=真横 / 90=真上"),
        "roll": (0.0, "float", "画面内での傾き(度)"),
        "size": (0.2, "float", "黒い影(シャドウ)の直径(画面の高さに対する比)。円盤は約4倍に広がる"),
        "position": ([0.5, 0.5], "xy", "中心 [x, y](画面比)"),
        "spin_speed": (1.0, "float", "円盤の回転の速さ(倍率)"),
        "lensing": (True, "bool", "背景の星空を重力レンズで歪ませる(2dのみ)"),
        **_COMMON_BG,
    },
    "starfield": {
        "density": (1.0, "float", "星の量"),
        "speed": (0.0, "float", "前進飛行の速さ(0=静止 / 1=ゆっくり / 4=ワープ)"),
        "color_variation": (0.5, "float", "星の色温度のばらつき(0=白一色〜1)"),
        "nebula": (0.35, "float", "星雲・天の川の濃さ(0〜1)"),
    },
}


def _is_hex(c) -> bool:
    if not isinstance(c, str):
        return False
    s = c.lstrip("#")
    if len(s) != 6:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def hex_rgb(c: str):
    """'#rrggbb' → (r, g, b) 0〜1 のsRGB。"""
    s = c.lstrip("#")
    return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _check(template: str, name: str, value, kind: str):
    where = f"space/{template}.{name}"
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{where} は数値で指定してください: {value!r}") from None
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "yes", "on", "false", "no", "off"):
            return value.lower() in ("true", "yes", "on")
        if isinstance(value, (int, float)):
            return bool(value)
        raise ValueError(f"{where} は true / false で指定してください: {value!r}")
    if kind == "preset":
        if value not in PRESETS:
            raise ValueError(f"{where}: 未知のプリセット '{value}' (使えるもの: {', '.join(PRESETS)})")
        return value
    if kind == "presets2":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"{where} は2つのプリセットのリストで指定してください: 例 [earth, moon]")
        return [_check(template, name, v, "preset") for v in value]
    if kind == "path":
        if value in (None, ""):
            return None
        p = Path(str(value))
        if not p.exists():
            raise ValueError(f"{where}: 画像ファイルがありません: {p}")
        return str(p.resolve())
    if kind == "paths2":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"{where} は [a, b] の2要素で指定してください(不要な方はnull)")
        return [_check(template, name, v, "path") for v in value]
    if kind == "xy":
        try:
            x, y = (float(v) for v in value)
        except (TypeError, ValueError):
            raise ValueError(f"{where} は [x, y] の2つの数値で指定してください: {value!r}") from None
        return [x, y]
    if kind == "color":
        if not _is_hex(value):
            raise ValueError(f"{where} は #rrggbb 形式の色で指定してください: {value!r}")
        return "#" + value.lstrip("#").lower()
    if kind == "color_or_none":
        if value in (None, False) or (isinstance(value, str) and value.lower() in ("none", "null", "")):
            return None
        return _check(template, name, value, "color")
    raise AssertionError(kind)


def resolve_params(template: str, params: dict = None) -> dict:
    """既定値とプリセットを埋めた完全なパラメータを返す(両エンジンはこれだけを見る)。"""
    if template not in TEMPLATES:
        raise ValueError(f"未知の宇宙テンプレート: '{template}' (使えるもの: {', '.join(TEMPLATES)})")
    params = dict(params or {})
    if params.get("_resolved") == template:          # 解決済みならそのまま
        return params
    spec = TEMPLATES[template]
    unknown = sorted(set(params) - set(spec))
    if unknown:
        raise ValueError(f"space/{template} に未知のパラメータ: {', '.join(unknown)}\n"
                         f"  使えるもの: {', '.join(spec)}")
    out = {}
    for name, (default, kind, _) in spec.items():
        v = params.get(name, default)
        if v == "auto" and kind != "color":
            out[name] = "auto"
        elif v is None and kind in ("float", "bool", "xy", "preset", "color"):
            out[name] = "auto" if default == "auto" else _check(template, name, default, kind)
        else:
            out[name] = _check(template, name, v, kind)
    if template == "planet":
        pr = PRESETS[out["preset"]]
        for key in ("atmosphere", "rings", "tilt", "inclination", "clouds", "night_lights"):
            if out[key] == "auto":
                out[key] = pr[key]
        if out["longitude"] == "auto":
            out["longitude"] = pr.get("longitude", 0.0)     # None = エンジン任せ(earth: 陸が多い面)
            if out["longitude"] is None and out["texture"]:
                out["longitude"] = 0.0
        if out["texture"] and "clouds" not in params:
            out["clouds"] = False                     # 画像を貼るときは雲・街明かりは画像側に任せる
        if out["texture"] and "night_lights" not in params:
            out["night_lights"] = False
        if "atmosphere" in params and out["atmosphere"] is not None and params["atmosphere"] != "auto":
            out["atm_strength"] = max(0.5, pr["atm_strength"])
        else:
            out["atm_strength"] = pr["atm_strength"] if out["atmosphere"] else 0.0
    elif template == "planet_compare":
        a, b = out["presets"]
        if out["size_ratio"] == "auto":
            out["size_ratio"] = PRESETS[b]["diameter_km"] / PRESETS[a]["diameter_km"]
        out["size_ratio"] = max(1e-3, out["size_ratio"])
    elif template == "sun":
        out["activity"] = min(1.0, max(0.0, out["activity"]))
    elif template == "starfield":
        out["color_variation"] = min(1.0, max(0.0, out["color_variation"]))
        out["speed"] = max(0.0, out["speed"])
    if "size" in out and out["size"] <= 0:
        raise ValueError(f"space/{template}.size は0より大きい数値で指定してください"
                         f"(画面の高さに対する比。例 0.6): {out['size']}")
    if "separation" in out:
        out["separation"] = max(0.0, out["separation"])
    for k in ("stars", "density"):
        if k in out:
            out[k] = max(0.0, out[k])
    if "nebula" in out:
        out["nebula"] = min(1.0, max(0.0, out["nebula"]))
    out["_resolved"] = template
    return out


def compare_layout(p: dict, w: int, h: int):
    """planet_compare の2惑星の (中心x, 中心y, 直径) を画面比で返す [(a), (b)]。"""
    ratio = p["size_ratio"]
    big = p["size"]
    da, db = (big, big * ratio) if ratio <= 1 else (big / ratio, big)
    aspect = w / h
    gap = p["separation"] * aspect                   # 高さ比に換算
    total = da + db + gap
    limit = 0.9 * aspect
    if total > limit:                                # 横に収まらなければ全体を縮める
        k = limit / total
        da, db, gap, total = da * k, db * k, gap * k, limit
    x0 = (aspect - total) / 2
    ca = (x0 + da / 2) / aspect
    cb = (x0 + da + gap + db / 2) / aspect
    return [(ca, 0.5, da), (cb, 0.5, db)]


# ---------------------------------------------------------------- カメラ(両エンジン共通)

def orbit_degrees(cam: CameraMove, u: float) -> float:
    """orbit の回り込み角(度)。amount×360度をイージングつきで進める。"""
    if cam.kind != "orbit":
        return 0.0
    e = EASINGS[cam.easing](min(1.0, max(0.0, u)))
    return cam.amount * 360.0 * e


def camera_state(cam: CameraMove, u: float):
    """進行度uでの (拡大率, 中身のx移動, 中身のy移動, 回り込み角度)。移動は画面比。"""
    s, ox, oy = cam.at(u)
    return s, ox, oy, orbit_degrees(cam, u)


def camera_track(cam: CameraMove, n_frames: int) -> list:
    return [[round(v, 7) for v in camera_state(cam, i / max(1, n_frames - 1))]
            for i in range(n_frames)]


def sun_vector(sun_angle: float, sun_elevation: float, orbit_deg: float = 0.0):
    """太陽方向の単位ベクトル(カメラ座標: x右 / y上 / z手前)。

    カメラが右へ回り込むと(orbit)、空間に固定された太陽はカメラから見て左へずれる。
    """
    a = math.radians(sun_angle + orbit_deg)
    e = math.radians(sun_elevation)
    return (-math.sin(a) * math.cos(e), math.sin(e), math.cos(a) * math.cos(e))


# ---------------------------------------------------------------- 窓口

# engine="auto" でBlenderが失敗した (テンプレート, エンジン) を覚えておき、同じ実行中は
# 起動し直さない(1ショットごとに起動→失敗を繰り返して時間を浪費しないため)
_BLENDER_FAILED = {}
_FALLBACK_SHOTS = [0]


def reset_auto_state():
    _BLENDER_FAILED.clear()
    _FALLBACK_SHOTS[0] = 0


def fallback_count() -> int:
    """この実行中、Blenderの失敗で2dに切り替えたショット数。"""
    return _FALLBACK_SHOTS[0]


def compensate_parallax(cam: CameraMove, k: float) -> CameraMove:
    """星空に奥行き(パララックス)があるぶん、背景の動きが指定量どおりになるよう動きを増やす。

    解析(estimate_camera)は画面全体=主に星の動きを測るので、amount は「解析で測れる動き」を
    意味するように揃える(天体そのものは奥行きのぶん大きく動く)。構図(frame_*)は変えない。
    """
    if k <= 0 or k >= 1:
        return cam
    if cam.kind.startswith("zoom"):
        return dataclasses.replace(cam, amount=(1.0 + cam.amount) ** (1.0 / k) - 1.0)
    if cam.kind.startswith(("pan", "tilt")):
        return dataclasses.replace(cam, amount=cam.amount / k)
    return cam


def make_space_source(template: str, params: dict, n_frames: int, w: int, h: int, fps: float,
                      cam: CameraMove = None, engine: str = "auto", seed: int = 0,
                      cache_dir: Path = Path("renders/cache")):
    """宇宙シーンの FrameSource を作る。engine は "auto" / "2d" / "blender" / "eevee" / "cycles"。"""
    cam = cam or CameraMove()
    engine = (engine or "auto").lower()
    if engine not in ENGINES:
        raise ValueError(f"未知の描画エンジン: {engine} (使えるもの: {', '.join(ENGINES)})")
    p = resolve_params(template, params)
    if engine in ("auto", "blender", "eevee", "cycles"):
        from . import blender_runner

        exe = blender_runner.find_blender()
        if exe is None and engine != "auto":
            raise FileNotFoundError(blender_runner.INSTALL_HINT)
        if exe is not None:
            render_engine = "cycles" if engine == "cycles" else "eevee"
            key = (template, render_engine)
            if engine == "auto" and key in _BLENDER_FAILED:
                _FALLBACK_SHOTS[0] += 1        # 既に失敗済み → 起動せず2dへ
            else:
                try:
                    return blender_runner.render_space(
                        template, p, n_frames, w, h, fps, compensate_parallax(cam, BLENDER_BG_SHARE),
                        seed=seed, cache_dir=Path(cache_dir), engine=render_engine, blender=exe)
                except (RuntimeError, OSError) as e:
                    if engine != "auto":
                        raise
                    first = str(e).splitlines()[0] if str(e) else e.__class__.__name__
                    _BLENDER_FAILED[key] = first
                    _FALLBACK_SHOTS[0] += 1
                    print(f"\n  [注意] Blenderでの描画に失敗したので、この実行中の {template} は2dで描きます: "
                          f"{first}", file=sys.stderr, flush=True)
    from .space2d import Space2DSource

    return Space2DSource(template, p, n_frames, w, h, fps,
                         compensate_parallax(cam, sum(STAR_PARALLAX) / 2), seed=seed)


def describe_templates() -> str:
    lines = []
    for t, spec in TEMPLATES.items():
        lines.append(f"[{t}]")
        for name, (default, _, desc) in spec.items():
            lines.append(f"  {name:15s} 既定={default!s:18s} {desc}")
    return "\n".join(lines)


def _parse_value(v: str):
    """コマンドラインの key=value の値を YAML として解釈する(数値・true・[0.3, 0.5] など)。"""
    import yaml

    try:
        return yaml.safe_load(v)
    except yaml.YAMLError:
        return v


def main(argv=None):
    """python -m videolab.produce.space [preview テンプレート key=value ...]"""
    import argparse

    import cv2

    ap = argparse.ArgumentParser(prog="python -m videolab.produce.space",
                                 description="宇宙テンプレートの一覧表示と試し描き")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="テンプレートとパラメータの一覧(既定)")
    pv = sub.add_parser("preview", help="1枚(または短い動画)を試し描きする")
    pv.add_argument("template", choices=list(TEMPLATES))
    pv.add_argument("params", nargs="*", help="key=value 例: preset=mars size=0.5 position=[0.6,0.5]")
    pv.add_argument("--engine", default="2d", choices=ENGINES)
    pv.add_argument("--size", default="1280x720", help="解像度 例 1920x1080")
    pv.add_argument("--camera", default="static", help="カメラワーク 例 zoom_in / pan_right / orbit")
    pv.add_argument("--seconds", type=float, default=0.0, help="0より大きいと mp4 を書き出す")
    pv.add_argument("--fps", type=float, default=30.0)
    pv.add_argument("--seed", type=int, default=0)
    pv.add_argument("--out", default=None, help="出力先(既定: preview_<テンプレート>.png / .mp4)")
    a = ap.parse_args(argv)
    if a.cmd in (None, "list"):
        print(describe_templates())
        return
    params = {}
    for kv in a.params:
        if "=" not in kv:
            raise SystemExit(f"key=value の形で指定してください: {kv}")
        k, v = kv.split("=", 1)
        params[k] = _parse_value(v)
    w, h = (int(x) for x in a.size.lower().split("x"))
    n = max(1, int(round(a.seconds * a.fps))) if a.seconds > 0 else 1
    src = make_space_source(a.template, params, n, w, h, a.fps, CameraMove(a.camera), engine=a.engine,
                            seed=a.seed)
    if n == 1:
        out = Path(a.out or f"preview_{a.template}.png")
        ok, buf = cv2.imencode(out.suffix or ".png", cv2.cvtColor(src.frame(0), cv2.COLOR_RGB2BGR))
        out.write_bytes(buf.tobytes())               # 日本語パスでも書けるように
        print(f"書き出しました: {out}")
        return
    import subprocess

    from .. import ffmpeg_util as ff

    out = Path(a.out or f"preview_{a.template}.mp4")
    cmd = [ff.find_ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", f"{a.fps}", "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", "20", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        proc.stdin.write(src.frame(i).tobytes())
    proc.stdin.close()
    proc.wait()
    print(f"書き出しました: {out}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
