#!/usr/bin/env python3
"""正規化スクリプトのラウンドトリップ検証。

See-through風のレイヤー名を持つ合成PSDをpytoshopで作り、
normalize_psd で正規化 → psd-toolsで読み戻して
グループ構成・リネーム・重ね順(合成画素)を検証する。GPU不要。

前提とする実挙動(このテスト自身で検証):
- pytoshop nested_layers: リスト先頭 = 最前面
- psd-tools 反復: 最背面から(index 0 = 一番下のレイヤー)
- 合成は composite(force=True) を使う(pytoshop出力は統合画像を持たないため)

usage: python tests/run_selftest.py
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import normalize_psd

CANVAS = 64

# (レイヤー名, 期待グループ, 期待正規名)。期待グループNoneは未分類(99_unmatched)行き。
# リスト先頭が最前面(pytoshopの仕様に合わせる)。
FIXTURE = [
    ("mystery_gadget", None, "mystery_gadget"),
    ("front hair", "00_前髪", "前髪"),
    ("pupil_R", "01_顔", "瞳_R"),
    ("eye_white_R", "01_顔", "白目_R"),
    ("mouth", "01_顔", "口"),
    ("face", "01_顔", "顔"),
    ("arm_L", "04_体", "腕_L"),
    ("sailor_uniform", "04_体", "服"),
    ("body", "04_体", "体"),
    ("back_hair", "05_後髪", "後髪"),
    ("background", "06_背景", "背景"),
]
MARKER = {name: 10 + i for i, (name, _, _) in enumerate(FIXTURE)}


def make_fixture_psd(path: Path) -> None:
    from pytoshop import enums
    from pytoshop.user import nested_layers

    layers = []
    for name, _, _ in FIXTURE:
        arr = np.full((CANVAS, CANVAS), MARKER[name], dtype=np.uint8)
        alpha = np.full((CANVAS, CANVAS), 255, dtype=np.uint8)
        layers.append(nested_layers.Image(
            name=name, visible=True, opacity=255, top=0, left=0,
            channels={0: arr, 1: arr, 2: arr, -1: alpha},
        ))
    psd = nested_layers.nested_layers_to_psd(
        layers, color_mode=enums.ColorMode.rgb, size=(CANVAS, CANVAS),
        compression=enums.Compression.zip,
    )
    with open(path, "wb") as f:
        psd.write(f)


def main() -> int:
    from psd_tools import PSDImage

    tmp = Path(tempfile.mkdtemp(prefix="l2d_selftest_"))
    src = tmp / "fixture.psd"
    out = tmp / "fixture_normalized.psd"
    make_fixture_psd(src)

    errors = []

    # 0) 前提の検証: 素材PSDの最前面 = FIXTURE先頭(mystery_gadget)
    src_front = int(np.asarray(PSDImage.open(src).composite(force=True))[0, 0, 0])
    if src_front != MARKER["mystery_gadget"]:
        errors.append(f"素材PSDの最前面が想定外: marker={src_front}")

    cfg = normalize_psd.load_config(normalize_psd.DEFAULT_CONFIG)
    width, height, layers = normalize_psd.extract_layers(src)
    if (width, height) != (CANVAS, CANVAS):
        errors.append(f"キャンバス寸法不一致: {width}x{height}")

    plan = normalize_psd.build_plan(layers, cfg)
    normalize_psd.write_psd(plan, cfg, width, height, out, top_first_order=True)

    psd = PSDImage.open(out)
    groups = {}   # psd-tools反復順(最背面から)で格納
    for group in psd:
        if not group.is_group():
            errors.append(f"トップレベルにグループ以外がある: {group.name}")
            continue
        groups[group.name] = [
            (l.name, int(np.asarray(l.topil().convert("RGB"))[0, 0, 0]))
            for l in group if not l.is_group()
        ]

    # 1) レイヤー名に終端NULが残っていないか(_strip_nul_namesの検証)
    for gname, entries in groups.items():
        for name, _ in [(gname, 0)] + entries:
            if name.endswith("\x00"):
                errors.append(f"終端NULが残っている: {name!r}")

    # 2) リネームとグループ配属(画素マーカーで取り違いも検出)
    for orig, exp_group, exp_name in FIXTURE:
        target = exp_group or "99_unmatched"
        hit = [(n, v) for n, v in groups.get(target, []) if v == MARKER[orig]]
        if not hit:
            errors.append(f"{orig}: グループ {target} に marker={MARKER[orig]} が無い")
        elif hit[0][0] != exp_name:
            errors.append(f"{orig}: 名前が {hit[0][0]!r} (期待: {exp_name})")

    # 3) 視覚的な最前面 = 未分類グループのmystery_gadget
    out_front = int(np.asarray(psd.composite(force=True))[0, 0, 0])
    if out_front != MARKER["mystery_gadget"]:
        errors.append(f"正規化後の最前面が想定外: marker={out_front}")

    # 4) グループ順(psd-tools反復=最背面から → 背景が先頭、未分類が末尾)
    expected_group_iter = ["06_背景", "05_後髪", "04_体", "01_顔", "00_前髪", "99_unmatched"]
    if list(groups.keys()) != expected_group_iter:
        errors.append(f"グループ順不一致: {list(groups.keys())}")

    # 5) グループ内の相対順維持(最背面から: 顔→口→白目→瞳)
    face_names = [n for n, _ in groups.get("01_顔", [])]
    if face_names != ["顔", "口", "白目_R", "瞳_R"]:
        errors.append(f"顔グループ内の順序不一致: {face_names}")

    if errors:
        print("[FAIL]")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] 全{len(FIXTURE)}レイヤーの正規化・リネーム・重ね順・NUL除去を確認 ({out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
