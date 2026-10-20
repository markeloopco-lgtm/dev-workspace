#!/usr/bin/env python3
"""立ち絵のPSD（レイヤーで表情・目・口を切り替える形式）から、videolab の立ち絵フォルダ用のPNGを書き出す。

  <表情>.png（口閉じ） / <表情>_open.png / <表情>_half.png / <表情>_blink.png（docs/09）

PSDToolKit 形式の決まりに対応: レイヤー名の先頭の「*」は同じフォルダの中で1つだけ表示する選択肢
（1つを出すと他は隠れる）、「!」は常に表示。どのレイヤーを出すかはレシピ（YAML）に書く。
まず inspect でレイヤーの一覧とレシピの下書きを作り、表情ごとに出すレイヤーを直してから export する。
配布されている立ち絵の利用規約で、加工（PNGへの書き出し）が認められているか先に確認すること。

usage:
  python scripts/psd_faces.py inspect 立ち絵.psd                            # レイヤーの一覧
  python scripts/psd_faces.py inspect 立ち絵.psd --recipe zundamon.yaml     # ＋レシピの下書き
  python scripts/psd_faces.py export 立ち絵.psd zundamon.yaml -o assets/chars/zundamon

レシピ（YAML）:
  show: [服/*制服]                  # 全部の表情で出すレイヤー（任意）
  hide: [汗]                        # 全部の表情で隠すレイヤー（任意）
  faces:
    normal:
      show: [眉/*普通眉, 目/*普通目]
      mouth: {closed: 口/*むふ, half: 口/*ほあ, open: 口/*ほあー}
      blink: 目/*目閉じ
    happy:
      show: [眉/*上がり眉, 目/*にっこり]
      mouth: {closed: 口/*にやり, open: 口/*わあー}

レイヤーは「フォルダ/レイヤー」の形で書く。先頭の * や ! は省略してよく、名前が1つしか無ければフォルダも省略できる。
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

MARKS = "*!"
VARIANTS = ("open", "half")


class RecipeError(ValueError):
    pass


def _bare(name: str) -> str:
    return name.lstrip(MARKS).strip()


class FacePsd:
    """PSDを開き、レイヤーの表示を切り替えて1枚の絵にする。"""

    def __init__(self, path):
        try:
            from psd_tools import PSDImage
        except ImportError:
            raise SystemExit("psd-tools が入っていません: .venv-video\\Scripts\\python.exe -m pip install psd-tools") \
                from None
        self.path = Path(path)
        self.psd = PSDImage.open(self.path)
        self.layers = list(self.psd.descendants())
        self.orig = {id(lyr): lyr.visible for lyr in self.layers}

    # ---- レイヤーの名前と場所
    def path_of(self, layer) -> str:
        parts = []
        cur = layer
        while cur is not None and cur is not self.psd:
            parts.append(cur.name)
            cur = cur.parent
        return "/".join(reversed(parts))

    def find(self, spec: str):
        """「フォルダ/レイヤー」を1つのレイヤーにする。見つからない・複数ある時は分かるように止める。"""
        parts = [p for p in str(spec).split("/") if p.strip()]
        if not parts:
            raise RecipeError("空のレイヤー名があります")
        level = list(self.psd)
        layer = None
        for i, part in enumerate(parts):
            hits = [c for c in level if c.name == part] or [c for c in level if _bare(c.name) == _bare(part)]
            if not hits and i == 0 and len(parts) == 1:
                # フォルダを省略した書き方: 全体から名前で探す
                hits = [c for c in self.layers if c.name == part] or \
                       [c for c in self.layers if _bare(c.name) == _bare(part)]
            if not hits:
                names = ", ".join(c.name for c in level) or "（なし）"
                raise RecipeError(f"レイヤー '{spec}' が見つかりません（'{part}' の所にあるもの: {names}）")
            if len(hits) > 1:
                where = ", ".join(self.path_of(h) for h in hits)
                raise RecipeError(f"レイヤー '{spec}' に当てはまるものが複数あります: {where}（フォルダから書いてください）")
            layer = hits[0]
            level = list(layer) if layer.is_group() else []
            if i < len(parts) - 1 and not layer.is_group():
                raise RecipeError(f"'{self.path_of(layer)}' はフォルダではありません（{spec}）")
        return layer

    # ---- 表示の切り替え
    def reset(self):
        for lyr in self.layers:
            lyr.visible = self.orig[id(lyr)] or lyr.name.startswith("!")

    def show(self, layer):
        cur = layer
        while cur is not None and cur is not self.psd:
            cur.visible = True
            if cur.name.startswith("*"):      # 選択肢: 同じフォルダの他の選択肢を隠す
                for sib in (cur.parent if cur.parent is not None else self.psd):
                    if sib is not cur and sib.name.startswith("*"):
                        sib.visible = False
            cur = cur.parent

    def render(self, show=(), hide=()) -> Image.Image:
        self.reset()
        for lyr in show:
            self.show(lyr)
        for lyr in hide:
            if not lyr.name.startswith("!"):
                lyr.visible = False
        im = self.psd.composite(force=True).convert("RGBA")
        arr = np.array(im)
        arr[arr[..., 3] == 0, :3] = 0       # 透明な所の色を消す(縮小した時に縁がにじまない)
        return Image.fromarray(arr, "RGBA")

    # ---- 一覧
    def tree_lines(self):
        out = []

        def walk(group, depth):
            for lyr in group:
                kind = "フォルダ" if lyr.is_group() else "画像"
                mark = "表示" if lyr.visible else "    "
                note = "  ← 選択肢(フォルダ内で1つ)" if lyr.name.startswith("*") else \
                       ("  ← 常に表示" if lyr.name.startswith("!") else "")
                out.append(f"[{mark}] {'  ' * depth}{lyr.name}{'/' if lyr.is_group() else ''}  ({kind}){note}")
                if lyr.is_group():
                    walk(lyr, depth + 1)
        walk(self.psd, 0)
        return out

    def choice_groups(self) -> dict:
        """選択肢(*)を持つフォルダ → [(選択肢のパス, 表示中か)]"""
        groups = {}
        for lyr in self.layers:
            if lyr.name.startswith("*"):
                parent = lyr.parent if lyr.parent is not None else self.psd
                key = self.path_of(parent) if parent is not self.psd else ""
                groups.setdefault(key, []).append((self.path_of(lyr), bool(self.orig[id(lyr)])))
        return groups


def _role(folder: str) -> str:
    name = _bare(folder.split("/")[-1]).lower() if folder else ""
    for role, words in (("mouth", ("口", "くち", "mouth")), ("eyes", ("目", "め", "eye")),
                        ("brows", ("眉", "まゆ", "brow"))):
        if any(w in name for w in words):
            return role
    return ""


def recipe_draft(fp: FacePsd) -> str:
    """レシピの下書き(コメントつきのYAML文字列)。いま表示されている選択肢を normal にする。"""
    groups = fp.choice_groups()
    lines = ["# psd_faces.py のレシピ（下書き）。表情ごとに「出すレイヤー」を フォルダ/レイヤー で書く（docs/09）",
             f"# 元のPSD: {fp.path.name}",
             "# 選択肢（名前の先頭が *）のあるフォルダ:"]
    for folder, items in groups.items():
        lines.append(f"#   {folder or '(いちばん上)'}: " + ", ".join(p.split("/")[-1] for p, _ in items))
    show, mouth, blink, mouth_items = [], None, None, []
    for folder, items in groups.items():
        cur = next((p for p, vis in items if vis), items[0][0])
        role = _role(folder)
        if role == "mouth":
            mouth, mouth_items = cur, [p for p, _ in items]
            continue
        show.append(cur)
        if role == "eyes":
            blink = next((p for p, _ in items if any(w in p.split("/")[-1] for w in ("閉", "とじ", "close"))), None)
    lines += ["show: []        # 全部の表情で出すレイヤー（服など）", "hide: []        # 全部の表情で隠すレイヤー（汗・効果など）",
              "faces:", "  normal:", f"    show: {yaml.safe_dump(show, allow_unicode=True, default_flow_style=True).strip()}"]
    if mouth:
        lines.append(f"    mouth: {{closed: {mouth}}}   # half: / open: を口のフォルダから選んで足す")
        lines.append("    # 口の選択肢: " + ", ".join(mouth_items))
    else:
        lines.append("    # mouth: {closed: 口/*閉じ, half: 口/*半開き, open: 口/*開き}   # 口のフォルダが見つからなかった")
    lines.append(f"    blink: {blink}" if blink else "    # blink: 目/*目閉じ")
    lines += ["  # happy: / sad: / surprised: / angry: も同じ形で足す（目・眉・口の選択肢を変える）", ""]
    return "\n".join(lines)


def _plan(fp: FacePsd, recipe: dict) -> list:
    """レシピ → [(ファイル名, 出すレイヤー, 隠すレイヤー)]。先に全部の名前を確かめる。"""
    if not isinstance(recipe, dict) or not isinstance(recipe.get("faces"), dict) or not recipe["faces"]:
        raise RecipeError("レシピに faces: がありません（表情ごとの設定）")
    errors = []

    def find_all(specs):
        found = []
        for s in [specs] if isinstance(specs, str) else (specs or []):
            try:
                found.append(fp.find(s))
            except RecipeError as e:
                errors.append(str(e))
        return found

    g_show, g_hide = find_all(recipe.get("show")), find_all(recipe.get("hide"))
    out = []
    for face, spec in recipe["faces"].items():
        spec = spec or {}
        f_show, f_hide = find_all(spec.get("show")), find_all(spec.get("hide"))
        mouth = spec.get("mouth") or {}
        if isinstance(mouth, str):
            mouth = {"closed": mouth}
        closed = find_all(mouth.get("closed"))
        out.append((f"{face}.png", g_show + f_show + closed, g_hide + f_hide))
        for v in VARIANTS:
            if mouth.get(v):
                out.append((f"{face}_{v}.png", g_show + f_show + find_all(mouth[v]), g_hide + f_hide))
        if spec.get("blink"):
            out.append((f"{face}_blink.png", g_show + f_show + closed + find_all(spec["blink"]), g_hide + f_hide))
    if errors:
        raise RecipeError("レシピのレイヤー名を直してください:\n  " + "\n  ".join(dict.fromkeys(errors)))
    return out


def export(psd_path, recipe_path, out_dir, max_height: int = 1600, log=print) -> list:
    fp = FacePsd(psd_path)
    recipe = yaml.safe_load(Path(recipe_path).read_text(encoding="utf-8-sig")) or {}
    plan = _plan(fp, recipe)
    images = []
    for i, (fname, show, hide) in enumerate(plan, 1):
        log(f"  [{i}/{len(plan)}] {fname}")
        images.append((fname, fp.render(show, hide)))
    # 全部の絵を同じ範囲で切り抜く(口パクの時に絵がずれないように)
    boxes = [im.getchannel("A").getbbox() for _, im in images]
    boxes = [b for b in boxes if b]
    if not boxes:
        raise RecipeError("書き出した絵が全部透明です。レシピで出すレイヤーを確認してください")
    W, H = images[0][1].size
    m = 4
    box = (max(0, min(b[0] for b in boxes) - m), max(0, min(b[1] for b in boxes) - m),
           min(W, max(b[2] for b in boxes) + m), min(H, max(b[3] for b in boxes) + m))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for fname, im in images:
        im = im.crop(box)
        if max_height and im.height > max_height:
            w = max(1, round(im.width * max_height / im.height))
            im = im.convert("RGBa").resize((w, max_height), Image.LANCZOS).convert("RGBA")
        im.save(out / fname)
        written.append(out / fname)
    (out / "README.txt").write_text(
        f"{fp.path.name} から scripts/psd_faces.py で書き出した立ち絵（レシピ: {Path(recipe_path).name}）。\n"
        "配布元の利用規約とクレジット表記に従って使う（look / 台本の credits に書く）。\n", encoding="utf-8")
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("inspect", help="レイヤーの一覧（とレシピの下書き）")
    s.add_argument("psd")
    s.add_argument("--recipe", help="レシピの下書きをこのファイルに書く")
    s = sub.add_parser("export", help="レシピどおりに表情ごとのPNGを書き出す")
    s.add_argument("psd")
    s.add_argument("recipe")
    s.add_argument("-o", "--out", required=True, help="書き出し先（例 assets/chars/zundamon）")
    s.add_argument("--max-height", type=int, default=1600, help="これより高い絵は縮小する（既定1600px。0で縮小しない）")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "inspect":
            fp = FacePsd(a.psd)
            print(f"{fp.path.name}: {fp.psd.width}x{fp.psd.height}px / レイヤー {len(fp.layers)}個")
            print("\n".join(fp.tree_lines()))
            if a.recipe:
                rp = Path(a.recipe)
                if rp.exists():
                    print(f"\n{rp} は既にあるので上書きしません", file=sys.stderr)
                    return 1
                rp.write_text(recipe_draft(fp), encoding="utf-8")
                print(f"\nレシピの下書きを書きました: {rp}（表情ごとに出すレイヤーを直して export する）")
            return 0
        print(f"書き出し: {a.psd} → {a.out}")
        files = export(a.psd, a.recipe, a.out, a.max_height)
        print(f"{len(files)}枚を書き出しました: {a.out}")
        return 0
    except (RecipeError, OSError, yaml.YAMLError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
