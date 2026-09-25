"""テロップ(字幕)とタイトルの描画。PILで縁取り文字をRGBA画像にしてキャッシュする。"""

import os
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 太めの日本語ゴシック(上から順に探す)。assets/fonts/ に置いたフォントが最優先。
FONT_CANDIDATES = [
    "C:/Windows/Fonts/BIZ-UDGothicB.ttc", "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/meiryob.ttc", "C:/Windows/Fonts/msgothic.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
]

DEFAULT_TELOP = {
    "font": "auto",
    "size": 0.062,           # 画面高さに対する文字サイズ
    "color": "#ffffff",
    "outline": "#000000",
    "outline_width": 0.14,   # 文字サイズに対する縁の太さ
    "y": 0.86,               # 文字列の中心の縦位置(0=上端, 1=下端)
    "max_chars": 22,         # 1枚のテロップに入れる最大文字数
    "band": None,            # 例: "#000000" + 透明度 → "#00000080" で帯を敷く
    "speaker_colors": {},
}
DEFAULT_TITLE = {"size": 0.11, "color": "#ffffff", "outline": "#000000", "outline_width": 0.12,
                 "y": 0.45, "duration": 2.2}


def find_font(pref: str = "auto", assets_dir: Path = Path("assets/fonts")) -> str:
    if pref and pref != "auto":
        if Path(pref).exists():
            return pref
        raise FileNotFoundError(f"フォントが見つかりません: {pref}")
    if assets_dir.exists():
        for f in sorted(assets_dir.iterdir()):
            if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                return str(f)
    windir = os.environ.get("WINDIR")
    cands = list(FONT_CANDIDATES)
    if windir:
        cands = [c.replace("C:/Windows", windir.replace("\\", "/")) for c in cands]
    for c in cands:
        if Path(c).exists():
            return c
    raise FileNotFoundError("日本語フォントが見つかりません。assets/fonts/ に .ttf/.otf を置いてください")


def parse_color(c: str):
    c = c.lstrip("#")
    if len(c) == 6:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
    if len(c) == 8:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4, 6))
    raise ValueError(f"色の書式が不正です: #{c}")


BREAK_AFTER = "、。！？!?」』）)…"
_HIRA = re.compile(r"[\u3041-\u309f]")
_NONHIRA = re.compile(r"[\u30a0-\u30ff\u3400-\u9fffA-Za-z0-9\uff10-\uff19「『（(]")


def _break_cost(text: str, i: int) -> int:
    """text[:i] | text[i:] で切る時の不自然さ(小さいほど自然)。"""
    prev, nxt = text[i - 1], text[i]
    if prev in BREAK_AFTER:
        return 0
    if _HIRA.match(prev) and _NONHIRA.match(nxt):   # 「…ので|二つ」のような文節の切れ目
        return 1
    if prev in "ー" or nxt in "ー、。！？ぁぃぅぇぉっゃゅょゎァィゥェォッャュョ":
        return 9                                      # 長音・小書き・句読点の直前は切らない
    return 5


def best_break(text: str, lo: float = 0.3, hi: float = 0.7) -> int:
    n = len(text)
    cands = range(max(1, int(n * lo)), min(n - 1, int(n * hi)) + 1)
    return min(cands, key=lambda i: (_break_cost(text, i), abs(i - n / 2)), default=n // 2)


def split_telop(text: str, max_chars: int) -> list:
    """長い台詞を max_chars 以下のテロップ単位に分ける(句読点・文節の切れ目を優先)。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    i = best_break(text, 0.25, 0.75)
    return split_telop(text[:i], max_chars) + split_telop(text[i:], max_chars)


def wrap_two_lines(text: str, font, max_w: float, draw) -> str:
    """画面幅に収まらない場合だけ2行に折り返す(句読点・文節の切れ目を優先)。"""
    if draw.textlength(text, font=font) <= max_w or len(text) < 6:
        return text
    i = best_break(text)
    return text[:i] + "\n" + text[i:]


class TextRenderer:
    """縁取り文字をRGBA配列に描き、(配列, 左上x, 左上y)を返す。"""

    def __init__(self, w: int, h: int, style: dict, font_path: str):
        self.w, self.h = w, h
        self.style = style
        self.font_path = font_path
        self._cache = {}

    def render(self, text: str, color: str = None, alpha: float = 1.0):
        st = self.style
        key = (text, color)
        if key not in self._cache:
            size = max(10, int(st["size"] * self.h))
            font = ImageFont.truetype(self.font_path, size)
            stroke = max(1, int(round(st["outline_width"] * size)))
            probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
            text2 = wrap_two_lines(text, font, self.w * 0.92, probe)
            bbox = probe.multiline_textbbox((0, 0), text2, font=font, stroke_width=stroke,
                                            align="center", spacing=int(size * 0.2))
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            pad = stroke + 2
            band = st.get("band")
            bw = min(self.w, tw + 2 * pad + (int(size * 0.8) if band else 0))
            im = Image.new("RGBA", (bw, th + 2 * pad + (int(size * 0.3) if band else 0)), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            if band:
                d.rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=int(size * 0.25),
                                    fill=parse_color(band))
            ox = (im.width - tw) / 2 - bbox[0]
            oy = (im.height - th) / 2 - bbox[1]
            d.multiline_text((ox, oy), text2, font=font, fill=parse_color(color or st["color"]),
                             stroke_width=stroke, stroke_fill=parse_color(st["outline"]),
                             align="center", spacing=int(size * 0.2))
            arr = np.asarray(im).astype(np.float32)
            x = int((self.w - im.width) / 2)
            y = int(st["y"] * self.h - im.height / 2)
            y = max(0, min(self.h - im.height, y))
            self._cache[key] = (arr, x, y)
        arr, x, y = self._cache[key]
        return arr, x, y


def blend(frame: np.ndarray, rgba: np.ndarray, x: int, y: int, alpha: float = 1.0) -> None:
    """frame(RGB uint8)にRGBA(float32 0-255)をその場で合成する。"""
    h, w = rgba.shape[:2]
    fh, fw = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(fw, x + w), min(fh, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    sub = rgba[y0 - y:y1 - y, x0 - x:x1 - x]
    a = sub[..., 3:4] * (alpha / 255.0)
    region = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = (region * (1 - a) + sub[..., :3] * a).astype(np.uint8)


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(events: list, path: Path) -> None:
    lines = []
    for i, ev in enumerate(events, 1):
        lines += [str(i), f"{srt_time(ev['start'])} --> {srt_time(ev['end'])}",
                  re.sub(r"\s*\n\s*", " ", ev["text"]), ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")
