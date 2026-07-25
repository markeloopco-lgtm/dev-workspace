#!/usr/bin/env python3
"""金融解説チャンネル: 長尺動画の自動生成パイプライン。

台本生成(Gemini) → 音声合成(Style-Bert-VITS2) → モーショングラフィックス描画(PIL+ffmpeg)
→ サムネイル生成 までを一気通貫で行う。YouTubeへの投稿は scripts/upload_youtube.py。

詳細な手順は docs/06_finance_video_automation.md 参照。

  python scripts/make_finance_video.py demo                  # サンプル台本で全工程テスト(無音)
  python scripts/make_finance_video.py script --next         # ネタ帳の次のトピックで台本生成
  python scripts/make_finance_video.py all --next            # 台本→音声→動画→サムネ
  python scripts/make_finance_video.py render <出力dir> --preview 20 --scale 0.5

依存: Pillow / numpy / PyYAML (requirements.txt) + ffmpeg (PATH上に必要)
"""

from __future__ import annotations

import argparse
import bisect
import datetime
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import wave
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "configs" / "finance_channel.yaml"
VIDEOS_DIR = REPO_ROOT / "output" / "videos"

AUDIO_RATE = 44100  # 全音声をこのレートのモノラル16bitに揃える
ENVELOPE_HZ = 50    # 口パク用の音量エンベロープの解像度


# ---------------------------------------------------------------- 共通ユーティリティ

def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def slugify(topic: str, max_len: int = 32) -> str:
    s = re.sub(r'[\\/:*?"<>|\s！？。、．，]+', "_", topic).strip("_")
    return s[:max_len] or "video"


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def ease_out(p: float) -> float:
    """0..1 の進行を減速カーブに（cubic ease-out）。"""
    p = clamp(p, 0.0, 1.0)
    return 1 - (1 - p) ** 3


def find_font(cfg: dict) -> str:
    for cand in cfg["fonts"]["candidates"]:
        if Path(cand).exists():
            return cand
    raise SystemExit(
        "日本語フォントが見つかりません。configs/finance_channel.yaml の "
        "fonts.candidates に実在するフォントパスを追加してください。"
    )


def run_ffmpeg(args: list[str]):
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args])
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg失敗: {' '.join(args)}")


# ---------------------------------------------------------------- 台本 (script.json)

SCRIPT_SCHEMA_EXAMPLE = {
    "title": "動画タイトル（30字以内・興味を引く）",
    "description": "概要欄の紹介文（2〜3文）",
    "tags": ["金融", "解説"],
    "sections": [
        {
            "heading": "セクション見出し（10字前後）",
            "cues": [
                {
                    "text": "ナレーション1文（読み上げる文。40〜80字）",
                    "caption": "画面下の字幕（省略時はtextと同じ）",
                    "visual": {"type": "keyword", "main": "複利", "sub": "利息が利息を生む"},
                }
            ],
        }
    ],
}

VISUAL_TYPES = """visualの種類（変化をつけて使い分ける）:
- {"type":"keyword","main":"強調ワード","sub":"補足ひとこと"}
- {"type":"list","title":"見出し","items":["項目1","項目2","項目3"]}
- {"type":"chart_line","title":"グラフ名","labels":["1年","5年","10年"],
   "series":[{"name":"系列名","values":[100,120,150]}]}   … 系列は1〜2本
- {"type":"chart_bar","title":"グラフ名","labels":["A","B"],
   "series":[{"name":"系列名","values":[10,20]}]}
- {"type":"compare","title":"比較","left":{"title":"A","items":["…"]},
   "right":{"title":"B","items":["…"]}}
- {"type":"none"}   … 字幕のみ（多用しない）"""


def build_prompt(cfg: dict, topic: str) -> str:
    minutes = cfg["script_gen"]["target_minutes"]
    chars = int(minutes * 330)  # 日本語ナレーションはおよそ330字/分
    return f"""あなたは日本語の金融解説YouTubeチャンネル「{cfg['channel']['name']}」の放送作家です。
テーマ「{topic}」の解説動画の台本を、下記のJSONスキーマ**のみ**で出力してください。

制約:
- ナレーション合計はおよそ{chars}字（{minutes}分想定）。セクションは4〜6個
- 各cueのtextは読み上げ用の自然な話し言葉1文（40〜80字）。captionはその要約（30字以内）
- {cfg['script_gen']['style'].strip()}
- 数値を出すときは現実的な概算・一般論にとどめ、visualのチャートと一致させる
- 最初のセクションは導入（視聴者の悩み・疑問から入る）、最後はまとめ
- 免責事項は本文に入れない（概要欄に自動で付く）

{VISUAL_TYPES}

スキーマ例:
{json.dumps(SCRIPT_SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)}
"""


def gemini_generate(cfg: dict, topic: str) -> dict:
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise SystemExit(
            "環境変数 GOOGLE_API_KEY が未設定です。docs/06のStep2を参照してください。\n"
            '  PowerShell: $env:GOOGLE_API_KEY = "キー"'
        )
    model = cfg["script_gen"]["model"]
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    body = {
        "contents": [{"parts": [{"text": build_prompt(cfg, topic)}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.7},
    }
    req = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=180) as res:
        data = json.load(res)
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def validate_script(s: dict) -> dict:
    """Geminiの出力ゆらぎを吸収して正規化する。"""
    if not isinstance(s.get("title"), str) or not s["title"].strip():
        raise ValueError("台本にtitleがありません")
    sections = s.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("台本にsectionsがありません")
    out_sections = []
    for sec in sections:
        cues = []
        for cue in sec.get("cues", []):
            text = str(cue.get("text", "")).strip()
            if not text:
                continue
            visual = cue.get("visual") or {"type": "none"}
            if not isinstance(visual, dict) or "type" not in visual:
                visual = {"type": "none"}
            cues.append(
                {
                    "text": text,
                    "caption": str(cue.get("caption") or text).strip(),
                    "visual": visual,
                }
            )
        if cues:
            out_sections.append({"heading": str(sec.get("heading", "")).strip(), "cues": cues})
    if not out_sections:
        raise ValueError("有効なcueがありません")
    return {
        "title": s["title"].strip(),
        "description": str(s.get("description", "")).strip(),
        "tags": [str(t) for t in s.get("tags", [])][:15],
        "sections": out_sections,
    }


def cmd_script(cfg: dict, topic: str) -> Path:
    outdir = VIDEOS_DIR / f"{datetime.date.today():%Y%m%d}_{slugify(topic)}"
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"[script] Geminiで台本生成中: {topic}")
    script = validate_script(gemini_generate(cfg, topic))
    script["topic"] = topic
    (outdir / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    n_cues = sum(len(sec["cues"]) for sec in script["sections"])
    n_chars = sum(len(c["text"]) for sec in script["sections"] for c in sec["cues"])
    print(f"[script] OK: {outdir/'script.json'} ({len(script['sections'])}章 {n_cues}文 {n_chars}字)")
    return outdir


def next_topic(cfg: dict) -> str:
    done = {p.name.split("_", 1)[-1] for p in VIDEOS_DIR.glob("*") if p.is_dir()}
    for topic in cfg.get("topics", []):
        if slugify(topic) not in done:
            return topic
    raise SystemExit("ネタ帳(topics)を使い切りました。configs/finance_channel.yaml に追記してください。")


# ---------------------------------------------------------------- 音声 (TTS + タイミング)

def write_silence(path: Path, sec: float):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes(b"\x00\x00" * int(sec * AUDIO_RATE))


def read_wav_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == AUDIO_RATE and w.getsampwidth() == 2, path
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)
    return data


def tts_sbv2(cfg: dict, text: str, raw_path: Path):
    t = cfg["tts"]
    params = {
        "text": text,
        "model_id": t["model_id"],
        "speaker_id": t["speaker_id"],
        "style": t.get("style", "Neutral"),
        "length": t.get("length", 1.0),
        "language": "JP",
        "auto_split": "true",
    }
    url = f"{t['endpoint'].rstrip('/')}/voice?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=600) as res:
            raw_path.write_bytes(res.read())
    except OSError as e:
        raise SystemExit(
            f"Style-Bert-VITS2サーバーに接続できません ({t['endpoint']}): {e}\n"
            "docs/04 Step3のサーバーを起動するか、テストなら --engine none を使ってください。"
        )


def cmd_tts(cfg: dict, outdir: Path, engine: str | None = None):
    script = json.loads((outdir / "script.json").read_text(encoding="utf-8"))
    engine = engine or cfg["tts"]["engine"]
    audio_dir = outdir / "audio"
    audio_dir.mkdir(exist_ok=True)
    v = cfg["video"]
    gap, intro, outro = v["cue_gap_sec"], v["intro_sec"], v["outro_sec"]

    chunks: list[np.ndarray] = [np.zeros(int(intro * AUDIO_RATE), dtype=np.int16)]
    cues_out = []
    pos = intro
    idx = 0
    for si, sec in enumerate(script["sections"]):
        for cue in sec["cues"]:
            wav = audio_dir / f"cue_{idx:03d}.wav"
            if engine == "none":
                # 無音テスト: 日本語はおよそ0.18秒/字で見積もる
                write_silence(wav, max(1.5, len(cue["text"]) * 0.18))
            else:
                raw = audio_dir / f"cue_{idx:03d}_raw.wav"
                print(f"[tts] {idx:03d}: {cue['text'][:24]}…")
                tts_sbv2(cfg, cue["text"], raw)
                run_ffmpeg(["-i", str(raw), "-ar", str(AUDIO_RATE), "-ac", "1",
                            "-sample_fmt", "s16", str(wav)])
                raw.unlink()
            data = read_wav_mono(wav)
            dur = len(data) / AUDIO_RATE
            cues_out.append(
                {"index": idx, "section": si, "heading": sec["heading"],
                 "start": round(pos, 3), "end": round(pos + dur, 3),
                 "caption": cue["caption"], "visual": cue["visual"]}
            )
            chunks.append(data)
            chunks.append(np.zeros(int(gap * AUDIO_RATE), dtype=np.int16))
            pos += dur + gap
            idx += 1
    chunks.append(np.zeros(int(outro * AUDIO_RATE), dtype=np.int16))

    mix = np.concatenate(chunks)
    with wave.open(str(outdir / "narration.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes(mix.tobytes())

    # 口パク用の音量エンベロープ（キャラ未設定でも軽いので常に保存）
    win = AUDIO_RATE // ENVELOPE_HZ
    n = len(mix) // win
    rms = np.sqrt((mix[: n * win].astype(np.float64).reshape(n, win) ** 2).mean(axis=1))
    peak = rms.max() or 1.0
    timing = {
        "intro": intro, "outro": outro,
        "total": round(len(mix) / AUDIO_RATE, 3),
        "cues": cues_out,
        "envelope_hz": ENVELOPE_HZ,
        "envelope": [round(float(x / peak), 2) for x in rms],
    }
    (outdir / "timing.json").write_text(
        json.dumps(timing, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[tts] OK: {timing['total']:.1f}秒 / {idx}文 → narration.wav, timing.json")


# ---------------------------------------------------------------- 描画ヘルパー

def wrap_text(font: ImageFont.FreeTypeFont, text: str, max_w: float) -> list[str]:
    """日本語向けの1文字単位折り返し。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n" or font.getlength(cur + ch) > max_w:
            if cur:
                lines.append(cur)
            cur = "" if ch == "\n" else ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def text_alpha(img: Image.Image, xy, text, font, fill_rgb, alpha, anchor=None):
    """透明度つきテキスト描画（フェード用）。"""
    if alpha <= 0:
        return
    if alpha >= 1:
        ImageDraw.Draw(img).text(xy, text, font=font, fill=fill_rgb, anchor=anchor)
        return
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).text(xy, text, font=font, fill=(*fill_rgb, int(alpha * 255)), anchor=anchor)
    img.alpha_composite(ov)


class Renderer:
    def __init__(self, cfg: dict, outdir: Path, fps: int, scale: float):
        self.cfg = cfg
        self.outdir = outdir
        self.fps = fps
        self.s = scale
        self.W = int(cfg["video"]["width"] * scale) // 2 * 2
        self.H = int(cfg["video"]["height"] * scale) // 2 * 2
        th = cfg["theme"]
        self.col = {k: hex_rgb(v) for k, v in th.items() if isinstance(v, str) and v.startswith("#")}
        font_path = find_font(cfg)
        fs = lambda px: ImageFont.truetype(font_path, max(10, int(px * scale)))
        self.f = {
            "big": fs(112), "h1": fs(76), "h2": fs(52), "body": fs(42),
            "caption": fs(40), "small": fs(30),
        }
        self.timing = json.loads((outdir / "timing.json").read_text(encoding="utf-8"))
        self.script = json.loads((outdir / "script.json").read_text(encoding="utf-8"))
        self.cue_starts = [c["start"] for c in self.timing["cues"]]
        self.base_bg = self._make_gradient()
        self.char_imgs = self._load_character()

    # --- 背景・共通部品

    def _make_gradient(self) -> Image.Image:
        top, btm = np.array(self.col["bg_top"], float), np.array(self.col["bg_bottom"], float)
        t = np.linspace(0, 1, self.H)[:, None, None]
        arr = (top * (1 - t) + btm * t).astype(np.uint8)
        return Image.fromarray(np.broadcast_to(arr, (self.H, self.W, 3)).copy(), "RGB").convert("RGBA")

    def _load_character(self):
        c = self.cfg.get("character") or {}
        if not c.get("base"):
            return None
        base = Image.open(REPO_ROOT / c["base"]).convert("RGBA")
        h = int(self.H * c.get("height_ratio", 0.45))
        ratio = h / base.height
        base = base.resize((int(base.width * ratio), h), Image.LANCZOS)
        mouth = None
        if c.get("mouth_open"):
            mouth = Image.open(REPO_ROOT / c["mouth_open"]).convert("RGBA")
            mouth = mouth.resize(base.size, Image.LANCZOS)
        return {"base": base, "mouth": mouth}

    def background(self, t: float) -> Image.Image:
        img = self.base_bg.copy()
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        for i in range(5):  # ゆっくり漂う光の玉
            ph = i * 1.7
            x = (0.15 + 0.18 * i) * self.W + math.sin(t * 0.12 + ph) * 0.06 * self.W
            y = (0.25 + 0.12 * (i % 3)) * self.H + math.cos(t * 0.09 + ph * 2) * 0.08 * self.H
            r = (0.05 + 0.02 * (i % 3)) * self.W
            d.ellipse([x - r, y - r, x + r, y + r], fill=(*self.col["accent"], 10))
        img.alpha_composite(ov.filter(ImageFilter.GaussianBlur(int(8 * self.s) or 1)))
        return img

    def progress_bar(self, img, t):
        d = ImageDraw.Draw(img)
        h = max(3, int(6 * self.s))
        d.rectangle([0, self.H - h, self.W * t / self.timing["total"], self.H], fill=self.col["accent"])

    def header(self, img, heading: str):
        d = ImageDraw.Draw(img)
        pad = int(50 * self.s)
        d.text((pad, pad), self.cfg["channel"]["name"], font=self.f["small"], fill=self.col["muted"])
        if heading:
            d.rectangle([pad, int(120 * self.s), pad + int(10 * self.s), int(180 * self.s)],
                        fill=self.col["accent"])
            d.text((pad + int(28 * self.s), int(118 * self.s)), heading,
                   font=self.f["h2"], fill=self.col["text"])

    def caption_bar(self, img, text: str, prog: float):
        a = ease_out(prog / 0.25)
        maxw = self.W * 0.86
        lines = wrap_text(self.f["caption"], text, maxw)[:2]
        lh = int(58 * self.s)
        bh = lh * len(lines) + int(40 * self.s)
        y0 = self.H - int(60 * self.s) - bh + (1 - a) * int(20 * self.s)
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        d.rounded_rectangle([self.W * 0.05, y0, self.W * 0.95, y0 + bh],
                            radius=int(18 * self.s), fill=(*self.col["caption_bg"], int(200 * a)))
        for i, ln in enumerate(lines):
            d.text((self.W / 2, y0 + int(24 * self.s) + i * lh + lh / 2), ln,
                   font=self.f["caption"], fill=(*self.col["text"], int(255 * a)),
                   anchor="mm")
        img.alpha_composite(ov)

    def character(self, img, t: float):
        if not self.char_imgs:
            return
        base = self.char_imgs["base"]
        idx = int(t * self.timing["envelope_hz"])
        env = self.timing["envelope"]
        level = env[idx] if idx < len(env) else 0.0
        sprite = base
        if self.char_imgs["mouth"] and level > 0.25 and int(t * 8) % 2 == 0:
            sprite = self.char_imgs["mouth"]
        bob = int(math.sin(t * 2 * math.pi * 0.4) * 8 * self.s)
        img.alpha_composite(sprite, (self.W - sprite.width - int(30 * self.s),
                                     self.H - sprite.height + int(12 * self.s) + bob))

    # --- 場面ごとの描画

    def draw_intro(self, img, t):
        a = ease_out(t / 0.8)
        d = ImageDraw.Draw(img)
        cy = self.H * 0.42
        for i, ln in enumerate(wrap_text(self.f["h1"], self.script["title"], self.W * 0.85)[:3]):
            text_alpha(img, (self.W / 2, cy + i * int(100 * self.s) - (1 - a) * 30 * self.s),
                       ln, self.f["h1"], self.col["text"], a, anchor="mm")
        lw = ease_out((t - 0.5) / 0.8) * self.W * 0.3
        if lw > 0:
            d.rectangle([self.W / 2 - lw, self.H * 0.62, self.W / 2 + lw, self.H * 0.62 + 5 * self.s],
                        fill=self.col["accent"])
        text_alpha(img, (self.W / 2, self.H * 0.7), self.cfg["channel"]["tagline"],
                   self.f["body"], self.col["muted"], ease_out((t - 0.9) / 0.8), anchor="mm")

    def draw_outro(self, img, t):
        a = ease_out(t / 0.8)
        text_alpha(img, (self.W / 2, self.H * 0.3), "ご視聴ありがとうございました",
                   self.f["h1"], self.col["text"], a, anchor="mm")
        y = self.H * 0.48
        for ln in self.cfg["upload"]["description_footer"].strip().splitlines():
            for sub in wrap_text(self.f["small"], ln.strip(), self.W * 0.8):
                text_alpha(img, (self.W / 2, y), sub, self.f["small"], self.col["muted"],
                           ease_out((t - 0.5) / 0.8), anchor="mm")
                y += int(46 * self.s)
        text_alpha(img, (self.W / 2, self.H * 0.78), self.cfg["channel"]["name"],
                   self.f["h2"], self.col["accent"], ease_out((t - 0.8) / 0.8), anchor="mm")

    def content_area(self):
        """ビジュアルの描画領域 (x0, y0, x1, y1)。"""
        return (self.W * 0.08, self.H * 0.24, self.W * 0.92, self.H * 0.68)

    def draw_visual(self, img, visual: dict, prog: float, t: float):
        kind = visual.get("type", "none")
        fn = getattr(self, f"vis_{kind}", None)
        if fn:
            fn(img, visual, prog, t)

    # --- visual別描画

    def vis_keyword(self, img, v, prog, t):
        x0, y0, x1, y1 = self.content_area()
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        a = ease_out(prog / 0.2)
        main = str(v.get("main", ""))[:12]
        font = self.f["big"] if len(main) <= 7 else self.f["h1"]
        text_alpha(img, (cx, cy - 30 * self.s), main, font, self.col["accent"], a, anchor="mm")
        w = font.getlength(main) / 2 * ease_out((prog - 0.1) / 0.25)
        if w > 0:
            ImageDraw.Draw(img).rectangle(
                [cx - w, cy + 60 * self.s, cx + w, cy + 60 * self.s + 6 * self.s],
                fill=self.col["text"])
        if v.get("sub"):
            text_alpha(img, (cx, cy + 130 * self.s), str(v["sub"]), self.f["body"],
                       self.col["text"], ease_out((prog - 0.25) / 0.3), anchor="mm")

    def vis_none(self, img, v, prog, t):
        pass

    def vis_list(self, img, v, prog, t):
        x0, y0, x1, y1 = self.content_area()
        d = ImageDraw.Draw(img)
        if v.get("title"):
            text_alpha(img, (x0, y0), str(v["title"]), self.f["h2"], self.col["accent"],
                       ease_out(prog / 0.2))
            y0 += 90 * self.s
        items = [str(i) for i in v.get("items", [])][:5]
        step = min(110 * self.s, (y1 - y0) / max(1, len(items)))
        for i, item in enumerate(items):
            a = ease_out((prog - 0.15 - i * 0.55 / max(1, len(items))) / 0.2)
            if a <= 0:
                continue
            y = y0 + i * step
            r = 12 * self.s
            ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(ov)
            od.ellipse([x0 + (1 - a) * 30 * self.s, y + 18 * self.s - r,
                        x0 + (1 - a) * 30 * self.s + 2 * r, y + 18 * self.s + r],
                       fill=(*self.col["accent"], int(255 * a)))
            img.alpha_composite(ov)
            text_alpha(img, (x0 + 45 * self.s + (1 - a) * 30 * self.s, y),
                       item[:26], self.f["body"], self.col["text"], a)

    def _chart_frame(self, img, v, prog):
        """チャート共通: タイトル・枠を描き、プロット領域を返す。"""
        x0, y0, x1, y1 = self.content_area()
        if v.get("title"):
            text_alpha(img, (x0, y0), str(v["title"]), self.f["h2"], self.col["accent"],
                       ease_out(prog / 0.2))
        px0, py0, px1, py1 = x0 + 40 * self.s, y0 + 100 * self.s, x1 - 40 * self.s, y1 - 50 * self.s
        d = ImageDraw.Draw(img)
        d.line([px0, py0, px0, py1, px1, py1], fill=self.col["muted"], width=max(2, int(3 * self.s)))
        return px0, py0, px1, py1

    def _series(self, v):
        series = [s for s in v.get("series", []) if s.get("values")][:2]
        allv = [float(x) for s in series for x in s["values"]]
        if not allv:
            return [], 0.0, 1.0
        lo, hi = min(allv + [0.0]), max(allv)
        if hi == lo:
            hi = lo + 1
        return series, lo, hi

    def vis_chart_line(self, img, v, prog, t):
        px0, py0, px1, py1 = self._chart_frame(img, v, prog)
        series, lo, hi = self._series(v)
        labels = [str(x) for x in v.get("labels", [])]
        colors = [self.col["positive"], self.col["negative"]]
        reveal = ease_out((prog - 0.15) / 0.6)
        d = ImageDraw.Draw(img)
        for si, s in enumerate(series):
            vals = [float(x) for x in s["values"]]
            n = len(vals)
            pts = [(px0 + (px1 - px0) * i / max(1, n - 1),
                    py1 - (py1 - py0) * (val - lo) / (hi - lo)) for i, val in enumerate(vals)]
            k = reveal * (n - 1)
            full = int(k)
            for i in range(full):
                d.line([pts[i], pts[i + 1]], fill=colors[si], width=max(3, int(6 * self.s)))
            if full < n - 1 and k > full:
                fx = pts[full][0] + (pts[full + 1][0] - pts[full][0]) * (k - full)
                fy = pts[full][1] + (pts[full + 1][1] - pts[full][1]) * (k - full)
                d.line([pts[full], (fx, fy)], fill=colors[si], width=max(3, int(6 * self.s)))
            for i, p in enumerate(pts[: full + 1]):
                r = 7 * self.s
                d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=colors[si])
            if s.get("name"):
                # 凡例はタイトルと重ならないよう右上に右詰めで置く
                name = str(s["name"])
                x = px1 - (len(series) - si) * 240 * self.s
                text_alpha(img, (x, py0 - 55 * self.s), f"― {name}",
                           self.f["small"], colors[si], ease_out(prog / 0.3))
        show = labels if len(labels) <= 6 else [labels[0], labels[len(labels) // 2], labels[-1]]
        idxs = (range(len(labels)) if len(labels) <= 6
                else [0, len(labels) // 2, len(labels) - 1])
        for i, lab in zip(idxs, show):
            x = px0 + (px1 - px0) * i / max(1, len(labels) - 1)
            text_alpha(img, (x, py1 + 15 * self.s), lab, self.f["small"], self.col["muted"],
                       reveal, anchor="ma")

    def vis_chart_bar(self, img, v, prog, t):
        px0, py0, px1, py1 = self._chart_frame(img, v, prog)
        series, lo, hi = self._series(v)
        labels = [str(x) for x in v.get("labels", [])]
        colors = [self.col["positive"], self.col["negative"]]
        d = ImageDraw.Draw(img)
        if not series:
            return
        n = len(series[0]["values"])
        group_w = (px1 - px0) / max(1, n)
        bar_w = group_w * 0.6 / len(series)
        for si, s in enumerate(series):
            for i, val in enumerate([float(x) for x in s["values"][:n]]):
                grow = ease_out((prog - 0.15 - i * 0.35 / max(1, n)) / 0.3)
                if grow <= 0:
                    continue
                h = (py1 - py0) * (val - lo) / (hi - lo) * grow
                x = px0 + i * group_w + group_w * 0.2 + si * bar_w
                d.rectangle([x, py1 - h, x + bar_w * 0.9, py1], fill=colors[si])
                if grow >= 1:
                    lab = f"{val:g}"
                    text_alpha(img, (x + bar_w * 0.45, py1 - h - 12 * self.s), lab,
                               self.f["small"], self.col["text"], 1, anchor="ms")
        for i, lab in enumerate(labels[:n]):
            text_alpha(img, (px0 + (i + 0.5) * group_w, py1 + 15 * self.s), lab[:8],
                       self.f["small"], self.col["muted"], ease_out((prog - 0.1) / 0.3), anchor="ma")

    def vis_compare(self, img, v, prog, t):
        x0, y0, x1, y1 = self.content_area()
        if v.get("title"):
            text_alpha(img, ((x0 + x1) / 2, y0), str(v["title"]), self.f["h2"],
                       self.col["accent"], ease_out(prog / 0.2), anchor="ma")
            y0 += 95 * self.s
        w = (x1 - x0 - 40 * self.s) / 2
        for side, panel, color in (
            (0, v.get("left") or {}, self.col["positive"]),
            (1, v.get("right") or {}, self.col["negative"]),
        ):
            a = ease_out((prog - 0.1 - side * 0.15) / 0.3)
            if a <= 0:
                continue
            shift = (1 - a) * 60 * self.s * (-1 if side == 0 else 1)
            bx0 = x0 + side * (w + 40 * self.s) + shift
            ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(ov)
            od.rounded_rectangle([bx0, y0, bx0 + w, y1], radius=int(16 * self.s),
                                 fill=(255, 255, 255, int(18 * a)),
                                 outline=(*color, int(220 * a)), width=max(2, int(3 * self.s)))
            od.text((bx0 + w / 2, y0 + 45 * self.s), str(panel.get("title", ""))[:10],
                    font=self.f["h2"], fill=(*color, int(255 * a)), anchor="mm")
            for i, item in enumerate([str(x) for x in panel.get("items", [])][:4]):
                for j, ln in enumerate(wrap_text(self.f["small"], item, w - 60 * self.s)[:2]):
                    od.text((bx0 + 30 * self.s, y0 + (100 + i * 78 + j * 36) * self.s), ln,
                            font=self.f["small"], fill=(*self.col["text"], int(255 * a)))
            img.alpha_composite(ov)

    # --- フレーム合成・エンコード

    def frame(self, t: float) -> Image.Image:
        tm = self.timing
        img = self.background(t)
        if t < tm["intro"]:
            self.draw_intro(img, t)
        elif t >= tm["total"] - tm["outro"]:
            self.draw_outro(img, t - (tm["total"] - tm["outro"]))
        else:
            i = bisect.bisect_right(self.cue_starts, t) - 1
            cue = tm["cues"][clamp(i, 0, len(tm["cues"]) - 1)]
            dur = max(0.5, cue["end"] - cue["start"])
            prog = clamp((t - cue["start"]) / dur, 0.0, 1.0)
            self.header(img, cue["heading"])
            self.draw_visual(img, cue["visual"], prog, t)
            self.caption_bar(img, cue["caption"], prog)
            self.character(img, t)
        self.progress_bar(img, t)
        return img

    def render(self, preview_sec: float | None = None):
        total = self.timing["total"] if preview_sec is None else min(preview_sec, self.timing["total"])
        n_frames = int(total * self.fps)
        out = self.outdir / ("preview.mp4" if preview_sec else "video.mp4")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{self.W}x{self.H}",
               "-r", str(self.fps), "-i", "-",
               "-i", str(self.outdir / "narration.wav"),
               "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-shortest", str(out)]
        print(f"[render] {self.W}x{self.H}@{self.fps}fps {total:.0f}秒 ({n_frames}フレーム) → {out.name}")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        assert proc.stdin
        step = max(1, n_frames // 10)
        for i in range(n_frames):
            img = self.frame(i / self.fps).convert("RGB")
            proc.stdin.write(img.tobytes())
            if i % step == 0:
                print(f"[render] {i * 100 // n_frames}%", flush=True)
        proc.stdin.close()
        if proc.wait() != 0:
            raise SystemExit("ffmpegエンコード失敗")
        print(f"[render] OK: {out}")
        return out


# ---------------------------------------------------------------- サムネイル

def cmd_thumbnail(cfg: dict, outdir: Path):
    script = json.loads((outdir / "script.json").read_text(encoding="utf-8"))
    W, H = 1280, 720
    r = Renderer.__new__(Renderer)  # 背景生成だけ借りる
    r.cfg, r.outdir, r.s, r.W, r.H = cfg, outdir, 1.0, W, H
    r.col = {k: hex_rgb(v) for k, v in cfg["theme"].items()
             if isinstance(v, str) and v.startswith("#")}
    img = Renderer._make_gradient(r)
    d = ImageDraw.Draw(img)
    font_path = find_font(cfg)
    f_title = ImageFont.truetype(font_path, 110)
    f_small = ImageFont.truetype(font_path, 44)
    d.rectangle([0, H - 14, W, H], fill=r.col["accent"])
    lines = wrap_text(f_title, script["title"], W * 0.88)[:3]
    y = H / 2 - len(lines) * 65
    for ln in lines:
        # 縁取りで視認性を上げる
        for dx, dy in ((-4, 0), (4, 0), (0, -4), (0, 4), (3, 3), (-3, -3)):
            d.text((W / 2 + dx, y + dy), ln, font=f_title, fill=(0, 0, 0), anchor="mm")
        d.text((W / 2, y), ln, font=f_title, fill=(255, 255, 255), anchor="mm")
        y += 130
    d.rounded_rectangle([40, 40, 90 + f_small.getlength(cfg["channel"]["name"]), 110],
                        radius=14, fill=r.col["accent"])
    d.text((65, 75), cfg["channel"]["name"], font=f_small, fill=(20, 20, 20), anchor="lm")
    out = outdir / "thumbnail.png"
    img.convert("RGB").save(out)
    print(f"[thumbnail] OK: {out}")


# ---------------------------------------------------------------- デモ台本

DEMO_SCRIPT = {
    "title": "複利のちから 10年でお金はどう増える？",
    "description": "複利のしくみを図解でやさしく解説します。",
    "tags": ["金融", "複利", "資産形成", "解説"],
    "topic": "デモ",
    "sections": [
        {
            "heading": "複利とは",
            "cues": [
                {"text": "みなさんは、複利という言葉を聞いたことがありますか。",
                 "caption": "「複利」って知っていますか？",
                 "visual": {"type": "keyword", "main": "複利", "sub": "利息が利息を生むしくみ"}},
                {"text": "複利とは、ついた利息にもさらに利息がつく増え方のことです。",
                 "caption": "利息にも利息がつく増え方",
                 "visual": {"type": "list", "title": "ポイント",
                            "items": ["元本に利息がつく", "利息にも利息がつく", "時間が長いほど有利"]}},
            ],
        },
        {
            "heading": "単利との違い",
            "cues": [
                {"text": "毎年5パーセントで運用した場合、単利と複利では差がどんどん開いていきます。",
                 "caption": "年5%で運用したときの差",
                 "visual": {"type": "chart_line", "title": "100万円を年5%で運用（万円）",
                            "labels": ["0年", "5年", "10年", "15年", "20年"],
                            "series": [{"name": "複利", "values": [100, 128, 163, 208, 265]},
                                       {"name": "単利", "values": [100, 125, 150, 175, 200]}]}},
                {"text": "20年後には、複利と単利でおよそ65万円もの差が生まれる計算です。",
                 "caption": "20年で約65万円の差に",
                 "visual": {"type": "chart_bar", "title": "20年後の金額（万円）",
                            "labels": ["複利", "単利"],
                            "series": [{"name": "20年後", "values": [265, 200]}]}},
                {"text": "早く始めるか、後で始めるか。向き不向きを整理してみましょう。",
                 "caption": "早く始める？後で始める？",
                 "visual": {"type": "compare", "title": "スタート時期の違い",
                            "left": {"title": "早く始める", "items": ["時間を味方にできる", "少額でもOK"]},
                            "right": {"title": "後で始める", "items": ["複利効果が小さい", "毎月の負担が大きい"]}}},
            ],
        },
        {
            "heading": "まとめ",
            "cues": [
                {"text": "複利は時間を味方につけるしくみです。焦らず、長い目で考えることが大切です。",
                 "caption": "複利は時間を味方につけるしくみ",
                 "visual": {"type": "keyword", "main": "時間×複利", "sub": "長い目で考えよう"}},
            ],
        },
    ],
}


# ---------------------------------------------------------------- CLI

def resolve_outdir(arg: str) -> Path:
    p = Path(arg)
    if not p.is_absolute():
        p = VIDEOS_DIR / arg if not p.exists() else p
    if not (p / "script.json").exists():
        raise SystemExit(f"script.json が見つかりません: {p}")
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="サンプル台本で全工程をテスト（無音・低解像度）")

    p = sub.add_parser("script", help="Geminiで台本生成")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--topic")
    g.add_argument("--next", action="store_true")

    p = sub.add_parser("tts", help="音声合成とタイミング生成")
    p.add_argument("dir")
    p.add_argument("--engine", choices=["sbv2", "none"])

    p = sub.add_parser("render", help="動画レンダリング")
    p.add_argument("dir")
    p.add_argument("--fps", type=int)
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--preview", type=float, help="先頭N秒だけ書き出す")

    p = sub.add_parser("thumbnail", help="サムネイル生成")
    p.add_argument("dir")

    p = sub.add_parser("all", help="台本→音声→動画→サムネを一括実行")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--topic")
    g.add_argument("--next", action="store_true")
    p.add_argument("--engine", choices=["sbv2", "none"])
    p.add_argument("--fps", type=int)
    p.add_argument("--scale", type=float, default=1.0)

    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    fps_default = cfg["video"]["fps"]

    if args.cmd == "demo":
        outdir = VIDEOS_DIR / "demo"
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "script.json").write_text(
            json.dumps(DEMO_SCRIPT, ensure_ascii=False, indent=2), encoding="utf-8")
        cmd_tts(cfg, outdir, engine="none")
        Renderer(cfg, outdir, fps=12, scale=0.5).render()
        cmd_thumbnail(cfg, outdir)
        print("\n[demo] 完了。output/videos/demo/video.mp4 を再生して確認してください。")
        print("[demo] 音声つき本番は docs/06 のStep3以降を参照。")
    elif args.cmd == "script":
        cmd_script(cfg, args.topic or next_topic(cfg))
    elif args.cmd == "tts":
        cmd_tts(cfg, resolve_outdir(args.dir), args.engine)
    elif args.cmd == "render":
        Renderer(cfg, resolve_outdir(args.dir),
                 fps=args.fps or fps_default, scale=args.scale).render(args.preview)
    elif args.cmd == "thumbnail":
        cmd_thumbnail(cfg, resolve_outdir(args.dir))
    elif args.cmd == "all":
        outdir = cmd_script(cfg, args.topic or next_topic(cfg))
        cmd_tts(cfg, outdir, args.engine)
        Renderer(cfg, outdir, fps=args.fps or fps_default, scale=args.scale).render()
        cmd_thumbnail(cfg, outdir)
        print(f"\n[all] 完了: {outdir}")
        print(f"[all] 投稿: python scripts/upload_youtube.py {outdir}")


if __name__ == "__main__":
    main()
