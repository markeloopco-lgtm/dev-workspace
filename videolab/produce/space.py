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
`python -m videolab.produce.space` で一覧を表示できる。

カメラワークの向きは解析側(analyze.classify_camera)と同じ定義:
  zoom_in = 中身が大きくなる / pan_right = 中身が左へ流れる(カメラが右へ)
  orbit   = カメラが天体の周りを右へ回り込む。角度は amount×360度(amount=0.08 → 約29度)。
            天体の模様は左へ、遠くの星空は右へ流れ(視差)、太陽光の当たる向きも変わる
2.5D表現のため、ズーム・パンは背景の星空にも弱めに(視差つきで)かかる。
  2d      : 遠い星 STAR_PARALLAX[0] 倍 / 近い星 STAR_PARALLAX[1] 倍
  Blender : 変化量の BLENDER_BG_SHARE を焦点距離/レンズシフト(画面全体)で、
            残りをドリー/トラック(カメラ移動=天体だけが動く)で表現する
"""

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
        "diameter_km": 12742,
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
        "colors": {"zone": "#ece2cc", "zone2": "#dcc8a4", "belt": "#b87c52",
                   "belt_dark": "#8b5538", "pole": "#8d8676", "storm": "#c4583a",
                   "oval": "#f6f1e8"},
    },
    "saturn": {
        "kind": "gas", "atmosphere": "#f0dcae", "atm_strength": 0.25, "rings": True,
        "tilt": 26.7, "inclination": 24.0, "clouds": False, "night_lights": False,
        "diameter_km": 116460, "storm": False,
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
        "colors": {"zone": "#4b74e0", "zone2": "#3c63cf", "belt": "#3355bb",
                   "belt_dark": "#253f96", "pole": "#2f4fb0", "storm": "#1c2c70",
                   "oval": "#e6efff"},
    },
    "ice": {
        "kind": "ice", "atmosphere": None, "atm_strength": 0.0, "rings": False,
        "tilt": 3.0, "inclination": 8.0, "clouds": False, "night_lights": False,
        "diameter_km": 3121.6,
        "colors": {"base": "#d9d2c6", "light": "#f4f2ee", "blue": "#b5cfe0",
                   "line": "#94603e", "dark": "#a89886"},
    },
    "lava": {
        "kind": "lava", "atmosphere": "#ff6a30", "atm_strength": 0.45, "rings": False,
        "tilt": 10.0, "inclination": 8.0, "clouds": False, "night_lights": False,
        "diameter_km": 3643,
        "colors": {"crust": "#1d1512", "crust2": "#3d2b22", "ash": "#5a4a40",
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
        "longitude": ("auto", "float", "最初に正面に来る経度(度)"),
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
            out["longitude"] = pr.get("longitude", 0.0)
        if out["texture"] and "clouds" not in params:
            out["clouds"] = False                     # 画像を貼るときは雲・街明かりは画像側に任せる
        if out["texture"] and "night_lights" not in params:
            out["night_lights"] = False
        if "atmosphere" in params and out["atmosphere"] is not None and params["atmosphere"] != "auto":
            out["atm_strength"] = max(0.5, pr["atm_strength"])
        else:
            out["atm_strength"] = pr["atm_strength"] if out["atmosphere"] else 0.0
        out["size"] = max(0.01, out["size"])
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
            return blender_runner.render_space(template, p, n_frames, w, h, fps, cam, seed=seed,
                                               cache_dir=Path(cache_dir), engine=render_engine,
                                               blender=exe)
    from .space2d import Space2DSource

    return Space2DSource(template, p, n_frames, w, h, fps, cam, seed=seed)


def describe_templates() -> str:
    lines = []
    for t, spec in TEMPLATES.items():
        lines.append(f"[{t}]")
        for name, (default, _, desc) in spec.items():
            lines.append(f"  {name:15s} 既定={default!s:18s} {desc}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
    print(describe_templates())
