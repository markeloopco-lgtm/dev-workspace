#!/usr/bin/env python3
"""仮の立ち絵(オリジナルの単純な図形のキャラ)を作る。本物の立ち絵素材が届くまでの動作確認用。

videolab の立ち絵の決まり(<表情>.png / _open / _half / _blink)どおりのPNGを書き出すので、
台本の characters.<名前>.dir に指定すれば、口パク・まばたき・表情の切り替えをそのまま試せる。
本番では、各キャラクターの利用ガイドラインに沿って配布されている立ち絵素材に差し替える(docs/09)。

usage:
  python scripts/make_placeholder_character.py assets/chars/zundamon
  python scripts/make_placeholder_character.py assets/chars/narrator_test --color "#c98b5a"
"""

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

FACES = ("normal", "happy", "sad", "surprised", "angry")
W, H = 600, 900


def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def draw(face: str, mouth: str, blink: bool, color) -> Image.Image:
    """体は角丸の四角、頭は円、目・眉・口は線と丸だけで描く(どの既存キャラにも似せない)。"""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    dark = tuple(max(0, int(v * 0.55)) for v in color)
    light = tuple(min(255, int(v + (255 - v) * 0.55)) for v in color)
    d.rounded_rectangle([140, 470, 460, 890], radius=110, fill=color, outline=dark, width=8)   # 体
    d.ellipse([110, 70, 490, 450], fill=light, outline=dark, width=8)                          # 頭
    ey = 245
    for ex in (225, 375):                                                                     # 目
        if blink:
            d.line([ex - 32, ey, ex + 32, ey], fill=(30, 30, 30), width=9)
        elif face == "happy":
            d.arc([ex - 34, ey - 26, ex + 34, ey + 30], 200, 340, fill=(30, 30, 30), width=9)
        else:
            r = 34 if face == "surprised" else 28
            d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=(255, 255, 255), outline=(30, 30, 30), width=6)
            d.ellipse([ex - 12, ey - 12, ex + 12, ey + 12], fill=(30, 30, 30))
    # (眉の内側, 外側) の上下。マイナスで上がる: 悲しい=内側が上がる / 怒り=内側が下がる
    brows = {"normal": (0, 0), "happy": (-6, -6), "sad": (-14, 14), "surprised": (-18, -18), "angry": (16, -16)}
    lift_in, lift_out = brows.get(face, (0, 0))
    for ex, sgn in ((225, 1), (375, -1)):                                                     # 眉
        inner, outer = ex + 40 * sgn, ex - 40 * sgn
        d.line([outer, ey - 62 + lift_out, inner, ey - 62 + lift_in], fill=dark, width=10)
    mx, my = 300, 360
    if mouth == "open":                                                                       # 口
        d.ellipse([mx - 42, my - 26, mx + 42, my + 40], fill=(120, 30, 40), outline=(30, 30, 30), width=5)
    elif mouth == "half":
        d.ellipse([mx - 32, my - 12, mx + 32, my + 18], fill=(120, 30, 40), outline=(30, 30, 30), width=5)
    elif face in ("happy",):
        d.arc([mx - 44, my - 30, mx + 44, my + 22], 20, 160, fill=(30, 30, 30), width=7)
    elif face == "sad":
        d.arc([mx - 40, my - 2, mx + 40, my + 46], 200, 340, fill=(30, 30, 30), width=7)
    elif face == "surprised":
        d.ellipse([mx - 16, my - 10, mx + 16, my + 22], outline=(30, 30, 30), width=6)
    else:
        d.line([mx - 34, my + 4, mx + 34, my + 4], fill=(30, 30, 30), width=7)
    return im


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir")
    ap.add_argument("--color", default="#6f8fcf", help="体の色(既定 青みのグレー)")
    a = ap.parse_args()
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    color = _hex(a.color)
    for face in FACES:
        draw(face, "closed", False, color).save(out / f"{face}.png")
        draw(face, "open", False, color).save(out / f"{face}_open.png")
        draw(face, "half", False, color).save(out / f"{face}_half.png")
        draw(face, "closed", True, color).save(out / f"{face}_blink.png")
    (out / "README.txt").write_text("仮の立ち絵(動作確認用のオリジナル図形)。本番では配布されている立ち絵素材に差し替える。\n",
                                    encoding="utf-8")
    print(f"仮の立ち絵を作りました: {out}（表情: {', '.join(FACES)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
