#!/usr/bin/env python3
"""See-throughが出力したPSDを、Live2D Cubism取り込み用の標準レイヤー構造に正規化する。

レイヤー名の表記ゆれ(英語タグ/日本語/L-R表記)を configs/layer_mapping.yaml の
正規名にリネームし、決まったフォルダ構成・重ね順に並べ替えたPSDを書き出す。
全モデルで同じレイヤー構造になるため、Cubism Editorのテンプレート機能で
1体目のリグを2体目以降へ流用できるようになる。

usage:
  python scripts/normalize_psd.py inspect input.psd
  python scripts/normalize_psd.py normalize input.psd -o output.psd
  python scripts/normalize_psd.py normalize input.psd --emit-pngs out_dir/
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml
from psd_tools import PSDImage

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "configs" / "layer_mapping.yaml"

SIDE_LEFT = re.compile(r"(?:^|[_\-\s(])(l|left|左)(?:$|[_\-\s)])", re.IGNORECASE)
SIDE_RIGHT = re.compile(r"(?:^|[_\-\s(])(r|right|右)(?:$|[_\-\s)])", re.IGNORECASE)


@dataclass
class MappingRule:
    name: str
    group: str
    patterns: list
    sided: bool = False
    compiled: list = field(default_factory=list)

    def __post_init__(self):
        self.compiled = [re.compile(p, re.IGNORECASE) for p in self.patterns]

    def matches(self, layer_name: str) -> bool:
        return any(p.search(layer_name) for p in self.compiled)


@dataclass
class SourceLayer:
    name: str
    path: str          # 元PSD内のフォルダパス("grp/sub"形式)
    index: int         # 元PSDでの通し番号(bottom=0)
    pixels: np.ndarray  # RGBA uint8 (h, w, 4)
    top: int
    left: int


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    rules = [MappingRule(**r) for r in cfg["rules"]]
    return {"rules": rules, "group_order": cfg["group_order"], "canvas": cfg.get("canvas", {})}


def extract_layers(psd_path: Path) -> tuple:
    psd = PSDImage.open(psd_path)
    layers = []
    counter = [0]

    def walk(parent, path=""):
        for layer in parent:
            # 一部ツールはunicodeレイヤー名に終端NULを残すため除去する
            name = layer.name.rstrip("\x00")
            if layer.is_group():
                walk(layer, f"{path}{name}/")
            else:
                pil = layer.topil()
                if pil is None:  # 空レイヤーはCubismでも不要なので落とす
                    continue
                arr = np.asarray(pil.convert("RGBA"), dtype=np.uint8)
                layers.append(SourceLayer(
                    name=name, path=path, index=counter[0],
                    pixels=arr, top=layer.top, left=layer.left,
                ))
                counter[0] += 1

    walk(psd)
    return psd.width, psd.height, layers


def detect_side(name: str) -> str:
    if SIDE_LEFT.search(name):
        return "L"
    if SIDE_RIGHT.search(name):
        return "R"
    return ""


def map_layer(layer: SourceLayer, rules: list) -> tuple:
    """(グループ名, 正規レイヤー名) を返す。どのルールにも合わなければ (None, 元の名前)。"""
    haystack = f"{layer.path}{layer.name}"
    # "arm_L"のような区切りは\bが効かないためスペース化した表記でもマッチを試みる
    normalized = re.sub(r"[_\-]+", " ", haystack)
    for rule in rules:
        if rule.matches(haystack) or rule.matches(normalized):
            new_name = rule.name
            if rule.sided:
                side = detect_side(haystack)
                if side:
                    new_name = f"{rule.name}_{side}"
            return rule.group, new_name
    return None, layer.name


def build_plan(layers: list, cfg: dict) -> dict:
    """正規化後の構成を組み立てる。返り値: {"groups": {grp: [SourceLayer...]}, "renames", "unmatched"}"""
    groups = {g: [] for g in cfg["group_order"]}
    unmatched, renames = [], []
    used_names = {}

    for layer in sorted(layers, key=lambda l: l.index):
        grp, new_name = map_layer(layer, cfg["rules"])
        if grp is None:
            unmatched.append(layer)
            continue
        # 同名衝突は連番で逃がす(テンプレ適用は名前一致が前提のため黙って上書きしない)
        key = (grp, new_name)
        if key in used_names:
            used_names[key] += 1
            new_name = f"{new_name}_{used_names[key]}"
        else:
            used_names[key] = 1
        renames.append((layer.name, grp, new_name))
        layer.name = new_name
        groups.setdefault(grp, []).append(layer)

    return {"groups": groups, "renames": renames, "unmatched": unmatched}


def _strip_nul_names(psd_path: Path) -> None:
    """pytoshopがunicodeレイヤー名に付ける終端NULを除去する(Cubismでの誤表示防止)。"""
    from psd_tools import PSDImage
    from psd_tools.constants import Tag

    psd = PSDImage.open(psd_path)

    def fix(layers):
        for layer in layers:
            blocks = layer._record.tagged_blocks
            blk = blocks.get(Tag.UNICODE_LAYER_NAME) if blocks else None
            if blk is not None and blk.data.value.endswith("\x00"):
                blk.data.value = blk.data.value.rstrip("\x00")
            if layer.is_group():
                fix(layer)

    fix(psd)
    psd.save(psd_path)


def write_psd(plan: dict, cfg: dict, width: int, height: int, out_path: Path,
              top_first_order: bool = True) -> None:
    """pytoshopで正規化済みPSDを書き出す。

    top_first_order: nested_layers のリスト先頭が「最前面」になる場合 True。
    (pytoshopの実挙動は先頭=最前面。tests/run_selftest.py で検証済み)
    """
    from pytoshop import enums
    from pytoshop.user import nested_layers

    def to_image(layer: SourceLayer) -> "nested_layers.Image":
        arr = layer.pixels
        return nested_layers.Image(
            name=layer.name, visible=True, opacity=255,
            top=layer.top, left=layer.left,
            channels={0: arr[:, :, 0], 1: arr[:, :, 1], 2: arr[:, :, 2], -1: arr[:, :, 3]},
        )

    group_order = cfg["group_order"]
    if not top_first_order:
        group_order = list(reversed(group_order))

    tree = []
    unmatched = plan["unmatched"]
    if unmatched:
        # 未分類は気付けるように最前面のフォルダにまとめる
        tree.append(nested_layers.Group(
            name="99_unmatched", visible=True, opacity=255,
            layers=[to_image(l) for l in unmatched],
        ))
    for grp in group_order:
        members = plan["groups"].get(grp, [])
        if not members:
            continue
        # グループ内は元PSDの重ね順を維持する(see-throughの深度順を信頼する)
        members = sorted(members, key=lambda l: l.index, reverse=top_first_order)
        tree.append(nested_layers.Group(
            name=grp, visible=True, opacity=255,
            layers=[to_image(l) for l in members],
        ))

    # RLEはpytoshopのC拡張が必要なため、純Pythonで完結するzip圧縮を使う
    psd = nested_layers.nested_layers_to_psd(
        tree, color_mode=enums.ColorMode.rgb, size=(height, width),
        compression=enums.Compression.zip,
    )
    with open(out_path, "wb") as f:
        psd.write(f)
    try:
        _strip_nul_names(out_path)
    except Exception as e:  # 名前修正は品質向上目的なので失敗しても書き出しは有効
        print(f"[warn] レイヤー名の終端NUL除去に失敗: {e}", file=sys.stderr)


def emit_pngs(plan: dict, cfg: dict, out_dir: Path) -> None:
    """PSD書き出しが使えない環境向けの代替出力: 連番PNG + マニフェスト。"""
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    seq = 0
    for grp in ["99_unmatched"] + list(cfg["group_order"]):
        members = plan["unmatched"] if grp == "99_unmatched" else plan["groups"].get(grp, [])
        for layer in members:
            fname = f"{seq:03d}_{grp}_{layer.name}.png"
            Image.fromarray(layer.pixels).save(out_dir / fname)
            manifest.append({"file": fname, "group": grp, "name": layer.name,
                             "top": layer.top, "left": layer.left})
            seq += 1
    with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def cmd_inspect(args) -> int:
    cfg = load_config(Path(args.config))
    width, height, layers = extract_layers(Path(args.psd))
    print(f"canvas: {width}x{height}  layers: {len(layers)}")
    print(f"{'#':>3}  {'元レイヤー名':<28} {'→ グループ':<14} → 正規名")
    for layer in layers:
        grp, new_name = map_layer(layer, cfg["rules"])
        dest = f"{grp:<14} → {new_name}" if grp else "(未分類)"
        print(f"{layer.index:>3}  {layer.path + layer.name:<28} {dest}")
    return 0


def cmd_normalize(args) -> int:
    cfg = load_config(Path(args.config))
    src = Path(args.psd)
    width, height, layers = extract_layers(src)
    plan = build_plan(layers, cfg)

    for old, grp, new in plan["renames"]:
        print(f"  {old}  →  {grp}/{new}")
    for layer in plan["unmatched"]:
        print(f"  [warn] 未分類: {layer.path}{layer.name}", file=sys.stderr)
    if plan["unmatched"] and args.strict:
        print("[error] --strict 指定のため未分類レイヤーがある状態では書き出しません。"
              "configs/layer_mapping.yaml にパターンを追加してください。", file=sys.stderr)
        return 1

    if args.emit_pngs:
        emit_pngs(plan, cfg, Path(args.emit_pngs))
        print(f"PNG書き出し完了: {args.emit_pngs}")
    else:
        out = Path(args.output) if args.output else src.with_name(src.stem + "_normalized.psd")
        write_psd(plan, cfg, width, height, out, top_first_order=args.top_first)
        print(f"正規化PSD書き出し完了: {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="レイヤー一覧とマッピング結果のプレビュー")
    p_inspect.add_argument("psd")
    p_inspect.add_argument("--config", default=str(DEFAULT_CONFIG))
    p_inspect.set_defaults(func=cmd_inspect)

    p_norm = sub.add_parser("normalize", help="正規化PSDの書き出し")
    p_norm.add_argument("psd")
    p_norm.add_argument("-o", "--output")
    p_norm.add_argument("--config", default=str(DEFAULT_CONFIG))
    p_norm.add_argument("--strict", action="store_true",
                        help="未分類レイヤーがあればエラー終了する")
    p_norm.add_argument("--emit-pngs", metavar="DIR",
                        help="PSDの代わりに連番PNG+manifest.jsonを書き出す")
    p_norm.add_argument("--top-first", action="store_true", default=True,
                        help=argparse.SUPPRESS)  # selftestで実挙動を固定済み
    p_norm.set_defaults(func=cmd_normalize)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
