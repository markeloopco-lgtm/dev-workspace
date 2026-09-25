"""Blender用: 宇宙シーン(惑星・太陽・ブラックホール・星空)を手続き的に組んで連番PNGを描画する。

blender_runner.py から次の形で呼ばれる(手で実行してもよい):
  blender -b --factory-startup -P blender_space.py -- --spec spec.json --out <出力フォルダ>
bpyモジュール版のPythonでも動く:
  python blender_space.py -- --spec spec.json --out <出力フォルダ>

対応: Blender 4.2 LTS 〜 5.x。外部素材は不要(地表画像 texture を指定した時だけ読む)。
  - EEVEEのエンジン名は 4.2〜4.5 が BLENDER_EEVEE_NEXT、5.0 以降が BLENDER_EEVEE → 列挙値で判定
  - コンポジター(5.0でAPIが変わった)は使わない。光のにじみは発光シェーダーで表現する
  - 半透明は material.surface_render_method(4.2+)で指定し、古い blend_method は使わない
  - 模様はシェーダーノード(Noise / Voronoi / ColorRamp / Math)だけで作る
  - アニメーションは全フレームにキーフレームを打ち、1フレームずつ描画する(途中から再開できる)

spec.json は blender_runner.build_spec() が作る。座標とカメラワークの約束は space.py と同じ:
  画面 x右 / y下、zoom_in = 中身が大きくなる、pan_right = 中身が左へ、orbit = カメラが右へ回り込む。
  ズーム・パンの BG_SHARE 分はレンズ(焦点距離・レンズシフト = 背景の星ごと動く)、
  残りはドリー/トラック(カメラ移動 = 天体だけが動く)で表す。
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

import bpy  # noqa: E402  (bpyモジュール版では bpy を先に読み込まないと bmesh/mathutils が無い)
import bmesh  # noqa: E402
from mathutils import Matrix, Vector  # noqa: E402

SCRIPT_VERSION = 3


# ================================================================ 引数・色

def parse_args(argv):
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    ap = argparse.ArgumentParser(prog="blender_space.py")
    ap.add_argument("--spec", required=True, help="blender_runner が書いた spec.json")
    ap.add_argument("--out", default=None, help="連番PNGの出力フォルダ(省略時は spec.json の隣)")
    ap.add_argument("--save-blend", default=None, help="組んだシーンを .blend にも保存する")
    ap.add_argument("--frames", default=None, help="描画するフレーム範囲 例 1-10(確認用)")
    return ap.parse_args(args)


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgba(hexstr: str, k: float = 1.0):
    """'#rrggbb'(sRGB) → Blenderのノード用の線形RGBA。"""
    s = hexstr.lstrip("#")
    r, g, b = (int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (_lin(r) * k, _lin(g) * k, _lin(b) * k, 1.0)


def mix_col(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))


# ================================================================ ノード組み立ての補助

class NB:
    """シェーダーノードを短く書くための補助。数値を渡すと既定値に、ソケットを渡すと接続する。"""

    def __init__(self, nt):
        self.nt = nt
        self.x = 0

    def node(self, kind, **props):
        n = self.nt.nodes.new(kind)
        for k, v in props.items():
            setattr(n, k, v)
        n.location = (self.x, 0)
        self.x += 30
        return n

    def put(self, sock, v):
        if isinstance(v, bpy.types.NodeSocket):
            self.nt.links.new(v, sock)
        elif v is not None:
            try:
                sock.default_value = v
            except (TypeError, ValueError):
                sock.default_value = (v, v, v) if len(sock.default_value) == 3 else (v, v, v, 1.0)

    @staticmethod
    def sock(sockets, key):
        """名前またはidentifierで(有効な)ソケットを探す。"""
        for s in sockets:
            if s.identifier == key:
                return s
        for s in sockets:
            if s.name == key and getattr(s, "enabled", True):
                return s
        for s in sockets:
            if s.name == key:
                return s
        raise KeyError(key)

    def math(self, op, a, b=None, c=None, clamp=False):
        n = self.node("ShaderNodeMath", operation=op, use_clamp=clamp)
        self.put(n.inputs[0], a)
        if b is not None:
            self.put(n.inputs[1], b)
        if c is not None:
            self.put(n.inputs[2], c)
        return n.outputs[0]

    def vmath(self, op, a, b=None, scale=None):
        n = self.node("ShaderNodeVectorMath", operation=op)
        self.put(n.inputs[0], a)
        if b is not None:
            self.put(n.inputs[1], b)
        if scale is not None:
            self.put(self.sock(n.inputs, "Scale"), scale)
        return n.outputs["Value"] if op in ("DOT_PRODUCT", "LENGTH", "DISTANCE") else n.outputs["Vector"]

    def maprange(self, v, a0, a1, b0=0.0, b1=1.0, clamp=True, smooth=False):
        n = self.node("ShaderNodeMapRange", clamp=clamp,
                      interpolation_type="SMOOTHSTEP" if smooth else "LINEAR")
        self.put(n.inputs["Value"], v)
        self.put(n.inputs["From Min"], a0)
        self.put(n.inputs["From Max"], a1)
        self.put(n.inputs["To Min"], b0)
        self.put(n.inputs["To Max"], b1)
        return n.outputs["Result"]

    def mix(self, fac, a, b, blend="MIX"):
        n = self.node("ShaderNodeMix", data_type="RGBA", blend_type=blend)
        self.put(self.sock(n.inputs, "Factor_Float"), fac)
        self.put(self.sock(n.inputs, "A_Color"), a)
        self.put(self.sock(n.inputs, "B_Color"), b)
        return self.sock(n.outputs, "Result_Color")

    def ramp(self, fac, stops, interp="LINEAR"):
        """stops: [(位置, RGBA), ...](32個まで)。"""
        n = self.node("ShaderNodeValToRGB")
        cr = n.color_ramp
        cr.interpolation = interp
        stops = sorted(stops, key=lambda s: s[0])[:32]
        while len(cr.elements) < len(stops):
            cr.elements.new(0.5)
        for el, (pos, col) in zip(cr.elements, stops):
            el.position = min(1.0, max(0.0, pos))
            el.color = col
        self.put(n.inputs["Fac"], fac)
        return n.outputs["Color"]

    def noise(self, vec, scale, detail=4.0, rough=0.5, distortion=0.0):
        n = self.node("ShaderNodeTexNoise")
        if hasattr(n, "noise_dimensions"):
            n.noise_dimensions = "3D"
        if hasattr(n, "normalize"):
            n.normalize = True
        self.put(n.inputs["Vector"], vec)
        self.put(n.inputs["Scale"], scale)
        self.put(n.inputs["Detail"], detail)
        self.put(n.inputs["Roughness"], rough)
        self.put(n.inputs["Distortion"], distortion)
        return n.outputs["Fac"]

    def noise_node(self, vec, scale, detail=4.0, rough=0.5):
        n = self.node("ShaderNodeTexNoise")
        if hasattr(n, "noise_dimensions"):
            n.noise_dimensions = "3D"
        self.put(n.inputs["Vector"], vec)
        self.put(n.inputs["Scale"], scale)
        self.put(n.inputs["Detail"], detail)
        self.put(n.inputs["Roughness"], rough)
        return n

    @staticmethod
    def node_out_color(n):
        """Noiseノードの色出力を -0.5〜0.5 付近のベクトルとして使う(ゆがみ用)。"""
        return n.outputs["Color"]

    def voronoi(self, vec, scale, feature="F1", randomness=1.0):
        n = self.node("ShaderNodeTexVoronoi", feature=feature)
        if hasattr(n, "voronoi_dimensions"):
            n.voronoi_dimensions = "3D"
        self.put(n.inputs["Vector"], vec)
        self.put(n.inputs["Scale"], scale)
        self.put(n.inputs["Randomness"], randomness)
        return n

    def combine(self, x, y, z):
        n = self.node("ShaderNodeCombineXYZ")
        self.put(n.inputs[0], x)
        self.put(n.inputs[1], y)
        self.put(n.inputs[2], z)
        return n.outputs[0]

    def separate(self, v):
        n = self.node("ShaderNodeSeparateXYZ")
        self.put(n.inputs[0], v)
        return n.outputs[0], n.outputs[1], n.outputs[2]

    def mapping(self, vec, loc=(0, 0, 0), scale=(1, 1, 1)):
        n = self.node("ShaderNodeMapping", vector_type="POINT")
        self.put(n.inputs["Vector"], vec)
        n.inputs["Location"].default_value = loc
        n.inputs["Scale"].default_value = scale
        return n.outputs["Vector"]

    def value(self, v):
        n = self.node("ShaderNodeValue")
        n.outputs[0].default_value = v
        return n

    def const_vec(self, v):
        return self.combine(float(v[0]), float(v[1]), float(v[2]))

    def smoothstep(self, v, e0, e1):
        return self.maprange(v, e0, e1, 0.0, 1.0, True, True)


def new_material(name):
    m = bpy.data.materials.new(name)
    if m.node_tree is None:                # 4.x。5.0以降は常にノード
        m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    return m, NB(nt), out


def set_blended(m, backface_cull=True):
    """半透明(加算の光など)。4.2+ は surface_render_method。古い blend_method は使わない。"""
    if hasattr(m, "surface_render_method"):
        m.surface_render_method = "BLENDED"
    if hasattr(m, "use_transparent_shadow"):
        m.use_transparent_shadow = True
    m.use_backface_culling = backface_cull
    if hasattr(m, "use_backface_culling_shadow"):
        m.use_backface_culling_shadow = False


def principled(nb, base=None, rough=0.9, spec=0.3, emis_col=None, emis_str=None, normal=None):
    p = nb.node("ShaderNodeBsdfPrincipled")
    nb.put(p.inputs["Base Color"], base)
    nb.put(p.inputs["Roughness"], rough)
    try:
        nb.put(p.inputs["Specular IOR Level"], spec)
    except KeyError:
        pass
    if emis_col is not None:
        nb.put(p.inputs["Emission Color"], emis_col)
        nb.put(p.inputs["Emission Strength"], 1.0 if emis_str is None else emis_str)
    if normal is not None:
        nb.put(p.inputs["Normal"], normal)
    return p.outputs[0]


def to_linear(nb, v, gain=1.0):
    """space2d の明るさ(表示空間, 0〜1付近)を発光の強さ(線形)へ: (v×gain)^2.2。

    2Dエンジンは表示空間で足し算しているので、同じ見た目にするには2.2乗が要る
    (そのままだと暗い所がsRGB変換で持ち上がり、光が広がって見える)。
    """
    return nb.math("POWER", nb.math("MAXIMUM", nb.math("MULTIPLY", v, gain), 0.0), 2.2)


def glow_shader(nb, color, strength):
    """透明+発光(加算)。裏面は消す。"""
    geo = nb.node("ShaderNodeNewGeometry")
    front = nb.math("SUBTRACT", 1.0, geo.outputs["Backfacing"])
    st = nb.math("MULTIPLY", strength, front)
    em = nb.node("ShaderNodeEmission")
    nb.put(em.inputs["Color"], color)
    nb.put(em.inputs["Strength"], st)
    tr = nb.node("ShaderNodeBsdfTransparent")
    add = nb.node("ShaderNodeAddShader")
    nb.put(add.inputs[0], tr.outputs[0])
    nb.put(add.inputs[1], em.outputs[0])
    return add.outputs[0]


def impact_param(nb, shell_r):
    """殻(半径shell_r)の表面の点を通る視線の、中心からの最短距離(=画面上の中心からの距離)。"""
    geo = nb.node("ShaderNodeNewGeometry")
    c = nb.vmath("DOT_PRODUCT", geo.outputs["Normal"], geo.outputs["Incoming"])
    c2 = nb.math("MULTIPLY", c, c)
    return nb.math("MULTIPLY", nb.math("SQRT", nb.math("SUBTRACT", 1.0, c2, clamp=True)), shell_r)


# ================================================================ メッシュ

def link(obj):
    bpy.context.scene.collection.objects.link(obj)
    return obj


def uv_sphere(name, segments=128, rings=64, radius=1.0):
    bm = bmesh.new()
    bm.loops.layers.uv.new("UVMap")
    bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=rings, radius=radius, calc_uvs=True)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))
    return link(bpy.data.objects.new(name, me))


def annulus(name, r0, r1, segments=256, rings=1):
    me = bpy.data.meshes.new(name)
    verts, faces = [], []
    for j in range(rings + 1):
        r = r0 + (r1 - r0) * j / rings
        for i in range(segments):
            a = 2 * math.pi * i / segments
            verts.append((r * math.cos(a), r * math.sin(a), 0.0))
    for j in range(rings):
        for i in range(segments):
            a, b = j * segments + i, j * segments + (i + 1) % segments
            faces.append((a, a + segments, b + segments, b))       # 反時計回り = 法線+Z
    me.from_pydata(verts, [], faces)
    me.update()
    return link(bpy.data.objects.new(name, me))


def empty(name, loc=(0, 0, 0)):
    o = bpy.data.objects.new(name, None)
    o.location = loc
    return link(o)


# ================================================================ 背景の星空(ワールド)

_STAR_RAMP = [(0.0, "#ff9a5c"), (0.18, "#ffc890"), (0.4, "#fff1e0"), (0.6, "#ffffff"),
              (0.8, "#dfe8ff"), (1.0, "#aac4ff")]


def build_world(spec, density, nebula, cvar, bg_scale=1.0):
    w = bpy.data.worlds.new("space")
    bpy.context.scene.world = w
    if w.node_tree is None:
        w.use_nodes = True
    nt = w.node_tree
    nt.nodes.clear()
    nb = NB(nt)
    out = nb.node("ShaderNodeOutputWorld")
    bg = nb.node("ShaderNodeBackground")
    tc = nb.node("ShaderNodeTexCoord")
    d = nb.vmath("NORMALIZE", tc.outputs["Generated"])
    seed = spec["seed"]
    off = ((seed * 7.31) % 97.0, (seed * 3.17) % 89.0, (seed * 5.77) % 83.0)
    ds = nb.vmath("ADD", d, nb.const_vec(off))
    frame_sr = spec["frame_solid_angle"]
    ramp = [(p, mix_col((1, 1, 1, 1), rgba(c), min(1.0, cvar * 1.5))) for p, c in _STAR_RAMP]
    total = None
    # (Voronoiの細かさ, 画面内の目標の星の数, 星の半径(セル単位), 明るさ)
    for scale, count, rv, bright in ((380.0, 2600, 0.22, 0.9), (150.0, 260, 0.18, 2.2),
                                     (60.0, 30, 0.12, 6.0)):
        if density <= 0:
            break
        scale *= bg_scale
        frac = count * density * (4 * math.pi / frame_sr) / (8 * math.pi * scale * scale * rv)
        frac = min(0.9, frac)
        vor = nb.voronoi(ds, scale)
        rnd_r, rnd_g, rnd_b = nb.separate(vor.outputs["Color"])
        present = nb.math("GREATER_THAN", rnd_r, 1.0 - frac)
        core = nb.maprange(vor.outputs["Distance"], 0.0, rv, 1.0, 0.0)
        core = nb.math("POWER", core, 2.0)
        amt = nb.math("MULTIPLY", nb.math("MULTIPLY", core, present),
                      nb.math("MULTIPLY_ADD", rnd_b, bright * 0.8, bright * 0.2))
        tint = nb.ramp(rnd_g, ramp)
        layer = nb.vmath("SCALE", tint, scale=amt)
        total = layer if total is None else nb.vmath("ADD", total, layer)
    if nebula > 0:
        ax = Vector((0.25, 0.55, 0.8)).normalized()
        dd = nb.math("ABSOLUTE", nb.vmath("DOT_PRODUCT", d, nb.const_vec(ax)))
        band = nb.maprange(dd, 0.0, 0.3, 1.0, 0.0, smooth=True)
        n1 = nb.noise(ds, 3.0, 6.0, 0.6)
        n2 = nb.noise(ds, 8.0, 4.0, 0.5)
        dust = nb.smoothstep(n2, 0.5, 0.68)
        core = nb.math("MULTIPLY", band, nb.math("SUBTRACT", n1, nb.math("MULTIPLY", dust, 0.35), clamp=True))
        mw = nb.vmath("SCALE", nb.const_vec(rgba("#d6cdbf")[:3]), scale=nb.math("MULTIPLY", core, 0.05 * nebula))
        cl = nb.math("POWER", nb.smoothstep(n1, 0.45, 0.85), 2.0)
        hue = nb.mix(nb.smoothstep(n2, 0.35, 0.65), rgba("#6c3fd0"), rgba("#1f7fa8"))
        neb = nb.vmath("SCALE", hue, scale=nb.math("MULTIPLY", cl, 0.035 * nebula))
        mw = nb.vmath("ADD", mw, neb)
        total = mw if total is None else nb.vmath("ADD", total, mw)
    nb.put(bg.inputs["Color"], total if total is not None else (0, 0, 0, 1))
    bg.inputs["Strength"].default_value = 1.0
    nb.put(out.inputs["Surface"], bg.outputs[0])


# ================================================================ 惑星の材質

def _crater_height(nb, P, scale, strength):
    vor = nb.voronoi(P, scale)
    rnd, _, _ = nb.separate(vor.outputs["Color"])
    radius = nb.math("MULTIPLY_ADD", rnd, 0.25, 0.12)
    x = nb.math("DIVIDE", vor.outputs["Distance"], radius)
    inside = nb.math("LESS_THAN", x, 1.0)
    bowl = nb.math("MULTIPLY", nb.math("MAXIMUM", nb.math("MULTIPLY_ADD", x, x, -1.0), -0.75), inside)
    rim = nb.math("MULTIPLY", nb.math("EXPONENT", nb.math("MULTIPLY", -1.0,
                                                          nb.math("POWER", nb.math("DIVIDE", nb.math("SUBTRACT", x, 1.0), 0.17), 2.0))), 0.3)
    h = nb.math("MULTIPLY", nb.math("ADD", bowl, rim), nb.math("MULTIPLY", radius, strength / scale))
    return h, rim


def planet_material(name, preset, pp, sun_world, texture=None):
    """プリセットの模様をシェーダーノードで作る(space2d.py の模様と同じ色・考え方)。"""
    kind = preset["kind"]
    c = preset["colors"]
    m, nb, out = new_material(name)
    tc = nb.node("ShaderNodeTexCoord")
    P = tc.outputs["Object"]                    # 惑星と一緒に回る座標(半径1の球面)
    px, py, pz = nb.separate(P)
    geo = nb.node("ShaderNodeNewGeometry")
    ndl = nb.vmath("DOT_PRODUCT", geo.outputs["Normal"], nb.const_vec(sun_world))
    base, rough, spec, emis, height = None, 0.9, 0.25, None, None
    if texture:
        img = bpy.data.images.load(texture, check_existing=True)
        tex = nb.node("ShaderNodeTexImage", extension="REPEAT")
        tex.image = img
        uv = nb.mapping(tc.outputs["UV"], loc=(0.25, 0, 0))
        nb.put(tex.inputs["Vector"], uv)
        base = tex.outputs["Color"]
        if kind == "earth":
            r, g, b = nb.separate(base)
            ocean = nb.smoothstep(nb.math("SUBTRACT", b, nb.math("MAXIMUM", r, g)), 0.02, 0.06)
            rough = nb.maprange(ocean, 0, 1, 0.9, 0.35)
    elif kind == "earth":
        h = nb.noise(P, 1.3, 10.0, 0.55, 0.25)
        sl = 0.555
        land = nb.smoothstep(h, sl - 0.004, sl + 0.004)
        ocean_col = nb.ramp(h, [(0.35, rgba(c["ocean_deep"])), (0.48, rgba(c["ocean"])),
                                (sl, rgba(c["ocean_shallow"]))])
        moist = nb.noise(P, 2.1, 6.0, 0.5)
        land_col = nb.ramp(moist, [(0.4, rgba(c["desert"])), (0.47, rgba(c["land_low"])),
                                   (0.6, rgba(c["land_forest"]))])
        elev = nb.maprange(h, sl, sl + 0.16)
        land_col = nb.mix(nb.math("MULTIPLY", elev, 0.8), land_col, rgba(c["land_high"]))
        lat = nb.math("ABSOLUTE", pz)
        tundra = nb.smoothstep(nb.math("MULTIPLY_ADD", moist, 0.15, lat), 0.88, 0.95)
        land_col = nb.mix(tundra, land_col, rgba(c["tundra"]))
        col = nb.mix(land, ocean_col, land_col)
        ice = nb.smoothstep(nb.math("MULTIPLY_ADD", moist, 0.08, lat), 0.955, 0.975)
        col = nb.mix(ice, col, rgba(c["ice"]))
        cv = nb.noise(P, 2.4, 12.0, 0.6, 0.6)
        cloud = nb.math("MULTIPLY", nb.smoothstep(cv, 0.5, 0.68), 0.95) if pp.get("clouds", True) else 0.0
        base = nb.mix(cloud, col, rgba(c["cloud"])) if pp.get("clouds", True) else col
        wet = nb.math("SUBTRACT", 1.0, nb.math("MAXIMUM", land, ice))
        rough = nb.maprange(nb.math("MULTIPLY", wet, nb.math("SUBTRACT", 1.0, cloud)), 0, 1, 0.92, 0.42)
        spec = nb.maprange(wet, 0, 1, 0.15, 0.35)
        height = nb.math("MULTIPLY", nb.math("MULTIPLY", elev, land), 0.6)
        if pp.get("night_lights", True):
            city = nb.smoothstep(nb.noise(P, 42.0, 2.0, 0.5), 0.6, 0.7)
            city = nb.math("MULTIPLY", city, nb.math("MULTIPLY", land, nb.math("SUBTRACT", 1.0, ice)))
            city = nb.math("MULTIPLY", city, nb.math("SUBTRACT", 1.0, nb.math("MULTIPLY", cloud, 0.8)))
            night = nb.maprange(ndl, 0.08, -0.12)
            emis = nb.vmath("SCALE", nb.const_vec(rgba(c["city"])[:3]),
                            scale=nb.math("MULTIPLY", nb.math("MULTIPLY", city, night), 2.5))
    elif kind == "rocky":
        moon = preset.get("craters", 0) >= 0.9
        big = nb.noise(P, 1.1, 5.0, 0.5, 0.2)
        detail = nb.noise(P, 6.0, 8.0, 0.55)
        if moon:
            mare = nb.smoothstep(nb.math("MULTIPLY_ADD", py, -0.13, big), 0.55, 0.62)
            col = nb.mix(nb.smoothstep(detail, 0.4, 0.7), rgba(c["base"]), rgba(c["light"]))
            col = nb.mix(nb.math("MULTIPLY", mare, 0.9), col, rgba(c["dark"]))
        else:
            col = nb.ramp(big, [(0.36, rgba(c["dark"])), (0.46, rgba(c["base"])),
                                (0.62, rgba(c["base"])), (0.72, rgba(c["light"]))])
        col = nb.vmath("SCALE", col, scale=nb.math("MULTIPLY_ADD", detail, 0.56, 0.72))
        h1, rim1 = _crater_height(nb, P, 7.0, 1.0)
        h2, rim2 = _crater_height(nb, P, 18.0, 0.7)
        h3, _ = _crater_height(nb, P, 45.0, 0.5)
        height = nb.math("ADD", nb.math("ADD", h1, h2), nb.math("ADD", h3, nb.math("MULTIPLY", detail, 0.01)))
        bright = nb.math("MULTIPLY", nb.math("ADD", rim1, rim2), 0.25 if moon else 0.12)
        col = nb.vmath("SCALE", col, scale=nb.math("ADD", 1.0, bright))
        if preset.get("polar_caps"):
            cap = nb.smoothstep(nb.math("MULTIPLY_ADD", detail, 0.04, nb.math("ABSOLUTE", pz)), 0.965, 0.985)
            col = nb.mix(cap, col, rgba(c["ice"]))
        base = col
    elif kind == "gas":
        bands = preset["bands"]
        turb = 3.2 if pp.get("_name") == "jupiter" else 1.4
        lat = nb.math("MULTIPLY", nb.math("ARCSINE", pz), 180.0 / math.pi)
        n1 = nb.noise(nb.mapping(P, scale=(1, 1, 4.5)), 2.2, 7.0, 0.55, 0.3)
        n2 = nb.noise(nb.mapping(P, scale=(1, 1, 2.5)), 9.0, 4.0, 0.5, 0.3)
        latp = nb.math("ADD", lat, nb.math("ADD", nb.math("MULTIPLY_ADD", n1, 2 * turb, -turb),
                                           nb.math("MULTIPLY_ADD", n2, 0.8 * turb, -0.4 * turb)))
        fac = nb.maprange(latp, 90.0, -90.0, 0.0, 1.0)
        stops, top = [], 90.0
        for lower, key in bands:
            mid = (top + lower) / 2.0
            stops.append(((90.0 - mid) / 180.0, rgba(c[key])))
            top = lower
        col = nb.ramp(fac, stops, interp="EASE")
        streak = nb.noise(nb.mapping(P, scale=(1, 1, 7)), 6.0, 5.0, 0.5)
        col = nb.vmath("SCALE", col, scale=nb.math("MULTIPLY_ADD", streak, 0.16, 0.92))
        if preset.get("storm"):
            lon = nb.math("ARCTAN2", px, nb.math("MULTIPLY", py, -1.0))
            lat_s, dlat, dlon = (-22.0, 4.6, 8.5) if pp.get("_name") == "jupiter" else (-20.0, 6.0, 13.0)
            dl = nb.math("DIVIDE", nb.math("SUBTRACT", lat, lat_s), dlat)
            dn = nb.math("DIVIDE", nb.math("MULTIPLY", lon, 180.0 / math.pi), dlon)
            d = nb.math("SQRT", nb.math("ADD", nb.math("MULTIPLY", dl, dl), nb.math("MULTIPLY", dn, dn)))
            core = nb.maprange(d, 1.0, 0.7)
            halo = nb.math("MULTIPLY", nb.maprange(d, 1.7, 1.05), nb.math("SUBTRACT", 1.0, core))
            col = nb.mix(nb.math("MULTIPLY", halo, 0.7), col, rgba(c["zone"]))
            col = nb.mix(nb.math("MULTIPLY", core, 0.9), col, rgba(c["storm"]))
        base = col
        rough, spec = 0.95, 0.1
    elif kind == "cloudy":
        v = nb.noise(nb.mapping(P, scale=(1, 1, 2.2)), 1.6, 7.0, 0.55, 0.5)
        base = nb.ramp(v, [(0.3, rgba(c["dark"])), (0.5, rgba(c["base"])), (0.7, rgba(c["light"]))])
        rough, spec = 0.95, 0.1
    elif kind == "ice":
        v = nb.noise(P, 2.0, 6.0, 0.5)
        col = nb.ramp(v, [(0.3, rgba(c["dark"])), (0.5, rgba(c["base"])), (0.7, rgba(c["light"]))])
        col = nb.mix(nb.math("MULTIPLY", nb.smoothstep(nb.noise(P, 1.5, 4.0, 0.5), 0.55, 0.7), 0.5),
                     col, rgba(c["blue"]))
        lines = None
        for k, (fr, wd) in enumerate(((1.6, 0.012), (2.7, 0.01), (4.5, 0.008), (7.5, 0.006))):
            n = nb.noise(nb.mapping(P, loc=(k * 3.1, k * 1.7, 0.0)), fr, 0.0, 0.5)
            ln = nb.maprange(nb.math("ABSOLUTE", nb.math("SUBTRACT", n, 0.5)), wd, wd * 0.3)
            ln = nb.math("MULTIPLY", ln, 1.0 - 0.15 * k)
            lines = ln if lines is None else nb.math("MAXIMUM", lines, ln)
        base = nb.mix(nb.math("MULTIPLY", lines, 0.85), col, rgba(c["line"]))
        height = nb.math("MULTIPLY", lines, 0.3)
        rough, spec = 0.6, 0.4
    elif kind == "lava":
        v = nb.noise(P, 2.5, 6.0, 0.5)
        col = nb.ramp(v, [(0.3, rgba(c["crust"])), (0.55, rgba(c["crust2"])), (0.75, rgba(c["ash"]))])
        warp = nb.vmath("ADD", P, nb.vmath("SCALE", nb.node_out_color(nb.noise_node(P, 2.0, 3.0, 0.5)), scale=0.35))
        e1 = nb.voronoi(warp, 3.5, "DISTANCE_TO_EDGE")
        e2 = nb.voronoi(nb.mapping(warp, loc=(0.3, 0.1, 0.7)), 9.0, "DISTANCE_TO_EDGE")
        crack = nb.math("MAXIMUM", nb.maprange(e1.outputs["Distance"], 0.04, 0.0),
                        nb.math("MULTIPLY", nb.maprange(e2.outputs["Distance"], 0.03, 0.0), 0.7))
        wob = nb.noise(P, 4.0, 3.0, 0.5)
        crack = nb.math("MULTIPLY", crack, nb.smoothstep(wob, 0.32, 0.6))
        base = nb.mix(crack, col, rgba(c["crust"], 0.5))
        hot = nb.math("POWER", crack, 3.0)
        ecol = nb.mix(hot, rgba(c["glow"]), rgba(c["hot"]))
        emis = nb.vmath("SCALE", ecol, scale=nb.math("MULTIPLY", crack, 1.6))
        height = nb.math("MULTIPLY", crack, -0.3)
        rough, spec = 0.8, 0.2
    normal = None
    if height is not None:
        bump = nb.node("ShaderNodeBump")
        nb.put(bump.inputs["Height"], height)
        bump.inputs["Strength"].default_value = 0.6
        bump.inputs["Distance"].default_value = 0.05
        normal = bump.outputs["Normal"]
    sh = principled(nb, base, rough, spec, emis, 1.0 if emis is not None else None, normal)
    nb.put(out.inputs["Surface"], sh)
    return m


def atmosphere_material(name, color, strength, sun_world, shell_r):
    m, nb, out = new_material(name)
    set_blended(m)
    b = impact_param(nb, shell_r)
    e = nb.math("SUBTRACT", b, 1.0)
    outside = nb.math("MULTIPLY", nb.math("EXPONENT", nb.math("DIVIDE", nb.math("MAXIMUM", e, 0.0), -0.022)),
                      nb.math("GREATER_THAN", e, 0.0))
    inside = nb.math("MULTIPLY", nb.math("POWER", nb.math("MINIMUM", b, 1.0), 7.0),
                     nb.math("LESS_THAN", e, 0.0))
    geo = nb.node("ShaderNodeNewGeometry")
    ndl = nb.vmath("DOT_PRODUCT", geo.outputs["Normal"], nb.const_vec(sun_world))
    lit = nb.maprange(ndl, -0.3, 0.6, 0.0, 1.0)
    disp = nb.math("MULTIPLY", nb.math("ADD", nb.math("MULTIPLY", outside, 0.8), nb.math("MULTIPLY", inside, 0.75)),
                   nb.math("MULTIPLY", lit, strength))
    nb.put(out.inputs["Surface"], glow_shader(nb, rgba(color), to_linear(nb, disp, 1.35)))
    return m


_RING_STOPS = [(1.24, 0.0), (1.25, 0.18), (1.52, 0.2), (1.53, 0.8), (1.94, 0.9), (1.95, 0.05),
               (2.02, 0.06), (2.03, 0.55), (2.2, 0.5), (2.205, 0.05), (2.22, 0.05), (2.225, 0.5),
               (2.27, 0.45), (2.275, 0.0), (2.31, 0.0), (2.32, 0.35), (2.33, 0.0)]


def ring_material(name, colors):
    m, nb, out = new_material(name)
    set_blended(m, backface_cull=False)
    tc = nb.node("ShaderNodeTexCoord")
    r = nb.vmath("LENGTH", tc.outputs["Object"])
    t = nb.maprange(r, 1.24, 2.33)
    fine = nb.noise(nb.combine(nb.math("MULTIPLY", r, 60.0), 0.0, 0.0), 1.0, 6.0, 0.6)
    op_stops = [((rr - 1.24) / (2.33 - 1.24), (o, o, o, 1.0)) for rr, o in _RING_STOPS]
    op = nb.separate(nb.ramp(t, op_stops))[0]
    op = nb.math("MULTIPLY", op, nb.math("MULTIPLY_ADD", fine, 0.7, 0.65), clamp=True)
    col = nb.ramp(t, [(0.0, rgba(colors.get("ring_dark", "#8e7d62"))), (0.55, rgba(colors.get("ring", "#d9c7a0"))),
                      (1.0, rgba(colors.get("ring", "#d9c7a0"), 0.9))])
    col = nb.vmath("SCALE", col, scale=nb.math("MULTIPLY_ADD", fine, 0.3, 0.85))
    diff = principled(nb, col, 1.0, 0.1)
    tr = nb.node("ShaderNodeBsdfTransparent")
    mx = nb.node("ShaderNodeMixShader")
    nb.put(mx.inputs[0], op)
    nb.put(mx.inputs[1], tr.outputs[0])
    nb.put(mx.inputs[2], diff)
    nb.put(out.inputs["Surface"], mx.outputs[0])
    return m


def _view_to_world(v):
    """カメラ座標(x右 / y上 / z手前) → Blenderの世界座標(カメラは -Y 側から +Y を見る)。"""
    return Vector((v[0], -v[2], v[1]))


def _planet_rotation(tilt_deg, incl_deg):
    """space2d と同じ M = Rz(-tilt)·Rx(incl) を、Blenderの世界座標での回転行列にする。"""
    t, i = math.radians(-tilt_deg), math.radians(incl_deg)
    rz = Matrix(((math.cos(t), -math.sin(t), 0), (math.sin(t), math.cos(t), 0), (0, 0, 1)))
    rx = Matrix(((1, 0, 0), (0, math.cos(i), -math.sin(i)), (0, math.sin(i), math.cos(i))))
    M = rz @ rx
    C = Matrix(((1, 0, 0), (0, 0, -1), (0, 1, 0)))          # 視点座標 → 世界(惑星座標 → ローカルも同形)
    return C @ M @ C.transposed()


def build_planet(tag, preset_name, preset, pp, center_w, radius, sun_world, fps, n_frames):
    root = empty(f"{tag}_root", center_w)
    root.scale = (radius, radius, radius)
    root.rotation_mode = "QUATERNION"
    root.rotation_quaternion = _planet_rotation(pp["tilt"], pp["inclination"]).to_quaternion()
    body = uv_sphere(f"{tag}_body", 160, 80)
    body.parent = root
    pp = dict(pp, _name=preset_name)
    mat = planet_material(f"{tag}_mat", preset, pp, sun_world, pp.get("texture"))
    body.data.materials.append(mat)
    if pp.get("atmosphere"):
        shell_r = 1.075
        atm = uv_sphere(f"{tag}_atm", 128, 64, shell_r)
        atm.parent = root
        atm.data.materials.append(atmosphere_material(f"{tag}_atm_mat", pp["atmosphere"],
                                                      pp.get("atm_strength", 0.8), sun_world, shell_r))
        if hasattr(atm, "visible_shadow"):
            atm.visible_shadow = False
    if pp.get("rings"):
        ring = annulus(f"{tag}_rings", 1.24, 2.33, 384, 1)
        ring.parent = root
        ring.data.materials.append(ring_material(f"{tag}_ring_mat", preset["colors"]))
    # 自転: space2d の中心経度 lon(t) = lon0 - ω t に合わせて体を回す(全フレームにキー)
    lon0 = math.radians(pp.get("longitude") or 0.0)
    w = math.radians(pp.get("rotation_speed", 6.0))
    body.rotation_mode = "XYZ"
    for f in range(n_frames):
        t = f / fps
        body.rotation_euler = (0.0, 0.0, w * t - lon0)
        body.keyframe_insert("rotation_euler", index=2, frame=f + 1)
    return root


# ================================================================ 太陽

def sun_material(color_hex, activity):
    m, nb, out = new_material("sun_mat")
    base = rgba(color_hex)
    center = mix_col(base, (1, 1, 1, 1), 0.3)
    limb = tuple(base[i] ** 2.0 * 0.85 for i in range(3)) + (1.0,)
    lw = nb.node("ShaderNodeLayerWeight")
    lw.inputs["Blend"].default_value = 0.5
    facing = lw.outputs["Facing"]                            # 中心0 → 縁1
    col = nb.ramp(facing, [(0.0, center), (0.55, base), (1.0, limb)])
    tc = nb.node("ShaderNodeTexCoord")
    P = tc.outputs["Object"]
    vor = nb.voronoi(P, 60.0, "F1")
    gran = nb.smoothstep(vor.outputs["Distance"], 0.9, 0.2)
    sup = nb.noise(P, 8.0, 3.0, 0.5)
    g = nb.math("MULTIPLY_ADD", gran, 0.18, nb.math("MULTIPLY_ADD", sup, 0.12, 0.8))
    spots = nb.smoothstep(nb.noise(P, 5.0, 3.0, 0.5), 0.78 - 0.12 * activity, 0.8 - 0.12 * activity)
    band = nb.maprange(nb.math("ABSOLUTE", nb.separate(P)[2]), 0.1, 0.55, 1.0, 0.0, smooth=True)
    dark = nb.math("SUBTRACT", 1.0, nb.math("MULTIPLY", nb.math("MULTIPLY", spots, band), 0.9 * min(1.0, activity * 2)))
    limb_i = nb.maprange(facing, 0.0, 1.0, 1.0, 0.62)
    st = to_linear(nb, nb.math("MULTIPLY", nb.math("MULTIPLY", g, dark), limb_i), 1.12)
    em = nb.node("ShaderNodeEmission")
    nb.put(em.inputs["Color"], col)
    nb.put(em.inputs["Strength"], st)
    nb.put(out.inputs["Surface"], em.outputs[0])
    return m


def corona_material(color_hex, activity, shell_r):
    m, nb, out = new_material("corona_mat")
    set_blended(m)
    b = impact_param(nb, shell_r)
    e = nb.math("MAXIMUM", nb.math("SUBTRACT", b, 1.0), 0.0)
    geo = nb.node("ShaderNodeNewGeometry")
    streamer = nb.noise(nb.vmath("NORMALIZE", geo.outputs["Normal"]), 4.0, 3.0, 0.6)
    streak = nb.math("MULTIPLY_ADD", nb.math("SUBTRACT", streamer, 0.5), 1.6 * activity, 1.0)
    # space2d.SunBody.corona と同じ式(表示空間の明るさ) → 2.2乗で線形の光の強さへ
    glow = nb.math("ADD", nb.math("MULTIPLY", nb.math("EXPONENT", nb.math("DIVIDE", e, -0.05)), 0.32),
                   nb.math("MULTIPLY", nb.math("ADD", nb.math("MULTIPLY", nb.math("EXPONENT", nb.math("DIVIDE", e, -0.3)), 0.2),
                                               nb.math("MULTIPLY", nb.math("EXPONENT", nb.math("DIVIDE", e, -1.2)), 0.07)), streak))
    fade = nb.maprange(b, shell_r, shell_r * 0.8)
    prom_n = nb.noise(geo.outputs["Position"], 9.0, 4.0, 0.6)
    prom = nb.math("MULTIPLY", nb.smoothstep(prom_n, 0.62, 0.72),
                   nb.math("MULTIPLY", nb.math("EXPONENT", nb.math("DIVIDE", e, -0.04)), 1.2 * activity))
    st = nb.math("MULTIPLY", nb.math("POWER", nb.math("MAXIMUM", nb.math("ADD", glow, prom), 0.0), 2.2), fade)
    col = mix_col(rgba(color_hex), (1, 1, 1, 1), 0.25)
    nb.put(out.inputs["Surface"], glow_shader(nb, col, st))
    return m


# ================================================================ ブラックホール

R_IN, R_OUT = 1.45, 4.3


def _disk_emission(nb, r, phi, tval, col_hex, spin, weight=None, cosv=None, geo_doppler=None):
    """半径r・角度phi(シャドウ半径単位)の円盤の発光(space2d.BlackHoleBody と同じ形)。"""
    om = nb.math("MULTIPLY", nb.math("POWER", nb.math("DIVIDE", R_IN, nb.math("MAXIMUM", r, R_IN)), 1.5), 0.55 * spin)
    ph = nb.math("SUBTRACT", phi, nb.math("MULTIPLY", om, tval))
    vec = nb.combine(nb.math("MULTIPLY", nb.math("COSINE", ph), 2.0),
                     nb.math("MULTIPLY", nb.math("SINE", ph), 2.0), nb.math("MULTIPLY", r, 6.0))
    n = nb.noise(vec, 1.0, 8.0, 0.62)
    v = nb.math("MULTIPLY_ADD", nb.math("SUBTRACT", n, 0.5), 1.7, 0.5, clamp=True)
    prof = nb.math("MULTIPLY", nb.math("POWER", nb.math("DIVIDE", R_IN, nb.math("MAXIMUM", r, 0.01)), 1.15),
                   nb.math("MULTIPLY", nb.smoothstep(r, R_IN * 0.96, R_IN * 1.12),
                           nb.maprange(r, R_OUT * 0.6, R_OUT, 1.0, 0.0, smooth=True)))
    if weight is not None:
        prof = nb.math("MULTIPLY", prof, weight)
    temp = nb.math("POWER", nb.math("MINIMUM", nb.math("POWER", nb.math("DIVIDE", R_IN, nb.math("MAXIMUM", r, 0.01)), 0.9), 1.0), 1.5)
    base = rgba(col_hex)
    hot = tuple(min(1.0, (base[i] + (1 - base[i]) * 0.3)) * 1.2 for i in range(3)) + (1.0,)
    cold = tuple(base[i] ** 1.8 * 0.8 for i in range(3)) + (1.0,)
    col = nb.mix(temp, cold, hot)
    if geo_doppler is not None:
        d = geo_doppler                        # 近づく側 +1 / 遠ざかる側 -1
    else:
        d = nb.math("MULTIPLY", cosv, -1.0)
    dop = nb.math("POWER", nb.math("MULTIPLY_ADD", d, 0.55, 1.0), 2.0)
    inten = nb.math("MULTIPLY", nb.math("MULTIPLY", prof, v), dop)
    return col, inten, nb.math("MULTIPLY", prof, v)


def disk_material(col_hex, spin, tval_node):
    m, nb, out = new_material("disk_mat")
    set_blended(m, backface_cull=False)
    tc = nb.node("ShaderNodeTexCoord")
    x, y, _ = nb.separate(tc.outputs["Object"])
    r = nb.math("SQRT", nb.math("ADD", nb.math("MULTIPLY", x, x), nb.math("MULTIPLY", y, y)))
    phi = nb.math("ARCTAN2", y, x)
    geo = nb.node("ShaderNodeNewGeometry")
    # 円盤のローカル座標での回転方向(反時計回り)を世界座標へ → カメラへ向かう側が+(明るい)
    tl = nb.combine(nb.math("MULTIPLY", y, -1.0), x, 0.0)
    vt = nb.node("ShaderNodeVectorTransform", vector_type="VECTOR", convert_from="OBJECT", convert_to="WORLD")
    nb.put(vt.inputs[0], tl)
    tang = nb.vmath("NORMALIZE", vt.outputs[0])
    dop = nb.vmath("DOT_PRODUCT", tang, geo.outputs["Incoming"])
    tv = nb.value(0.0)
    tval_node.append(tv)
    col, inten, dens = _disk_emission(nb, r, phi, tv.outputs[0], col_hex, spin, geo_doppler=dop)
    alpha = nb.math("MULTIPLY", dens, 2.2, clamp=True)
    alpha = nb.math("MINIMUM", alpha, 0.93)
    lin = to_linear(nb, inten, 1.7)
    em = nb.node("ShaderNodeEmission")
    nb.put(em.inputs["Color"], col)
    nb.put(em.inputs["Strength"], lin)
    tr = nb.node("ShaderNodeBsdfTransparent")
    mx = nb.node("ShaderNodeMixShader")
    nb.put(mx.inputs[0], alpha)
    nb.put(mx.inputs[1], tr.outputs[0])
    nb.put(mx.inputs[2], em.outputs[0])
    add = nb.node("ShaderNodeAddShader")
    em2 = nb.node("ShaderNodeEmission")                                 # 薄い所も光は足す
    nb.put(em2.inputs["Color"], col)
    nb.put(em2.inputs["Strength"], nb.math("MULTIPLY", lin, nb.math("SUBTRACT", 1.0, alpha)))
    nb.put(add.inputs[0], mx.outputs[0])
    nb.put(add.inputs[1], em2.outputs[0])
    nb.put(out.inputs["Surface"], add.outputs[0])
    return m


def halo_material(col_hex, spin, tval_node):
    """カメラを向く板に、裏側円盤のレンズ像(シャドウの上下の輪)と光子リングを描く。"""
    m, nb, out = new_material("halo_mat")
    set_blended(m)
    tc = nb.node("ShaderNodeTexCoord")
    x, y, _ = nb.separate(tc.outputs["Object"])
    rho = nb.math("SQRT", nb.math("ADD", nb.math("MULTIPLY", x, x), nb.math("MULTIPLY", y, y)))
    ps = nb.math("ARCTAN2", y, x)
    top = nb.math("GREATER_THAN", y, 0.0)
    rd_top = nb.math("ADD", R_IN, nb.math("DIVIDE", nb.math("SUBTRACT", rho, 1.07), 0.3))
    rd_bot = nb.math("ADD", R_IN, nb.math("DIVIDE", nb.math("SUBTRACT", rho, 1.04), 0.17))
    rd = nb.math("ADD", nb.math("MULTIPLY", top, rd_top), nb.math("MULTIPLY", nb.math("SUBTRACT", 1.0, top), rd_bot))
    s = nb.math("SINE", ps)
    wt = nb.math("ADD", nb.math("MULTIPLY", nb.smoothstep(s, 0.0, 0.5), nb.math("MULTIPLY", top, 1.25)),
                 nb.math("MULTIPLY", nb.smoothstep(nb.math("MULTIPLY", s, -1.0), 0.0, 0.5),
                         nb.math("MULTIPLY", nb.math("SUBTRACT", 1.0, top), 0.75)))
    wt = nb.math("MULTIPLY", wt, nb.smoothstep(rho, 0.99, 1.08))
    tv = nb.value(0.0)
    tval_node.append(tv)
    cosv = nb.math("COSINE", ps)
    col, inten, _ = _disk_emission(nb, rd, nb.math("ABSOLUTE", ps), tv.outputs[0], col_hex, spin, weight=wt, cosv=cosv)
    ring = nb.math("EXPONENT", nb.math("MULTIPLY", -1.0, nb.math("POWER", nb.math("DIVIDE", nb.math("SUBTRACT", rho, 1.025), 0.014), 2.0)))
    ring = nb.math("MULTIPLY", ring, nb.math("MULTIPLY_ADD", cosv, -0.5, 1.0))
    base = rgba(col_hex)
    hot = tuple(min(1.0, (base[i] + (1 - base[i]) * 0.3)) * 1.2 for i in range(3)) + (1.0,)
    em_col = nb.mix(nb.math("DIVIDE", ring, nb.math("ADD", ring, nb.math("ADD", inten, 0.001))), col, hot)
    st = to_linear(nb, nb.math("ADD", inten, nb.math("MULTIPLY", ring, 0.55)), 1.7)
    nb.put(out.inputs["Surface"], glow_shader(nb, em_col, st))
    return m


# ================================================================ 星の前進飛行

def build_flight(spec, p, n_frames, fps, vfov):
    import random
    rng = random.Random(spec["seed"] * 7919 + 5)
    speed = p["speed"]
    dur = n_frames / fps
    z_near, z_far = 0.35, 14.0
    length = speed * dur + z_far
    count = int(min(20000, 2600 * max(0.05, p["density"]) * length / z_far))
    aspect = spec["w"] / spec["h"]
    spread = math.tan(math.radians(vfov / 2)) * z_far * aspect * 1.15
    tail = speed * (0.06 + 0.035 * min(speed, 6.0))
    wdt = 0.0016
    me = bpy.data.meshes.new("flight_stars")
    verts, faces, cols = [], [], []
    cvar = p.get("color_variation", 0.5)
    for i in range(count):
        x = rng.uniform(-spread, spread)
        z = rng.uniform(-spread / aspect * 1.3, spread / aspect * 1.3)
        y = rng.uniform(z_near, length)
        b = 0.35 + 0.65 * rng.random() ** 1.5
        tcol = rgba(_STAR_RAMP[min(len(_STAR_RAMP) - 1, int(rng.random() * len(_STAR_RAMP)))][1])
        colv = mix_col((1, 1, 1, 1), tcol, min(1.0, cvar * 1.5))
        base = len(verts)
        for dy in (0.0, tail):
            for dx, dz in ((-wdt, -wdt), (wdt, -wdt), (wdt, wdt), (-wdt, wdt)):
                verts.append((x + dx, y + dy, z + dz))
        for f in ((0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)):
            faces.append(tuple(base + k for k in f))
        cols.extend([(colv[0] * b, colv[1] * b, colv[2] * b, 1.0)] * 8)
    me.from_pydata(verts, [], faces)
    attr = me.color_attributes.new("star_col", "FLOAT_COLOR", "POINT")
    for i, c in enumerate(cols):
        attr.data[i].color = c
    me.update()
    obj = link(bpy.data.objects.new("flight_stars", me))
    m, nb, out = new_material("flight_mat")
    at = nb.node("ShaderNodeAttribute", attribute_name="star_col")
    em = nb.node("ShaderNodeEmission")
    nb.put(em.inputs["Color"], at.outputs["Color"])
    em.inputs["Strength"].default_value = 2.5
    nb.put(out.inputs["Surface"], em.outputs[0])
    obj.data.materials.append(m)
    if hasattr(obj, "visible_shadow"):
        obj.visible_shadow = False
    return speed


# ================================================================ カメラ

def animate_camera(cam_obj, spec, pivot, base_dist, vfov, flight_speed=0.0):
    """camera_track(毎フレームの 拡大率・x移動・y移動・回り込み角)をキーフレームにする。"""
    cam = cam_obj.data
    w, h = spec["w"], spec["h"]
    k = spec["bg_share"]
    f0 = 12.0 / math.tan(math.radians(vfov / 2))
    fps = spec["fps"]
    for f, (s, ox, oy, orb) in enumerate(spec["camera_track"]):
        s = max(1e-3, s)
        s_d, s_f = s ** (1.0 - k), s ** k
        dist = base_dist / s_d
        cam.lens = f0 * s_f
        f_px = (h / 2.0) / math.tan(math.radians(vfov / 2)) * s_f
        dx = -(1.0 - k) * ox * w * dist / f_px                 # カメラが右へ → 中身は左へ
        dz = (1.0 - k) * oy * h * dist / f_px                  # カメラが上へ → 中身は下へ
        cam.shift_x = -k * ox * w / h                         # sensor_fit=VERTICAL ではシフトは高さ単位
        cam.shift_y = k * oy
        th = math.radians(orb)
        fwd_y = flight_speed * f / fps
        c_base = Vector((dx, -dist + fwd_y, dz))
        rel = c_base - pivot
        rot = Matrix.Rotation(th, 3, "Z")
        cam_obj.location = pivot + rot @ rel
        cam_obj.rotation_euler = (math.radians(90.0), 0.0, th)
        fr = f + 1
        cam_obj.keyframe_insert("location", frame=fr)
        cam_obj.keyframe_insert("rotation_euler", frame=fr)
        cam.keyframe_insert("lens", frame=fr)
        cam.keyframe_insert("shift_x", frame=fr)
        cam.keyframe_insert("shift_y", frame=fr)


def size_to_radius(size_frac, dist, vfov):
    """画面の高さに対する直径の比 → その距離に置く球の半径。"""
    a = math.atan(size_frac * math.tan(math.radians(vfov / 2)))   # 半径/画面の半分の高さ = size
    return dist * math.sin(a)


def screen_to_world(xf, yf, dist, vfov, aspect):
    t = math.tan(math.radians(vfov / 2))
    return Vector(((xf - 0.5) * 2 * t * aspect * dist, 0.0, -(yf - 0.5) * 2 * t * dist))


# ================================================================ レンダー設定

def eevee_engine_id():
    items = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    for cand in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):       # 4.2〜4.5 / 5.0〜
        if cand in items:
            return cand
    return "BLENDER_EEVEE_NEXT"


def setup_render(scene, spec):
    r = scene.render
    r.resolution_x, r.resolution_y = int(spec["w"]), int(spec["h"])
    r.resolution_percentage = 100
    fps = float(spec["fps"])
    r.fps = max(1, int(round(fps)))
    r.fps_base = r.fps / fps
    scene.frame_start, scene.frame_end = 1, int(spec["n_frames"])
    engine = spec.get("engine", "eevee")
    used = None
    if engine == "cycles":
        r.engine = "CYCLES"
        cy = scene.cycles
        cy.device = "CPU"
        cy.samples = int(spec.get("samples") or 32)
        cy.seed = int(spec["seed"])
        for attr, v in (("use_denoising", True), ("max_bounces", 4), ("diffuse_bounces", 2),
                        ("glossy_bounces", 2), ("transmission_bounces", 2), ("transparent_max_bounces", 16),
                        ("caustics_reflective", False), ("caustics_refractive", False)):
            if hasattr(cy, attr):
                try:
                    setattr(cy, attr, v)
                except Exception:
                    pass
        used = "CYCLES"
        if hasattr(r, "use_persistent_data"):
            r.use_persistent_data = True
    else:
        eid = eevee_engine_id()
        for cand in (eid, "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
            try:
                r.engine = cand
                used = cand
                break
            except TypeError:
                continue
        ev = scene.eevee
        ev.taa_render_samples = int(spec.get("samples") or 24)
        for attr, v in (("use_raytracing", False), ("use_gtao", False), ("use_bloom", False),
                        ("use_shadows", True), ("volumetric_samples", 16)):
            if hasattr(ev, attr):
                try:
                    setattr(ev, attr, v)
                except Exception:
                    pass
    for cand in ("Standard", "Filmic"):             # space2d と色を合わせるため Standard
        try:
            scene.view_settings.view_transform = cand
            break
        except TypeError:
            continue
    try:
        scene.view_settings.look = "None"
    except TypeError:
        pass
    im = r.image_settings
    if hasattr(im, "media_type"):
        try:
            im.media_type = "IMAGE"
        except TypeError:
            pass
    im.file_format = "PNG"
    im.color_mode = "RGB"
    im.color_depth = "8"
    r.film_transparent = False
    if hasattr(r, "use_motion_blur"):
        r.use_motion_blur = False
    return used


# ================================================================ シーン組み立て

def build_scene(spec):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    except Exception:
        pass
    scene = bpy.context.scene
    engine_used = setup_render(scene, spec)
    tpl, p = spec["template"], spec["params"]
    n, fps = int(spec["n_frames"]), float(spec["fps"])
    aspect = spec["w"] / spec["h"]
    vfov = float(spec["vfov_deg"])
    flight = tpl == "starfield" and p.get("speed", 0) > 0
    if flight:
        vfov = float(spec.get("flight_vfov_deg", 62.0))
    t = math.tan(math.radians(vfov / 2))
    spec["frame_solid_angle"] = 4 * t * t * aspect
    density = p.get("stars", p.get("density", 1.0))
    build_world(spec, density, p.get("nebula", 0.12), p.get("color_variation", 0.5))
    sun_world = _view_to_world(spec["sun_view"]).normalized()
    D = 10.0
    pivot = Vector((0, 0, 0))
    tvals = []
    if tpl in ("planet", "planet_compare"):
        lamp = bpy.data.lights.new("sun", type="SUN")
        lamp.energy = 3.4
        if hasattr(lamp, "angle"):
            lamp.angle = math.radians(0.6)
        lo = link(bpy.data.objects.new("sun", lamp))
        lo.rotation_mode = "QUATERNION"
        lo.rotation_quaternion = sun_world.to_track_quat("Z", "Y")
    if tpl == "planet":
        pr = spec["presets"][p["preset"]]
        c = screen_to_world(p["position"][0], p["position"][1], D, vfov, aspect)
        rad = size_to_radius(p["size"], D, vfov)
        build_planet("planet", p["preset"], pr, p, c, rad, sun_world, fps, n)
        pivot = c
    elif tpl == "planet_compare":
        for k, (cx, cy, dia) in enumerate(spec["layout"]):
            name = p["presets"][k]
            pr = spec["presets"][name]
            pp = {"tilt": pr["tilt"], "inclination": pr["inclination"], "atmosphere": pr["atmosphere"],
                  "atm_strength": pr["atm_strength"], "rings": pr["rings"], "clouds": pr["clouds"],
                  "night_lights": pr["night_lights"], "longitude": pr.get("longitude") or 0.0,
                  "rotation_speed": p["rotation_speed"], "texture": p["textures"][k]}
            c = screen_to_world(cx, cy, D, vfov, aspect)
            build_planet(f"planet{k}", name, pr, pp, c, size_to_radius(dia, D, vfov), sun_world, fps, n)
        pivot = screen_to_world(0.5, 0.5, D, vfov, aspect)
    elif tpl == "sun":
        c = screen_to_world(p["position"][0], p["position"][1], D, vfov, aspect)
        rad = size_to_radius(p["size"], D, vfov)
        root = empty("sun_root", c)
        root.scale = (rad, rad, rad)
        body = uv_sphere("sun_body", 128, 64)
        body.parent = root
        body.data.materials.append(sun_material(p["color"], p["activity"]))
        shell_r = 4.0
        cor = uv_sphere("corona", 96, 48, shell_r)
        cor.parent = root
        cor.data.materials.append(corona_material(p["color"], p["activity"], shell_r))
        w = math.radians(p.get("rotation_speed", 2.0))
        for f in range(n):
            body.rotation_euler = (0.0, 0.0, w * f / fps)
            body.keyframe_insert("rotation_euler", index=2, frame=f + 1)
        pivot = c
    elif tpl == "black_hole":
        c = screen_to_world(p["position"][0], p["position"][1], D, vfov, aspect)
        rad = size_to_radius(p["size"], D, vfov)
        root = empty("bh_root", c)
        root.scale = (rad, rad, rad)
        root.rotation_euler = (0.0, math.radians(-p.get("roll", 0.0)), 0.0)
        shadow = uv_sphere("bh_shadow", 96, 48)
        shadow.parent = root
        m, nb, out = new_material("shadow_mat")
        em = nb.node("ShaderNodeEmission")
        em.inputs["Color"].default_value = (0, 0, 0, 1)
        nb.put(out.inputs["Surface"], em.outputs[0])
        shadow.data.materials.append(m)
        tilt = empty("bh_disk_tilt", (0, 0, 0))
        tilt.parent = root
        tilt.rotation_euler = (math.radians(max(1.0, min(89.0, p["tilt"]))), 0.0, 0.0)
        disk = annulus("bh_disk", R_IN * 0.95, R_OUT, 384, 24)
        disk.parent = tilt
        disk.data.materials.append(disk_material(p["disk_color"], p.get("spin_speed", 1.0), tvals))
        halo = annulus("bh_halo", 0.96, 2.4, 384, 16)
        halo.parent = root
        halo.data.materials.append(halo_material(p["disk_color"], p.get("spin_speed", 1.0), tvals))
        pivot = c
    cam_data = bpy.data.cameras.new("cam")
    cam_data.sensor_fit = "VERTICAL"
    cam_data.sensor_height = 24.0
    cam_data.clip_start = 0.01
    cam_data.clip_end = 2000.0
    cam = link(bpy.data.objects.new("cam", cam_data))
    cam.rotation_mode = "XYZ"
    scene.camera = cam
    if tpl == "black_hole":
        halo = bpy.data.objects["bh_halo"]
        con = halo.constraints.new("TRACK_TO")
        con.target = cam
        con.track_axis = "TRACK_Z"
        con.up_axis = "UP_Y"
    speed = build_flight(spec, p, n, fps, vfov) if flight else 0.0
    animate_camera(cam, spec, pivot, D, vfov, speed)
    for f in range(n):                                     # 円盤の時間(差動回転)
        for tv in tvals:
            tv.outputs[0].default_value = f / fps
            tv.outputs[0].keyframe_insert("default_value", frame=f + 1)
    return scene, engine_used


def render_frames(scene, out_dir: Path, frames=None):
    n = scene.frame_end
    todo = list(range(1, n + 1)) if frames is None else frames
    t0 = time.time()
    done = 0
    for f in todo:
        path = out_dir / f"frame_{f:04d}.png"
        if path.exists() and path.stat().st_size > 0:
            continue
        scene.frame_set(f)
        scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        done += 1
        el = time.time() - t0
        print(f"VL_PROGRESS {f} {n} {el / done:.2f}", flush=True)


def main():
    a = parse_args(sys.argv)
    spec_path = Path(a.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out_dir = Path(a.out) if a.out else spec_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"VL_INFO blender {bpy.app.version_string} template={spec['template']}", flush=True)
    scene, engine_used = build_scene(spec)
    print(f"VL_ENGINE {engine_used}", flush=True)
    if a.save_blend:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(a.save_blend).resolve()))
    frames = None
    if a.frames:
        lo, _, hi = a.frames.partition("-")
        frames = list(range(int(lo), int(hi or lo) + 1))
    render_frames(scene, out_dir, frames)
    if frames is None:
        (out_dir / "done.json").write_text(json.dumps({
            "blender": bpy.app.version_string, "engine": engine_used, "frames": scene.frame_end,
            "script_version": SCRIPT_VERSION}, ensure_ascii=False), encoding="utf-8")
    print("VL_DONE", flush=True)


if __name__ == "__main__":
    main()
