#!/usr/bin/env python3
"""録画をニュース番組風に自動編集する: ジェットカット + テロップ + BGM。

ffmpegのsilencedetectで無音区間を検出してジェットカット(無音飛ばし)し、
faster-whisper(ローカル・無料)の文字起こしからニュース風テロップ
(下部の帯 + 白抜き太字)をASS字幕として生成、カットと焼き込みと
BGMミックスを1回のエンコードで行う。全工程無料・GPU任意。

文字起こしは作業フォルダに telop.srt として保存されるため、
誤認識をテキストエディタで直してから render をやり直せば反映される。

usage:
  python scripts/auto_edit.py run 入力.mp4                 # 全自動 → 入力_edited.mp4
  python scripts/auto_edit.py run 入力.mp4 --bgm 曲.mp3    # BGM付き
  python scripts/auto_edit.py analyze 入力.mp4             # カット計画の確認だけ
  python scripts/auto_edit.py transcribe 入力.mp4          # 文字起こしだけ (SRT)
  python scripts/auto_edit.py render 入力.mp4              # 保存済み計画/SRTで再レンダー
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "configs" / "auto_edit.yaml"

SILENCE_RE = re.compile(r"silence_(start|end):\s*(-?[0-9.]+(?:[eE][+-]?\d+)?)")
SRT_TIME_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


@dataclass
class TelopEvent:
    start: float
    end: float
    text: str


# ---------------------------------------------------------------- 純ロジック
# (ffmpeg/whisper不要。tests/run_autoedit_selftest.py で検証)

def parse_silencedetect(log_text: str, duration: float) -> list:
    """ffmpeg silencedetect のログから無音区間 [(start, end), ...] を作る。

    末尾が無音のまま終わるとsilence_endが出ないため、その場合はdurationで閉じる。
    """
    silences, start = [], None
    for kind, value in SILENCE_RE.findall(log_text):
        t = max(0.0, min(float(value), duration))
        if kind == "start":
            start = t
        elif start is not None:
            if t > start:
                silences.append((start, t))
            start = None
    if start is not None and duration > start:
        silences.append((start, duration))
    return silences


def build_keep_segments(silences: list, duration: float, pad: float,
                        join_gap: float, min_keep: float) -> list:
    """無音区間の補集合(=残す区間)を組み立てる。

    前後にpad秒の余白を付け、join_gap以下で隣接する区間は結合、
    min_keep未満の区間はノイズとして捨てる。
    """
    keeps, cursor = [], 0.0
    for s, e in sorted(silences):
        s, e = max(0.0, s), min(duration, e)
        if s > cursor:
            keeps.append((cursor, s))
        cursor = max(cursor, e)
    if duration > cursor:
        keeps.append((cursor, duration))

    padded = [(max(0.0, s - pad), min(duration, e + pad)) for s, e in keeps]
    merged = []
    for s, e in padded:
        if merged and s - merged[-1][1] <= join_gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= min_keep]


def output_duration(keeps: list) -> float:
    return sum(e - s for s, e in keeps)


def remap_time(t: float, keeps: list) -> float:
    """元動画の時刻をカット後タイムラインの時刻へ写す。

    カットされた区間内の時刻は直後のつなぎ目に落ちる(出力は連続なので
    前区間の終わり=次区間の始まり。字幕がカットをまたいでも自然に縮む)。
    """
    acc = 0.0
    for s, e in keeps:
        if t < s:
            return acc
        if t <= e:
            return acc + (t - s)
        acc += e - s
    return acc


def remap_events(events: list, keeps: list, min_duration: float) -> list:
    """テロップ群を出力タイムラインへ写し、最低表示時間と重なりを整える。"""
    out = []
    for ev in sorted(events, key=lambda ev: ev.start):
        ns, ne = remap_time(ev.start, keeps), remap_time(ev.end, keeps)
        if ne - ns < 0.05:  # 丸ごとカットされた字幕は捨てる
            continue
        out.append(TelopEvent(ns, ne, ev.text))
    total = output_duration(keeps)
    for i, ev in enumerate(out):
        limit = out[i + 1].start if i + 1 < len(out) else total
        want = max(ev.end, ev.start + min_duration)
        if limit > ev.start:
            ev.end = min(want, limit)
    return out


def display_width(text: str) -> float:
    """全角=1, 半角=0.5 で表示幅を数える。"""
    return sum(1.0 if unicodedata.east_asian_width(c) in "FWA" else 0.5
               for c in text)


KINSOKU_HEAD = "、。！？!?…,)）」』"  # 行頭に置かない文字(簡易禁則)


def wrap_lines(text: str, max_chars: float) -> list:
    """句読点・スペースを優先しつつ最大幅で改行する。"""
    text = re.sub(r"\s+", " ", text.strip())
    tokens = [t for t in re.split(r"(?<=[、。！？!?…,]) *| +", text) if t]
    lines, cur = [], ""
    for tok in tokens:
        if cur and display_width(cur + tok) > max_chars:
            lines.append(cur)
            cur = ""
        cur += tok
        while display_width(cur) > max_chars:  # 句読点なしの長文は強制改行
            w = display_width(cur)
            cut = len(cur)
            while cut > 1 and display_width(cur[:cut]) > max_chars:
                cut -= 1
            rest = cur[cut:]
            if rest and all(c in KINSOKU_HEAD for c in rest):
                break  # はみ出しが句読点だけなら、ぶら下げて1行に収める
            if w <= max_chars * 2 and display_width(rest) < max_chars * 0.4:
                # 2行目が極端に短くなるときは全体を半々に割る(見た目優先)
                cut = len(cur)
                while cut > 1 and display_width(cur[:cut]) > w / 2:
                    cut -= 1
            while cut < len(cur) and cur[cut] in KINSOKU_HEAD:  # 行頭禁則
                cut += 1
            if cut >= len(cur):
                break
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        lines.append(cur)
    return lines


def split_telop(event: TelopEvent, max_chars: float, max_lines: int,
                strip_period: bool = True) -> list:
    """1つの文字起こし区間を「max_lines行までのテロップ」列に分割する。

    行数が超える場合は文字数比で表示時間を按分した複数イベントにする。
    """
    lines = wrap_lines(event.text, max_chars)
    if not lines:
        return []
    chunks = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
    weights = [sum(display_width(l) for l in chunk) or 1.0 for chunk in chunks]
    total_w, span = sum(weights), event.end - event.start
    out, t = [], event.start
    for chunk, w in zip(chunks, weights):
        text = "\\N".join(chunk)
        if strip_period:
            text = re.sub(r"[。]+$", "", text)
        end = t + span * (w / total_w)
        out.append(TelopEvent(t, end, text))
        t = end
    out[-1].end = event.end  # 丸め誤差で縮まないように
    return out


def parse_srt(text: str) -> list:
    """SRT字幕を読み込む(番号行は任意。改行はスペースに畳む)。"""
    events = []
    for block in re.split(r"\n\s*\n", text.strip().replace("\r\n", "\n")):
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        m = SRT_TIME_RE.search(lines[0]) or (
            SRT_TIME_RE.search(lines[1]) if len(lines) > 1 else None)
        if not m:
            continue
        time_idx = 0 if SRT_TIME_RE.search(lines[0]) else 1
        h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
        start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1.ljust(3, "0")) / 1000
        end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2.ljust(3, "0")) / 1000
        body = " ".join(lines[time_idx + 1:]).strip()
        if body and end > start:
            events.append(TelopEvent(start, end, body))
    return events


def format_srt(events: list) -> str:
    def stamp(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    blocks = [f"{i}\n{stamp(ev.start)} --> {stamp(ev.end)}\n{ev.text}"
              for i, ev in enumerate(events, 1)]
    return "\n\n".join(blocks) + "\n"


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_color(rgb_hex: str, opacity: float = 1.0) -> str:
    """"RRGGBB" と不透明度(0〜1)をASSの &HAABBGGRR 形式へ。"""
    rgb = rgb_hex.lstrip("#")
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    alpha = max(0, min(255, int(round((1.0 - opacity) * 255))))
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", "\\N")


def build_ass(events: list, cfg: dict, width: int, height: int,
              total: float, title_text: str = "") -> str:
    """ニュース風スタイルのASS字幕を組み立てる。

    レイヤー0=帯(座布団), 1=アクセントライン, 2=本文。テロップ表示中だけ
    帯が出る。タイトルはBorderStyle=3(文字に沿う不透明ボックス)で全編表示。
    """
    st, tl = cfg["style"], cfg["title"]
    scale = height / 1080.0
    fs = max(8, round(st["font_size"] * scale))
    outline = round(st["outline_width"] * scale, 1)
    max_lines = cfg["telop"]["max_lines"]
    line_h = fs * 1.3
    pad_inner = 20 * scale
    band_h = max_lines * line_h + 2 * pad_inner
    bottom = st["bottom_margin"] * scale
    y1 = height - bottom
    y0 = y1 - band_h
    accent_h = st["accent_height"] * scale
    title_fs = max(8, round(tl["font_size"] * scale))

    header = f"""[Script Info]
; auto_edit.py generated (news-style telop)
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Telop,{st['font']},{fs},{ass_color(st['text_color'])},{ass_color(st['text_color'])},{ass_color(st['outline_color'])},&H96000000,-1,0,0,0,100,100,0,0,1,{outline},0,5,0,0,0,1
Style: Band,{st['font']},{fs},{ass_color(st['band_color'], st['band_opacity'])},{ass_color(st['band_color'], st['band_opacity'])},{ass_color(st['band_color'], st['band_opacity'])},&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Accent,{st['font']},{fs},{ass_color(st['accent_color'])},{ass_color(st['accent_color'])},{ass_color(st['accent_color'])},&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Title,{st['font']},{title_fs},{ass_color(tl['text_color'])},{ass_color(tl['text_color'])},{ass_color(tl['bg_color'], tl['bg_opacity'])},{ass_color(tl['bg_color'], tl['bg_opacity'])},-1,0,0,0,100,100,0,0,3,{round(12 * scale, 1)},0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header.rstrip("\n")]

    if title_text:
        pos = f"\\pos({round(24 * scale)},{round(24 * scale)})"
        lines.append(f"Dialogue: 3,{ass_time(0)},{ass_time(total)},Title,,0,0,0,,"
                     f"{{\\an7{pos}}}{ass_escape(title_text)}")

    band = (f"m 0 {y0:.0f} l {width} {y0:.0f} {width} {y1:.0f} 0 {y1:.0f}")
    accent = (f"m 0 {y0 - accent_h:.0f} l {width} {y0 - accent_h:.0f} "
              f"{width} {y0:.0f} 0 {y0:.0f}")
    cy = (y0 + y1) / 2
    for ev in events:
        s, e = ass_time(ev.start), ass_time(ev.end)
        if cfg["style"]["band_enabled"]:
            lines.append(f"Dialogue: 0,{s},{e},Band,,0,0,0,,"
                         f"{{\\p1\\pos(0,0)}}{band}{{\\p0}}")
            if accent_h >= 1:
                lines.append(f"Dialogue: 1,{s},{e},Accent,,0,0,0,,"
                             f"{{\\p1\\pos(0,0)}}{accent}{{\\p0}}")
        lines.append(f"Dialogue: 2,{s},{e},Telop,,0,0,0,,"
                     f"{{\\an5\\pos({width / 2:.0f},{cy:.0f})}}{ass_escape(ev.text)}")
    return "\n".join(lines) + "\n"


def build_filter_script(keeps: list, has_ass: bool, ass_name: str,
                        bgm: dict, total: float) -> str:
    """カット→連結→字幕焼き込み→BGMミックスのfilter_complexを組み立てる。

    入力0=本編動画, 入力1=BGM(あれば)。出力ラベルは常に [vout]/[aout]。
    """
    parts, pairs = [], []
    for i, (s, e) in enumerate(keeps):
        parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        pairs.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(pairs)}concat=n={len(keeps)}:v=1:a=1[vc][ac]")

    if has_ass:
        parts.append(f"[vc]subtitles={ass_name}[vout]")
    else:
        parts.append("[vc]null[vout]")

    if bgm:
        fade = bgm["fade"]
        chain = (f"[1:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                 f"volume={bgm['volume_db']}dB")
        if fade > 0:
            chain += (f",afade=t=in:st=0:d={fade}"
                      f",afade=t=out:st={max(0.0, total - fade):.3f}:d={fade}")
        parts.append(chain + "[bgm]")
        if bgm["ducking"]:
            parts.append("[ac]asplit=2[acm][acs]")
            parts.append("[bgm][acs]sidechaincompress="
                         "threshold=0.05:ratio=8:attack=20:release=500[duck]")
            parts.append("[acm][duck]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            parts.append("[ac][bgm]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        parts.append("[ac]anull[aout]")
    return ";\n".join(parts) + "\n"


# ---------------------------------------------------------------- 外部ツール連携

def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def need_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"[error] {name} が見つかりません。Windowsなら PowerShell で\n"
                 f"  winget install Gyan.FFmpeg\n"
                 f"を実行後、ターミナルを開き直してください(無料)。")
    return path


def probe(input_path: Path) -> dict:
    """動画の長さ・解像度・音声の有無を取得する。"""
    out = subprocess.run(
        [need_tool("ffprobe"), "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height",
         "-of", "json", str(input_path)],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    width = height = 0
    has_audio = False
    for st in info.get("streams", []):
        if st.get("codec_type") == "video" and not width:
            width, height = int(st.get("width", 0)), int(st.get("height", 0))
        if st.get("codec_type") == "audio":
            has_audio = True
    return {"duration": float(info["format"]["duration"]),
            "width": width, "height": height, "has_audio": has_audio}


def detect_silences(input_path: Path, cfg: dict, duration: float) -> list:
    jc = cfg["jetcut"]
    proc = subprocess.run(
        [need_tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(input_path),
         "-vn", "-af",
         f"silencedetect=noise={jc['silence_threshold_db']}dB:d={jc['min_silence']}",
         "-f", "null", "-"],
        capture_output=True, text=True)
    return parse_silencedetect(proc.stderr, duration)


def analyze(input_path: Path, cfg: dict) -> dict:
    info = probe(input_path)
    if not info["has_audio"]:
        sys.exit("[error] 音声トラックがありません。ジェットカットは音声を基準にします。")
    jc = cfg["jetcut"]
    silences = detect_silences(input_path, cfg, info["duration"])
    keeps = build_keep_segments(silences, info["duration"], jc["keep_padding"],
                                jc["join_gap"], jc["min_keep"])
    if not keeps:
        sys.exit("[error] 全編が無音判定になりました。configs/auto_edit.yaml の "
                 "silence_threshold_db を下げて(例: -45)再実行してください。")
    return {"input": str(input_path), "duration": info["duration"],
            "width": info["width"], "height": info["height"],
            "keep_segments": [[round(s, 3), round(e, 3)] for s, e in keeps],
            "params": dict(jc)}


def transcribe(input_path: Path, cfg: dict) -> list:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("[error] faster-whisper が入っていません(無料)。\n"
                 "  pip install faster-whisper\n"
                 "を実行してください。文字起こしを飛ばすなら --no-telop、"
                 "手持ちの字幕を使うなら --srt ファイル.srt を指定します。")
    tc = cfg["telop"]
    attempts = [tc["device"]] + (["cpu"] if tc["device"] != "cpu" else [])
    last_err = None
    for device in attempts:
        try:
            model = WhisperModel(tc["model"], device=device,
                                 compute_type=tc["compute_type"])
            segments, _info = model.transcribe(
                str(input_path), language=tc["language"], vad_filter=True)
            events = [TelopEvent(seg.start, seg.end, seg.text.strip())
                      for seg in segments if seg.text.strip()]
            return events
        except Exception as e:  # CUDA/cuDNN不備はここで拾ってCPUに切り替える
            last_err = e
            if device != "cpu":
                print(f"[warn] device={device} で失敗、CPUで再試行します: {e}",
                      file=sys.stderr)
    sys.exit(f"[error] 文字起こしに失敗しました: {last_err}")


def make_telop_events(raw_events: list, plan: dict, cfg: dict) -> list:
    tc = cfg["telop"]
    keeps = [tuple(seg) for seg in plan["keep_segments"]]
    split = []
    for ev in raw_events:
        split.extend(split_telop(ev, tc["max_chars_per_line"], tc["max_lines"],
                                 tc["strip_trailing_period"]))
    return remap_events(split, keeps, tc["min_duration"])


def encoder_args(cfg: dict, use_nvenc: bool) -> list:
    enc = cfg["encode"]
    if use_nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23"]
    return ["-c:v", "libx264", "-crf", str(enc["crf"]), "-preset", enc["preset"]]


def render(input_path: Path, plan: dict, events: list, cfg: dict,
           workdir: Path, out_path: Path, bgm_path: Path = None,
           title_text: str = "") -> None:
    keeps = [tuple(seg) for seg in plan["keep_segments"]]
    total = output_duration(keeps)
    ass_name = None
    if events or title_text:
        ass_name = "telop.ass"
        (workdir / ass_name).write_text(
            build_ass(events, cfg, plan["width"], plan["height"], total, title_text),
            encoding="utf-8")

    bgm_cfg = None
    if bgm_path:
        bgm_cfg = {"volume_db": cfg["bgm"]["volume_db"], "fade": cfg["bgm"]["fade"],
                   "ducking": cfg["bgm"]["ducking"]}
    (workdir / "filter.txt").write_text(
        build_filter_script(keeps, ass_name is not None, ass_name or "", bgm_cfg, total),
        encoding="utf-8")

    ffmpeg = need_tool("ffmpeg")
    base = [ffmpeg, "-y", "-hide_banner", "-i", str(input_path.resolve())]
    if bgm_path:
        if cfg["bgm"]["loop"]:
            base += ["-stream_loop", "-1"]
        base += ["-i", str(bgm_path.resolve())]
    base += ["-filter_complex_script", "filter.txt",
             "-map", "[vout]", "-map", "[aout]",
             "-c:a", "aac", "-b:a", cfg["encode"]["audio_bitrate"],
             "-pix_fmt", "yuv420p", "-movflags", "+faststart"]

    codec = cfg["encode"]["video_codec"]
    tries = (["h264_nvenc", "libx264"] if codec == "auto"
             else [codec])
    for i, c in enumerate(tries):
        cmd = base + encoder_args(cfg, c == "h264_nvenc") + [str(out_path.resolve())]
        # 字幕フィルタのパス指定はcwd相対が最も安全(Windowsのドライブ文字対策)
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
        if proc.returncode == 0:
            return
        if i + 1 < len(tries):
            print(f"[warn] {c} でのエンコードに失敗、{tries[i + 1]} で再試行します",
                  file=sys.stderr)
    tail = "\n".join(proc.stderr.splitlines()[-15:])
    sys.exit(f"[error] ffmpegが失敗しました:\n{tail}")


# ---------------------------------------------------------------- サブコマンド

def default_workdir(input_path: Path) -> Path:
    wd = input_path.parent / f"{input_path.stem}_autoedit"
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def print_plan_summary(plan: dict) -> None:
    keeps = plan["keep_segments"]
    total = output_duration([tuple(k) for k in keeps])
    cut = plan["duration"] - total
    print(f"  元の長さ : {plan['duration']:.1f}秒")
    print(f"  編集後   : {total:.1f}秒 ({len(keeps)}区間)")
    print(f"  カット   : {cut:.1f}秒 ({cut / plan['duration'] * 100:.0f}%削減)")


def cmd_analyze(args) -> int:
    cfg = load_config(Path(args.config))
    input_path = Path(args.input)
    plan = analyze(input_path, cfg)
    wd = default_workdir(input_path)
    (wd / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print("カット計画を保存しました:", wd / "plan.json")
    print_plan_summary(plan)
    return 0


def cmd_transcribe(args) -> int:
    cfg = load_config(Path(args.config))
    input_path = Path(args.input)
    events = transcribe(input_path, cfg)
    wd = default_workdir(input_path)
    (wd / "telop.srt").write_text(format_srt(events), encoding="utf-8")
    print(f"文字起こしを保存しました({len(events)}区間):", wd / "telop.srt")
    print("誤認識はこのSRTを直してから render を実行すると反映されます。")
    return 0


def _load_raw_events(args, wd: Path):
    """--srt指定 > 作業フォルダのtelop.srt の順で字幕を読み込む。無ければNone。"""
    if args.srt:
        srt = Path(args.srt)
        if not srt.exists():
            sys.exit(f"[error] SRTファイルが見つかりません: {srt}")
        return parse_srt(srt.read_text(encoding="utf-8"))
    srt = wd / "telop.srt"
    if srt.exists():
        return parse_srt(srt.read_text(encoding="utf-8"))
    return None


def _resolve_bgm(args, cfg) -> Path:
    raw = args.bgm or cfg["bgm"]["file"]
    if not raw:
        return None
    bgm = Path(raw)
    if not bgm.exists():
        sys.exit(f"[error] BGMファイルが見つかりません: {bgm}")
    return bgm


def cmd_render(args) -> int:
    cfg = load_config(Path(args.config))
    input_path = Path(args.input)
    wd = default_workdir(input_path)
    plan_path = Path(args.plan) if args.plan else wd / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        print("カット計画が無いので analyze から実行します…")
        plan = analyze(input_path, cfg)
        (wd / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    events = []
    if not args.no_telop:
        raw = _load_raw_events(args, wd)
        if raw is None:
            sys.exit("[error] 字幕がありません。先に transcribe を実行するか、"
                     "--srt ファイル.srt か --no-telop を指定してください。")
        events = make_telop_events(raw, plan, cfg)

    out = Path(args.output) if args.output else \
        input_path.with_name(f"{input_path.stem}_edited.mp4")
    title = args.title if args.title is not None else cfg["title"]["text"]
    render(input_path, plan, events, cfg, wd, out,
           bgm_path=_resolve_bgm(args, cfg), title_text=title)
    print("書き出し完了:", out)
    print_plan_summary(plan)
    return 0


def cmd_run(args) -> int:
    cfg = load_config(Path(args.config))
    input_path = Path(args.input)
    wd = default_workdir(input_path)

    print("1/3 無音検出(ジェットカット計画)…")
    plan = analyze(input_path, cfg)
    (wd / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print_plan_summary(plan)

    events = []
    if not args.no_telop:
        if args.srt:
            print("2/3 字幕読み込み(--srt指定)…")
            srt = Path(args.srt)
            if not srt.exists():
                sys.exit(f"[error] SRTファイルが見つかりません: {srt}")
            raw = parse_srt(srt.read_text(encoding="utf-8"))
        else:
            print("2/3 文字起こし(初回はモデルDLで数分かかります)…")
            raw = transcribe(input_path, cfg)
            (wd / "telop.srt").write_text(format_srt(raw), encoding="utf-8")
            print(f"  → {wd / 'telop.srt'} に保存(誤認識はここを直して render)")
        events = make_telop_events(raw, plan, cfg)
    else:
        print("2/3 テロップなし(--no-telop)")

    print("3/3 レンダリング…")
    out = Path(args.output) if args.output else \
        input_path.with_name(f"{input_path.stem}_edited.mp4")
    title = args.title if args.title is not None else cfg["title"]["text"]
    render(input_path, plan, events, cfg, wd, out,
           bgm_path=_resolve_bgm(args, cfg), title_text=title)
    print("書き出し完了:", out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, telop_opts=True):
        p.add_argument("input", help="入力動画 (mp4/mkv/mov等)")
        p.add_argument("--config", default=str(DEFAULT_CONFIG))
        if telop_opts:
            p.add_argument("-o", "--output", help="出力先 (省略時: 入力_edited.mp4)")
            p.add_argument("--no-telop", action="store_true", help="テロップなし")
            p.add_argument("--srt", help="文字起こしの代わりに使うSRT字幕")
            p.add_argument("--bgm", help="BGMファイル (設定のbgm.fileより優先)")
            p.add_argument("--title", help="左上の番組名バー (設定のtitle.textより優先)")

    p = sub.add_parser("run", help="全自動: 解析→文字起こし→レンダリング")
    common(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("analyze", help="無音検出してカット計画だけ作る")
    common(p, telop_opts=False)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("transcribe", help="文字起こしだけ行いSRT保存")
    common(p, telop_opts=False)
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("render", help="保存済みの計画/SRTからレンダリング")
    common(p)
    p.add_argument("--plan", help="カット計画JSON (省略時: 作業フォルダのplan.json)")
    p.set_defaults(func=cmd_render)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
