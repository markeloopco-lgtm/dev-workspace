#!/usr/bin/env python3
"""台本YAML + 音声WAV から YouTubeショート用の縦型MP4を書き出す。

Style-Bert-VITS2で作った台詞WAVと立ち絵PNG(口閉じ/口開き)を組み合わせ、
字幕を焼き込んだ 1080x1920 の動画を作る。GPU不要・ffmpegのみ必要。

口パクは音声のRMS(音の大きさ)から自動生成する。
同じ絵になるフレームは1枚しか描かないので、長い動画でも描画は速い。

usage:
  python scripts/make_short.py init myshort/          # 台本テンプレ一式を作る
  python scripts/make_short.py build myshort/script.yaml -o out.mp4
  python scripts/make_short.py build script.yaml --probe   # 描画せず構成だけ確認
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

    out = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        while para:
            take = min(len(para), limit)
            while take > 1 and too_wide(para[:take]):
                take -= 1
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


def render_frame(base: Image.Image, char: Image.Image, cfg: dict,
                 font: ImageFont.FreeTypeFont, text: str) -> Image.Image:
    """背景 + 立ち絵 + 字幕 を合成した1枚を返す。"""
    w, h = cfg["size"]
    frame = base.copy()

    if char is not None:
        ch = cfg["character"]
        y = h - int(h * ch["bottom_margin_ratio"]) - char.height
        if ch["align"] == "left":
            x = int(w * 0.05)
        elif ch["align"] == "right":
            x = w - char.width - int(w * 0.05)
        else:
            x = (w - char.width) // 2
        frame.paste(char, (x, y), char)

    st = cfg["subtitle"]
    draw = ImageDraw.Draw(frame)
    font_px = max(1, int(h * st["size_ratio"]))
    max_px = w * st["max_width_ratio"]
    lines = wrap_text(text, st["max_chars_per_line"], font, max_px)
    spacing = int(font_px * st["line_spacing_ratio"])
    stroke = max(1, int(h * st["stroke_width_ratio"]))
    total_h = len(lines) * font_px + (len(lines) - 1) * spacing
    y = h - int(h * st["bottom_ratio"]) - total_h
    for line in lines:
        tw = draw.textlength(line, font=font)
        draw.text(
            ((w - tw) / 2, y), line, font=font, fill=st["color"],
            stroke_width=stroke, stroke_fill=st["stroke"],
        )
        y += font_px + spacing
    return frame


# ---------------------------------------------------------------- 台本読み込み


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

    for i, ln in enumerate(lines, 1):
        if not ln.get("audio"):
            raise ScriptError(f"{i}番目の台詞に audio (音声WAVのパス) がありません。")
        wav = resolve_path(base_dir, ln["audio"])
        if not wav.exists():
            raise ScriptError(
                f"{i}番目の音声が見つかりません: {wav}\n"
                "Style-Bert-VITS2で書き出したWAVを置いてください。"
            )
        samples, rate = read_wav_mono(wav)
        ln["_samples"], ln["_rate"] = samples, rate
        ln["_sec"] = len(samples) / rate
        ln.setdefault("text", "")
    return cfg, lines, base_dir


# ---------------------------------------------------------------- 本体


def build(args) -> int:
    script_path = Path(args.script).resolve()
    cfg, lines, base_dir = load_script(script_path)

    fps = int(cfg["fps"])
    gap = float(cfg["gap_sec"])
    total_sec = sum(ln["_sec"] for ln in lines) + gap * (len(lines) - 1)

    print(f"台詞 {len(lines)}本 / 想定尺 {total_sec:.1f}秒 / {cfg['size'][0]}x{cfg['size'][1]} {fps}fps")
    for i, ln in enumerate(lines, 1):
        preview = ln["text"].replace("\n", " ")[:24]
        print(f"  {i:2d}. {ln['_sec']:5.2f}秒  {preview}")
    if total_sec > SHORTS_MAX_SEC:
        print(f"  ⚠ {SHORTS_MAX_SEC}秒を超えています。ショートではなく通常動画として"
              "扱われる可能性があります(投稿画面で確認してください)")
    if args.probe:
        return 0

    ffmpeg = find_ffmpeg()
    font = resolve_font(cfg["subtitle"]["font"], max(1, int(cfg["size"][1] * cfg["subtitle"]["size_ratio"])))
    closed, opened = load_character(cfg["character"], base_dir, cfg["size"][1])
    base_img = make_base(cfg, base_dir)

    out_path = Path(args.output or script_path.with_suffix(".mp4")).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # 台詞ごと・口の開閉ごとに1枚だけ描く(同じ絵を使い回す)
        stills = {}
        for i, ln in enumerate(lines):
            for state in (False, True):
                img = render_frame(base_img, opened if state else closed, cfg, font, ln["text"])
                p = tmp / f"f{i:04d}_{int(state)}.png"
                img.convert("RGB").save(p)
                stills[(i, state)] = p
                if closed is None or opened is closed:
                    stills[(i, True)] = p  # 口パク無しなら1枚で足りる
                    break

        # フレーム列(同じ絵が続く区間はまとめる)を concat 用の一覧にする
        entries = []

        def push(path: Path, n: int):
            if n <= 0:
                return
            if entries and entries[-1][0] == path:
                entries[-1][1] += n
            else:
                entries.append([path, n])

        for i, ln in enumerate(lines):
            n = max(1, int(round(ln["_sec"] * fps)))
            for state in mouth_states(ln["_samples"], ln["_rate"], n, cfg["mouth"]):
                push(stills[(i, state)], 1)
            if i < len(lines) - 1:
                push(stills[(i, False)], int(round(gap * fps)))  # 間は口を閉じる

        concat = tmp / "frames.txt"
        with concat.open("w", encoding="utf-8") as fh:
            for path, n in entries:
                fh.write(f"file '{path.as_posix()}'\nduration {n / fps:.6f}\n")
            fh.write(f"file '{entries[-1][0].as_posix()}'\n")  # 最終フレームは要再掲

        voice = tmp / "voice.wav"
        build_voice_track(lines, gap, voice)

        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-i", str(voice)]
        bgm = cfg["bgm"]
        if bgm.get("file"):
            bgm_path = resolve_path(base_dir, bgm["file"])
            if not bgm_path.exists():
                raise ScriptError(f"BGMが見つかりません: {bgm_path}")
            cmd += ["-stream_loop", "-1", "-i", str(bgm_path), "-filter_complex",
                    f"[2:a]volume={bgm['gain_db']}dB[b];[1:a][b]amix=inputs=2:duration=first[a]",
                    "-map", "0:v", "-map", "[a]"]
        else:
            cmd += ["-map", "0:v", "-map", "1:a"]
        cmd += ["-r", str(fps), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-shortest", str(out_path)]

        print(f"エンコード中… ({len(entries)}区間 / 静止画 {len(set(stills.values()))}枚)")
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


def init(args) -> int:
    out_dir = Path(args.directory).resolve()
    (out_dir / "voice").mkdir(parents=True, exist_ok=True)
    (out_dir / "assets").mkdir(parents=True, exist_ok=True)
    script = out_dir / "script.yaml"
    if script.exists() and not args.force:
        raise ScriptError(f"すでにあります: {script}  (上書きするなら --force)")
    script.write_text(TEMPLATE, encoding="utf-8")
    print(f"✓ 台本テンプレを作成: {script}")
    print(f"  1) {out_dir / 'voice'} に台詞WAVを置く")
    print(f"  2) {out_dir / 'assets'} に立ち絵・背景を置く")
    print(f"  3) python scripts/make_short.py build {script} -o out.mp4")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="台本YAMLからショート動画(縦型MP4)を作る")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="台本テンプレとフォルダ一式を作る")
    p_init.add_argument("directory")
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
