#!/usr/bin/env python3
"""台本YAML + 音声WAV から YouTubeショート用の縦型MP4を書き出す。

2つのスタイルに対応する:
- talk   : 立ち絵 + 字幕。キャラが喋る形式 (口パクは音声のRMSから自動生成)
- matome : 「◯◯さん、〜すぎると話題に」型。見出しバナー + 写真のスローズーム
           + ネットの反応カードが積み上がっていく形式

どちらも同じ台本形式で、lines の style で行ごとに切り替わる。
GPU不要・ffmpegのみ必要。同じ絵になるフレームは1枚しか描かない。

usage:
  python scripts/make_short.py init myshort/                  # talk用テンプレ
  python scripts/make_short.py init myshort/ --style matome   # まとめ型テンプレ
  python scripts/make_short.py build myshort/script.yaml -o out.mp4
  python scripts/make_short.py build script.yaml --probe      # 構成だけ確認
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- 既定値

DEFAULTS = {
    "fps": 30,
    "size": [1080, 1920],
    "background": {"color": "#101024", "image": None},
    "character": {
        "closed": None,
        "open": None,
        "height_ratio": 0.55,
        "align": "center",
        "bottom_margin_ratio": 0.16,
    },
    "subtitle": {
        "font": None,
        "size_ratio": 0.055,
        "color": "#ffffff",
        "stroke": "#000000",
        "stroke_width_ratio": 0.007,
        "bottom_ratio": 0.20,
        "max_chars_per_line": 16,
        "max_width_ratio": 0.88,
        "line_spacing_ratio": 0.30,
    },
    # 画面上部に出しっぱなしにする見出し (まとめ型のスレタイ風)
    "banner": {
        "text": None,
        "size_ratio": 0.042,
        "color": "#ffe600",
        "stroke": "#000000",
        "stroke_width_ratio": 0.008,
        "top_ratio": 0.045,
        "max_chars_per_line": 13,
        "max_width_ratio": 0.92,
        "line_spacing_ratio": 0.25,
    },
    # lines の image: で出す写真の置き場とスローズーム
    "image": {
        "top_ratio": 0.14,
        "height_ratio": 0.38,
        "width_ratio": 0.92,
        "zoom_from": 1.0,
        "zoom_to": 1.08,
        "zoom_step_frames": 2,   # 何フレームごとにズーム段階を進めるか
    },
    # style: comment の行が積み上がるカード
    "comment": {
        "name": "名無しさん",
        "size_ratio": 0.030,
        "name_size_ratio": 0.022,
        "width_ratio": 0.92,
        "max_visible": 3,
        "top_ratio": 0.55,
        "bg": "#141622",
        "bg_alpha": 225,
        "text_color": "#ffffff",
        "name_color": "#8fd3ff",
        "max_chars_per_line": 22,
        "pad_ratio": 0.016,
        "gap_ratio": 0.012,
    },
    "mouth": {"threshold": 0.12, "min_hold_frames": 2},
    "gap_sec": 0.25,
    "bgm": {"file": None, "gain_db": -18.0},
}

# 日本語が出るフォントの候補。上から順に探す。
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\meiryob.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\YuGothB.ttc",
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    "/etc/alternatives/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
]

# ショート動画の上限。超えると通常動画扱いになるので警告する。
SHORTS_MAX_SEC = 180

# 音声が1本も無い台本で使うサンプリングレート
SILENT_RATE = 24000


class ScriptError(Exception):
    """台本の不備。ユーザーに直してもらうもの。"""


# ---------------------------------------------------------------- 小道具


def deep_merge(base: dict, over: dict) -> dict:
    """既定値に台本の指定を上書きする(ネスト対応)。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def find_ffmpeg() -> str:
    """ffmpegの場所を返す。PATH → imageio-ffmpeg同梱 の順に探す。"""
    exe = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise ScriptError(
        "ffmpegが見つかりません。次のどちらかで用意してください:\n"
        "  1) https://ffmpeg.org/ からダウンロードしてPATHを通す\n"
        "  2) pip install imageio-ffmpeg   (同梱バイナリを自動で使います)"
    )


def resolve_font(spec, size: int) -> ImageFont.FreeTypeFont:
    """字幕フォントを解決する。指定が無ければ日本語フォントを自動で探す。"""
    tried = []
    for cand in ([spec] if spec else []) + FONT_CANDIDATES:
        if not cand:
            continue
        tried.append(str(cand))
        if Path(cand).exists():
            return ImageFont.truetype(str(cand), size)
    raise ScriptError(
        "日本語フォントが見つかりませんでした。台本の subtitle.font に\n"
        "フォントファイルのパスを書いてください(例: C:\\Windows\\Fonts\\meiryob.ttc)。\n"
        "探した場所: " + ", ".join(tried)
    )


def resolve_path(base_dir: Path, value) -> Path:
    """台本からの相対パスを台本ファイルの場所基準で解決する。"""
    p = Path(value)
    return p if p.is_absolute() else (base_dir / p)


def hex_rgba(color: str, alpha: int) -> tuple:
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha)


# ---------------------------------------------------------------- 音声


def read_wav_mono(path: Path):
    """WAVを読んで (モノラルfloat配列, サンプリングレート) を返す。"""
    with wave.open(str(path), "rb") as w:
        n_ch, width, rate, n_frames = (
            w.getnchannels(),
            w.getsampwidth(),
            w.getframerate(),
            w.getnframes(),
        )
        raw = w.readframes(n_frames)
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ScriptError(f"{path.name}: 対応していないWAV形式です({width * 8}bit)")
    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1)
    return data, rate


def mouth_states(samples: np.ndarray, rate: int, n_frames: int, cfg: dict) -> list:
    """音の大きさから 1フレームごとの口の開閉(True=開き)を作る。"""
    if n_frames <= 0:
        return []
    per_frame = max(1, len(samples) // n_frames) if len(samples) else 1
    rms = []
    for i in range(n_frames):
        chunk = samples[i * per_frame : (i + 1) * per_frame]
        rms.append(float(np.sqrt(np.mean(chunk**2))) if len(chunk) else 0.0)
    peak = max(rms) if rms else 0.0
    if peak <= 0:
        return [False] * n_frames
    norm = [v / peak for v in rms]
    states = [v >= cfg["threshold"] for v in norm]

    # パカパカしすぎないよう、短すぎる切り替わりは直前の状態に吸収する
    hold = max(1, int(cfg["min_hold_frames"]))
    out, i = [], 0
    while i < len(states):
        j = i
        while j < len(states) and states[j] == states[i]:
            j += 1
        run = j - i
        value = states[i] if run >= hold or not out else out[-1]
        out.extend([value] * run)
        i = j
    return out


def build_voice_track(lines: list, gap_sec: float, out_path: Path) -> float:
    """台詞WAVを間隔を空けて連結し、1本のWAVにする。総再生秒を返す。"""
    rates = {ln["_rate"] for ln in lines}
    if len(rates) > 1:
        raise ScriptError(
            "台詞WAVのサンプリングレートが混在しています: "
            + ", ".join(f"{r}Hz" for r in sorted(rates))
            + "\nStyle-Bert-VITS2の出力設定を揃えてから書き出してください。"
        )
    rate = rates.pop()
    gap = np.zeros(int(rate * gap_sec), dtype=np.float32)
    chunks = []
    for i, ln in enumerate(lines):
        if i:
            chunks.append(gap)
        chunks.append(ln["_samples"])
    track = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    peak = float(np.max(np.abs(track))) if len(track) else 0.0
    if peak > 1.0:  # 連結でクリップした場合だけ正規化する
        track = track / peak
    pcm = np.clip(track * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return len(track) / rate


# ---------------------------------------------------------------- 描画


def wrap_text(text: str, limit: int, font=None, max_px=None) -> list:
    """日本語向けの折り返し。明示的な改行を優先する。

    文字数(limit)だけでなく、fontとmax_pxを渡すと**実際の描画幅**でも折る。
    日本語と英数字では1文字の幅が倍違うので、文字数だけだと横にはみ出す。
    """
    def too_wide(s: str) -> bool:
        return font is not None and max_px is not None and font.getlength(s) > max_px

    kinsoku = "、。，．・：；！？」』）］｝〉》ゝゞー!?),.]"  # 行頭に来させない文字
    out = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        while para:
            take = min(len(para), limit)
            while take > 1 and too_wide(para[:take]):
                take -= 1
            # 次の行頭が句読点等になるなら前の行にぶら下げる(最大2文字)
            hang = 0
            while (take + hang < len(para) and hang < 2
                   and para[take + hang] in kinsoku):
                hang += 1
            take += hang
            out.append(para[:take])
            para = para[take:]
    return out or [""]


def load_character(cfg: dict, base_dir: Path, canvas_h: int):
    """立ち絵(口閉じ/口開き)を読み込んで指定の高さに揃える。"""
    closed_spec = cfg.get("closed")
    if not closed_spec:
        return None, None
    closed_path = resolve_path(base_dir, closed_spec)
    if not closed_path.exists():
        raise ScriptError(f"立ち絵が見つかりません: {closed_path}")
    closed = Image.open(closed_path).convert("RGBA")

    target_h = max(1, int(canvas_h * cfg["height_ratio"]))
    scale = target_h / closed.height
    size = (max(1, int(closed.width * scale)), target_h)
    closed = closed.resize(size, Image.LANCZOS)

    open_spec = cfg.get("open")
    if open_spec:
        open_path = resolve_path(base_dir, open_spec)
        if not open_path.exists():
            raise ScriptError(f"口開きの立ち絵が見つかりません: {open_path}")
        opened = Image.open(open_path).convert("RGBA").resize(size, Image.LANCZOS)
    else:
        opened = closed  # 口開きが無ければ口パクしない(静止立ち絵)
    return closed, opened


def make_base(cfg: dict, base_dir: Path) -> Image.Image:
    """背景レイヤーを作る。画像指定があれば画面いっぱいに切り抜いて敷く。"""
    w, h = cfg["size"]
    bg = cfg["background"]
    canvas = Image.new("RGBA", (w, h), bg["color"])
    if bg.get("image"):
        path = resolve_path(base_dir, bg["image"])
        if not path.exists():
            raise ScriptError(f"背景画像が見つかりません: {path}")
        img = Image.open(path).convert("RGBA")
        scale = max(w / img.width, h / img.height)  # 短辺基準で拡大してセンター切り抜き
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                         Image.LANCZOS)
        canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2), img)
    return canvas


class SceneRenderer:
    """1フレームぶんの合成を担当する。フォントと画像の縮尺はキャッシュする。"""

    def __init__(self, cfg: dict, base_dir: Path):
        self.cfg = cfg
        self.base_dir = base_dir
        self.w, self.h = cfg["size"]
        self.base = make_base(cfg, base_dir)
        self.char_closed, self.char_open = load_character(cfg["character"], base_dir, self.h)
        font_path = cfg["subtitle"]["font"]
        self.f_sub = resolve_font(font_path, max(1, int(self.h * cfg["subtitle"]["size_ratio"])))
        self.f_banner = resolve_font(font_path, max(1, int(self.h * cfg["banner"]["size_ratio"])))
        self.f_com = resolve_font(font_path, max(1, int(self.h * cfg["comment"]["size_ratio"])))
        self.f_com_name = resolve_font(font_path,
                                       max(1, int(self.h * cfg["comment"]["name_size_ratio"])))
        self._sources = {}    # 画像パス -> 元画像
        self._scaled = {}     # (パス, ズーム倍率) -> ボックスに切り抜いた画像

    @property
    def has_mouth(self) -> bool:
        return self.char_closed is not None and self.char_open is not self.char_closed

    def _photo(self, path: Path, zoom: float) -> Image.Image:
        key = (str(path), round(zoom, 4))
        if key in self._scaled:
            return self._scaled[key]
        if str(path) not in self._sources:
            self._sources[str(path)] = Image.open(path).convert("RGBA")
        src = self._sources[str(path)]
        ic = self.cfg["image"]
        bw = max(1, int(self.w * ic["width_ratio"]))
        bh = max(1, int(self.h * ic["height_ratio"]))
        s = max(bw / src.width, bh / src.height) * zoom
        img = src.resize((max(bw, int(src.width * s)), max(bh, int(src.height * s))),
                         Image.LANCZOS)
        x = (img.width - bw) // 2
        y = (img.height - bh) // 2
        out = img.crop((x, y, x + bw, y + bh))
        self._scaled[key] = out
        return out

    def _draw_outlined(self, draw, lines, font, color, stroke, stroke_w, y, spacing):
        for line in lines:
            tw = draw.textlength(line, font=font)
            draw.text(((self.w - tw) / 2, y), line, font=font, fill=color,
                      stroke_width=stroke_w, stroke_fill=stroke)
            y += font.size + spacing
        return y

    def render(self, ctx: dict, mouth_open: bool, zoom: float) -> Image.Image:
        cfg = self.cfg
        frame = self.base.copy()

        # 写真 (まとめ型のメインビジュアル)
        if ctx["image"] is not None:
            photo = self._photo(ctx["image"], zoom)
            ic = cfg["image"]
            frame.paste(photo, ((self.w - photo.width) // 2, int(self.h * ic["top_ratio"])),
                        photo)

        # 立ち絵
        char = self.char_open if mouth_open else self.char_closed
        if char is not None:
            ch = cfg["character"]
            y = self.h - int(self.h * ch["bottom_margin_ratio"]) - char.height
            if ch["align"] == "left":
                x = int(self.w * 0.05)
            elif ch["align"] == "right":
                x = self.w - char.width - int(self.w * 0.05)
            else:
                x = (self.w - char.width) // 2
            frame.paste(char, (x, y), char)

        draw = ImageDraw.Draw(frame)

        # 見出しバナー (常時表示)
        bn = cfg["banner"]
        if bn["text"]:
            lines = wrap_text(bn["text"], bn["max_chars_per_line"],
                              self.f_banner, self.w * bn["max_width_ratio"])
            self._draw_outlined(draw, lines, self.f_banner, bn["color"], bn["stroke"],
                                max(1, int(self.h * bn["stroke_width_ratio"])),
                                int(self.h * bn["top_ratio"]),
                                int(self.f_banner.size * bn["line_spacing_ratio"]))

        # ネットの反応カード
        if ctx["comments"]:
            self._draw_comments(frame, ctx["comments"])
            draw = ImageDraw.Draw(frame)

        # 字幕 (talk行のみ。comment行は本文がカードに出るので重ねない)
        st = cfg["subtitle"]
        if ctx["style"] == "talk" and ctx["text"]:
            lines = wrap_text(ctx["text"], st["max_chars_per_line"],
                              self.f_sub, self.w * st["max_width_ratio"])
            spacing = int(self.f_sub.size * st["line_spacing_ratio"])
            total_h = len(lines) * self.f_sub.size + (len(lines) - 1) * spacing
            y = self.h - int(self.h * st["bottom_ratio"]) - total_h
            self._draw_outlined(draw, lines, self.f_sub, st["color"], st["stroke"],
                                max(1, int(self.h * st["stroke_width_ratio"])), y, spacing)
        return frame

    def _draw_comments(self, frame: Image.Image, comments: list) -> None:
        cm = self.cfg["comment"]
        cw = int(self.w * cm["width_ratio"])
        x0 = (self.w - cw) // 2
        pad = int(self.h * cm["pad_ratio"])
        gap = int(self.h * cm["gap_ratio"])
        y = int(self.h * cm["top_ratio"])
        overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        placed = []
        for c in comments:
            body = wrap_text(c["text"], cm["max_chars_per_line"], self.f_com, cw - 2 * pad)
            head_h = self.f_com_name.size
            body_h = len(body) * int(self.f_com.size * 1.25)
            card_h = pad + head_h + pad // 2 + body_h + pad
            od.rounded_rectangle((x0, y, x0 + cw, y + card_h), radius=int(pad * 0.8),
                                 fill=hex_rgba(cm["bg"], int(cm["bg_alpha"])))
            placed.append((c, body, y))
            y += card_h + gap
        frame.alpha_composite(overlay)
        draw = ImageDraw.Draw(frame)
        for c, body, cy in placed:
            ty = cy + pad
            draw.text((x0 + pad, ty), f"{c['no']}: {c['name']}",
                      font=self.f_com_name, fill=cm["name_color"])
            ty += self.f_com_name.size + pad // 2
            for line in body:
                draw.text((x0 + pad, ty), line, font=self.f_com, fill=cm["text_color"])
                ty += int(self.f_com.size * 1.25)


# ---------------------------------------------------------------- 台本読み込み


def build_contexts(lines: list, cfg: dict, base_dir: Path) -> list:
    """行ごとの画面状態(写真・コメントの積み上がり)を組み立てる。

    image: は一度指定すると以降の行にも表示され続ける(スライド式)。
    style: comment の行はカードとして積まれ、max_visible を超えると古い順に消える。
    """
    sticky_image = None
    stack = []
    ctxs = []
    for i, ln in enumerate(lines, 1):
        if ln.get("image"):
            p = resolve_path(base_dir, ln["image"])
            if not p.exists():
                raise ScriptError(f"{i}番目の行の画像が見つかりません: {p}")
            sticky_image = p
        style = ln.get("style", "talk")
        if style not in ("talk", "comment"):
            raise ScriptError(f"{i}番目の行の style が不正です: {style} (talk か comment)")
        if style == "comment":
            stack.append({"no": len(stack) + 1,
                          "name": ln.get("name") or cfg["comment"]["name"],
                          "text": ln.get("text", "")})
        ctxs.append({
            "style": style,
            "text": ln.get("text", ""),
            "image": sticky_image,
            "comments": list(stack[-int(cfg["comment"]["max_visible"]):]),
        })
    return ctxs


def load_script(path: Path) -> tuple:
    """台本YAMLを読み、既定値をかぶせて (設定, 台詞リスト, 台本の場所) を返す。"""
    if not path.exists():
        raise ScriptError(f"台本が見つかりません: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base_dir = path.parent
    cfg = deep_merge(DEFAULTS, raw)
    cfg["size"] = [int(cfg["size"][0]), int(cfg["size"][1])]

    lines = raw.get("lines") or []
    if not lines:
        raise ScriptError("台本に lines がありません。台詞を1つ以上書いてください。")

    rate_hint = None
    for i, ln in enumerate(lines, 1):
        if ln.get("audio"):
            wav = resolve_path(base_dir, ln["audio"])
            if not wav.exists():
                raise ScriptError(
                    f"{i}番目の音声が見つかりません: {wav}\n"
                    "Style-Bert-VITS2で書き出したWAVを置いてください。"
                )
            samples, rate = read_wav_mono(wav)
            ln["_samples"], ln["_rate"] = samples, rate
            ln["_sec"] = len(samples) / rate
            rate_hint = rate
        elif ln.get("duration"):
            ln["_sec"] = float(ln["duration"])  # 無音行: 表示時間だけ指定
        else:
            raise ScriptError(
                f"{i}番目の行に audio (音声WAV) か duration (表示秒数) が必要です。"
            )
        ln.setdefault("text", "")

    # 無音行に、音声行と同じレートの無音サンプルを入れる
    for ln in lines:
        if "_samples" not in ln:
            rate = rate_hint or SILENT_RATE
            ln["_samples"] = np.zeros(int(rate * ln["_sec"]), dtype=np.float32)
            ln["_rate"] = rate
    return cfg, lines, base_dir


# ---------------------------------------------------------------- 本体


def build(args) -> int:
    script_path = Path(args.script).resolve()
    cfg, lines, base_dir = load_script(script_path)
    ctxs = build_contexts(lines, cfg, base_dir)

    fps = int(cfg["fps"])
    gap = float(cfg["gap_sec"])
    total_sec = sum(ln["_sec"] for ln in lines) + gap * (len(lines) - 1)

    print(f"台詞 {len(lines)}本 / 想定尺 {total_sec:.1f}秒 / "
          f"{cfg['size'][0]}x{cfg['size'][1]} {fps}fps")
    for i, (ln, ctx) in enumerate(zip(lines, ctxs), 1):
        tag = "コメ" if ctx["style"] == "comment" else "　　"
        mark = "画" if ln.get("image") else "　"
        preview = ln["text"].replace("\n", " ")[:22]
        print(f"  {i:2d}. {ln['_sec']:5.2f}秒 {tag}{mark} {preview}")
    if total_sec > SHORTS_MAX_SEC:
        print(f"  ⚠ {SHORTS_MAX_SEC}秒を超えています。ショートではなく通常動画として"
              "扱われる可能性があります(投稿画面で確認してください)")
    if args.probe:
        return 0

    ffmpeg = find_ffmpeg()
    renderer = SceneRenderer(cfg, base_dir)

    out_path = Path(args.output or script_path.with_suffix(".mp4")).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ic = cfg["image"]
    zooming = ic["zoom_to"] > ic["zoom_from"]
    step = max(1, int(ic["zoom_step_frames"]))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cache = {}

        def frame_file(li: int, mouth: bool, zstep: int, zoom: float) -> Path:
            key = (li, mouth, zstep)
            if key not in cache:
                img = renderer.render(ctxs[li], mouth, zoom)
                p = tmp / f"f{li:04d}_{int(mouth)}_{zstep:04d}.png"
                img.convert("RGB").save(p)
                cache[key] = p
            return cache[key]

        entries = []

        def push(path: Path, n: int):
            if n <= 0:
                return
            if entries and entries[-1][0] == path:
                entries[-1][1] += n
            else:
                entries.append([path, n])

        for li, (ln, ctx) in enumerate(zip(lines, ctxs)):
            n = max(1, int(round(ln["_sec"] * fps)))
            states = (mouth_states(ln["_samples"], ln["_rate"], n, cfg["mouth"])
                      if renderer.has_mouth else [False] * n)
            line_zoom = zooming and ctx["image"] is not None
            last = (0, ic["zoom_from"])
            for f in range(n):
                if line_zoom:
                    zstep = f // step
                    t = (zstep * step) / max(1, n - 1)
                    zoom = ic["zoom_from"] + (ic["zoom_to"] - ic["zoom_from"]) * min(1.0, t)
                else:
                    zstep, zoom = 0, ic["zoom_from"]
                last = (zstep, zoom)
                push(frame_file(li, states[f], zstep, zoom), 1)
            if li < len(lines) - 1:  # 行間は口を閉じ、ズームは止めたまま
                push(frame_file(li, False, last[0], last[1]), int(round(gap * fps)))

        concat = tmp / "frames.txt"
        with concat.open("w", encoding="utf-8") as fh:
            for path, n in entries:
                fh.write(f"file '{path.as_posix()}'\nduration {n / fps:.6f}\n")
            fh.write(f"file '{entries[-1][0].as_posix()}'\n")  # 最終フレームは要再掲

        voice = tmp / "voice.wav"
        build_voice_track(lines, gap, voice)

        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
               "-i", str(voice)]
        bgm = cfg["bgm"]
        if bgm.get("file"):
            bgm_path = resolve_path(base_dir, bgm["file"])
            if not bgm_path.exists():
                raise ScriptError(f"BGMが見つかりません: {bgm_path}")
            cmd += ["-stream_loop", "-1", "-i", str(bgm_path), "-filter_complex",
                    f"[2:a]volume={bgm['gain_db']}dB[b];"
                    "[1:a][b]amix=inputs=2:duration=first[a]",
                    "-map", "0:v", "-map", "[a]"]
        else:
            cmd += ["-map", "0:v", "-map", "1:a"]
        cmd += ["-r", str(fps), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-shortest", str(out_path)]

        print(f"エンコード中… ({len(entries)}区間 / 静止画 {len(cache)}枚)")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            sys.stderr.write(res.stderr[-4000:] + "\n")
            raise ScriptError("ffmpegが失敗しました。上のログを確認してください。")

    print(f"✓ 書き出し完了: {out_path}  ({total_sec:.1f}秒)")
    return 0


TEMPLATE = """\
# ショート動画の台本。voice/ にStyle-Bert-VITS2で書き出したWAVを置く。
title: "サンプルショート"
fps: 30
size: [1080, 1920]

background:
  color: "#101024"
  # image: assets/bg.png

character:
  # scripts/normalize_psd.py の --emit-pngs で書き出した立ち絵を使う
  # closed: assets/char_closed.png
  # open:   assets/char_open.png
  height_ratio: 0.55
  align: center

subtitle:
  # Windowsなら例: C:\\Windows\\Fonts\\meiryob.ttc (未指定なら自動で探す)
  font: null
  size_ratio: 0.055
  max_chars_per_line: 16

# bgm:
#   file: assets/bgm.mp3
#   gain_db: -18

lines:
  - text: "1行目の台詞をここに書く"
    audio: voice/001.wav
  - text: "2行目。字幕は自動で折り返される"
    audio: voice/002.wav
"""

TEMPLATE_MATOME = """\
# 「◯◯、〜すぎると話題に」型ショートの台本。
# 見出し + 写真スローズーム + ネットの反応カードの構成。
# assets/ に使う画像を置く (使用権のある画像だけを使うこと)。
title: "まとめ型サンプル"
fps: 30
size: [1080, 1920]

background:
  color: "#0d0d16"

# 画面上部に出しっぱなしになる見出し (スレタイの型)
banner:
  text: "◯◯◯◯、最高すぎると話題に"

# 写真の出る位置とスローズームの強さ
image:
  height_ratio: 0.38
  zoom_to: 1.08      # 1.0 にするとズームなし(書き出しが速くなる)

# ネットの反応カードの見た目
comment:
  name: "名無しさん"   # name未指定のコメントに使う名前
  max_visible: 3       # 同時に見えるカード数

subtitle:
  font: null           # Windowsなら自動でメイリオ等を探す

# bgm:
#   file: assets/bgm.mp3
#   gain_db: -18

lines:
  # ナレーション行 (style省略=talk)。image: は以降の行にも表示され続ける
  - text: "先日のイベントでの一幕が話題になっています"
    audio: voice/001.wav
    image: assets/photo1.png

  # ネットの反応。audio で読み上げるか、duration で表示するだけかを選べる
  - style: comment
    text: "これは伝説"
    audio: voice/002.wav
  - style: comment
    name: "風吹けば名無し"
    text: "本人が一番楽しそうなの好き"
    duration: 2.5
  - style: comment
    text: "何年経ってもすごいわ"
    duration: 2.5

  # 写真を差し替えつつ締めのナレーション
  - text: "これからの活動にも注目です"
    audio: voice/003.wav
    image: assets/photo2.png
"""


def init(args) -> int:
    out_dir = Path(args.directory).resolve()
    (out_dir / "voice").mkdir(parents=True, exist_ok=True)
    (out_dir / "assets").mkdir(parents=True, exist_ok=True)
    script = out_dir / "script.yaml"
    if script.exists() and not args.force:
        raise ScriptError(f"すでにあります: {script}  (上書きするなら --force)")
    script.write_text(TEMPLATE_MATOME if args.style == "matome" else TEMPLATE,
                      encoding="utf-8")
    print(f"✓ 台本テンプレを作成: {script}  (style={args.style})")
    print(f"  1) {out_dir / 'voice'} に台詞WAVを置く")
    print(f"  2) {out_dir / 'assets'} に画像を置く")
    print(f"  3) python scripts/make_short.py build {script} -o out.mp4")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="台本YAMLからショート動画(縦型MP4)を作る")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="台本テンプレとフォルダ一式を作る")
    p_init.add_argument("directory")
    p_init.add_argument("--style", choices=["talk", "matome"], default="talk",
                        help="talk=立ち絵+字幕 / matome=見出し+写真+反応カード")
    p_init.add_argument("--force", action="store_true", help="既存のscript.yamlを上書き")
    p_init.set_defaults(func=init)

    p_build = sub.add_parser("build", help="台本から動画を書き出す")
    p_build.add_argument("script")
    p_build.add_argument("-o", "--output", help="出力MP4 (既定: 台本と同じ場所)")
    p_build.add_argument("--probe", action="store_true",
                         help="書き出さずに尺と構成だけ表示する")
    p_build.set_defaults(func=build)

    args = ap.parse_args()
    try:
        return args.func(args)
    except ScriptError as e:
        sys.stderr.write(f"\nエラー: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
