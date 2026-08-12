#!/usr/bin/env python3
"""録画を自動編集する: ジェットカット + テロップ + BGM。

ffmpegのsilencedetectで無音区間を検出してジェットカット(無音飛ばし)し、
faster-whisper(ローカル・無料)の文字起こしからテロップをASS字幕として生成、
カットと焼き込みとBGMミックスを1回のエンコードで行う。全工程無料・GPU任意。

テロップの様式は preset で選ぶ (configs/auto_edit.yaml):
  business … ビジネス系YouTube (座布団なし・太い縁取り・キーワード色替え)
  talk     … 対談向け (話者ごとに座布団を色分け・詰めたカット)
  news     … ニュース番組風 (画面下の帯 + 金のライン)

文字起こしは作業フォルダに telop.srt として保存されるため、
誤認識をテキストエディタで直してから render をやり直せば反映される。

usage:
  python scripts/auto_edit.py run 入力.mp4                 # 全自動 → 入力_edited.mp4
  python scripts/auto_edit.py run 入力.mp4 --bgm 曲.mp3    # BGM付き
  python scripts/auto_edit.py preview 入力.mp4             # テロップの見た目を1枚確認
  python scripts/auto_edit.py fonts                        # 使えるフォントを調べる
  python scripts/auto_edit.py analyze 入力.mp4             # カット計画の確認だけ
  python scripts/auto_edit.py transcribe 入力.mp4          # 文字起こしだけ (SRT)
  python scripts/auto_edit.py render 入力.mp4              # 保存済み計画/SRTで再レンダー
"""

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "configs" / "auto_edit.yaml"
# ここに .ttf/.otf を置くと、PCにインストールしなくてもテロップに使える
FONTS_DIR = ROOT / "assets" / "fonts"

SILENCE_RE = re.compile(r"silence_(start|end):\s*(-?[0-9.]+(?:[eE][+-]?\d+)?)")
SRT_TIME_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


@dataclass
class TelopEvent:
    start: float
    end: float
    text: str
    speaker: str = ""
    scene: str = ""   # シーン名 (configs の scenes: に対応。空なら通常)


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


def remap_events(events: list, keeps: list, min_duration: float,
                 max_duration: float = None, reading_speed: float = None) -> list:
    """テロップ群を出力タイムラインへ写し、表示時間と重なりを整える。

    放送字幕の目安(2秒前後以上・6.5秒以下・毎秒約4文字)に寄せるが、
    次のテロップの開始は必ず追い越さない。
    """
    out = []
    for ev in sorted(events, key=lambda ev: ev.start):
        ns, ne = remap_time(ev.start, keeps), remap_time(ev.end, keeps)
        if ne - ns < 0.05:  # 丸ごとカットされた字幕は捨てる
            continue
        out.append(TelopEvent(ns, ne, ev.text, ev.speaker, ev.scene))
    total = output_duration(keeps)
    for i, ev in enumerate(out):
        limit = out[i + 1].start if i + 1 < len(out) else total
        want_len = ev.end - ev.start
        if reading_speed:  # 読み切れる長さを確保する
            want_len = max(want_len, display_width(ev.text.replace("\\N", ""))
                           / reading_speed)
        want_len = max(want_len, min_duration)
        if max_duration:
            want_len = min(want_len, max_duration)
        if limit > ev.start:
            ev.end = min(ev.start + want_len, limit)
    return out


def display_width(text: str) -> float:
    """全角=1, 半角=0.5 で表示幅を数える(強調マーカーの * は数えない)。"""
    return sum(1.0 if unicodedata.east_asian_width(c) in "FWA" else 0.5
               for c in text if c != "*")


# 行頭に置かない文字(簡易禁則)。句読点・閉じ括弧に加えて、
# 小書き仮名と長音符も行頭に来ると読みにくい(「…がき/ょう…」を防ぐ)
KINSOKU_HEAD = (
    "、。！？!?…,)）」』】〕〉》"
    "ぁぃぅぇぉっゃゅょゎゕゖ"
    "ァィゥェォッャュョヮヵヶ"
    "ーヽヾゝゞ々〜"
)
PUNCT_TAIL = "、。！？!?…,"           # ここで改行すると自然に読める文字
WORD_CHAR = re.compile(r"[0-9０-９A-Za-zＡ-Ｚａ-ｚ.%％]")  # 途中で切らない文字


def _protected_spans(text: str) -> list:
    """途中で改行したくない範囲。「1000万円」「50%」や *強調* の囲みを割らない。"""
    spans = [(m.start(), m.end()) for m in NUMBER_RE.finditer(text) if m.group()]
    return spans + [(m.start(), m.end()) for m in EMPHASIS_MARK.finditer(text)]


def _can_break(text: str, i: int, spans: list = ()) -> bool:
    """text[i] の直前で改行してよいか。"""
    if i <= 0 or i >= len(text):
        return False
    if text[i] in KINSOKU_HEAD:  # 行頭禁則
        return False
    if any(s < i < e for s, e in spans):
        return False
    # 英数字の連なりは割らない (「Live2D」が途中で切れるのを防ぐ)
    return not (WORD_CHAR.match(text[i - 1]) and WORD_CHAR.match(text[i]))


def _wrap_greedy(text: str, max_chars: float) -> list:
    """句読点・スペースの切れ目を優先しつつ、最大幅で順に折り返す。"""
    tokens = [t for t in re.split(r"(?<=[、。！？!?…,]) *| +", text) if t]
    lines, cur = [], ""
    for tok in tokens:
        if cur and display_width(cur + tok) > max_chars:
            lines.append(cur)
            cur = ""
        cur += tok
        while display_width(cur) > max_chars:  # 句読点で切れない長文は強制改行
            cut = len(cur)
            while cut > 1 and display_width(cur[:cut]) > max_chars:
                cut -= 1
            rest = cur[cut:]
            if rest and all(c in KINSOKU_HEAD for c in rest):
                break  # はみ出しが句読点だけなら、ぶら下げて1行に収める
            back = cut  # 数字や英単語の途中に落ちたら手前の切れ目まで戻す
            spans = _protected_spans(cur)
            while back > 1 and not _can_break(cur, back, spans):
                back -= 1
            if back > 1:
                cut = back
            while cut < len(cur) and cur[cut] in KINSOKU_HEAD:  # 行頭禁則
                cut += 1
            if cut >= len(cur):
                break
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        lines.append(cur)
    return lines


def _balance_two(text: str, max_chars: float) -> list:
    """2行に割る位置を全探索して選ぶ。行長が揃うほど良く、句読点の切れ目を優先。"""
    best = None
    spans = _protected_spans(text)
    for i in range(1, len(text)):
        if not _can_break(text, i, spans):
            continue
        left, right = text[:i].rstrip(), text[i:].lstrip()
        if not left or not right:
            continue
        a, b = display_width(left), display_width(right)
        if a > max_chars or b > max_chars:
            continue
        # 句読点で切れるなら多少行長がずれても優先する
        score = abs(a - b) - (max_chars if text[i - 1] in PUNCT_TAIL else 0)
        if best is None or score < best[0]:
            best = (score, [left, right])
    return best[1] if best else None


def wrap_lines(text: str, max_chars: float) -> list:
    """テロップ1枚ぶんの文字列を行に折り返す。

    2行に収まる場合は行長を揃える。放送字幕は行長を揃えるのが基本で、
    貪欲に詰めると「15文字 + 3文字」のような不格好な2行目になるため。
    """
    text = re.sub(r"\s+", " ", text.strip())
    lines = _wrap_greedy(text, max_chars)
    if len(lines) == 2:
        balanced = _balance_two(text, max_chars)
        if balanced:
            lines = balanced
    return lines


SPEAKER_RE = re.compile(r"^\s*([^\s:：]{1,12})\s*[:：]\s*(.+)$", re.DOTALL)
QUESTION_END = re.compile(r"[?？]\s*$")
EMPHASIS_MARK = re.compile(r"\*([^*]+)\*")
# ビジネス系YouTubeは数字を強調するのが定番。単位が付いていれば一緒に拾う
NUMBER_RE = re.compile(
    r"[0-9０-９]+(?:[.,．][0-9０-９]+)?"
    # 長い単位から並べる(「万円」より先に「万」を書くと "1000万" で切れる)
    r"(?:万円|億円|ヶ月|か月|カ月|時間|%|％|割|倍|円|万|億|年|日|人|件|位|分|秒)?")


def split_speaker(text: str, speakers: dict) -> tuple:
    """「名前: 発言」を (名前, 発言) に分ける。既知の話者名でなければ分けない。"""
    m = SPEAKER_RE.match(text)
    if m and speakers and m.group(1) in speakers:
        return m.group(1), m.group(2).strip()
    return "", text


def detect_scene(text: str, cfg: dict) -> tuple:
    """行頭の記号からシーンを判定し (シーン名, 記号を外した本文) を返す。

    記号が無ければ auto ルール(いまは「文末が？なら質問」)で自動判定する。
    """
    scenes = cfg.get("scenes") or {}
    for name, spec in scenes.items():
        mark = (spec or {}).get("mark")
        if mark and text.lstrip().startswith(mark):
            return name, text.lstrip()[len(mark):].lstrip()
    for name, spec in scenes.items():
        if (spec or {}).get("auto") == "question" and QUESTION_END.search(text):
            return name, text
    return "", text


def prepare_event(event: TelopEvent, cfg: dict) -> TelopEvent:
    """SRTの1行から話者(「名前:」)とシーン(行頭の記号)を取り出す。"""
    name, body = split_speaker(event.text, cfg.get("speakers") or {})
    scene, body = detect_scene(body, cfg)
    return TelopEvent(event.start, event.end, body.strip(),
                      name or event.speaker, scene or event.scene)


def _merge_spans(spans: list) -> list:
    merged = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def emphasis_runs(text: str, cfg: dict) -> list:
    """テロップ本文を [(部分文字列, 強調か), ...] に分解する。

    `*ここ*` の囲み・キーワード一覧・数字(auto_numbers)を強調扱いにする。
    改行 "\\N" は独立した要素として残す(行をまたぐ強調は分割される)。
    """
    em = cfg.get("emphasis") or {}
    runs = []
    for i, part in enumerate(text.split("\\N")):
        if i:
            runs.append(("\\N", False))
        # `*...*` を外し、外した範囲を強調スパンとして覚える
        plain, marked, idx = "", [], 0
        for m in EMPHASIS_MARK.finditer(part):
            plain += part[idx:m.start()]
            start = len(plain)
            plain += m.group(1)
            marked.append([start, len(plain)])
            idx = m.end()
        plain += part[idx:]

        if not em.get("enabled"):
            runs.append((plain, False))
            continue

        spans = list(marked)
        for kw in (em.get("keywords") or []):
            if kw:
                spans += [[m.start(), m.end()] for m in re.finditer(re.escape(kw), plain)]
        if em.get("auto_numbers"):
            spans += [[m.start(), m.end()] for m in NUMBER_RE.finditer(plain)
                      if m.group().strip()]

        cur = 0
        for s, e in _merge_spans(spans):
            if s > cur:
                runs.append((plain[cur:s], False))
            runs.append((plain[s:e], True))
            cur = e
        if cur < len(plain):
            runs.append((plain[cur:], False))
    return [(t, f) for t, f in runs if t]


def auto_max_chars(cfg: dict, width: int, height: int, size_ratio: float) -> float:
    """その文字サイズで1行に入る文字数(全角換算)を画面幅から求める。

    日本語フォントは全角がちょうど1em幅なので、
    使える幅 ÷ (文字サイズ + 字間) でほぼ正確に出せる。
    """
    st = cfg["style"]
    fs = size_ratio * height
    avail = width * (1 - 2 * st["safe_margin_ratio"])
    advance = fs * (1 + st.get("tracking_em", 0))
    return max(6.0, avail / advance)


def line_budget(cfg: dict, width: int, height: int) -> float:
    """1行に入れる文字数の上限(全角換算)。

    auto のときは「2行にしたときの文字サイズ」で計算する。こうすると、
    少しはみ出すだけの文なら文字を縮めて1行に収まり(横幅を使い切る)、
    それより長いものだけが2行になる。1行を縮めても2行のときより
    小さくはならないので、読みにくくならない。
    """
    st, tc = cfg["style"], cfg["telop"]
    configured = tc.get("max_chars_per_line")
    if not (configured is None or str(configured).lower() == "auto"):
        return float(configured)
    ratio = st.get("font_size_ratio_wrapped") or st["font_size_ratio"]
    return auto_max_chars(cfg, width, height, ratio)


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
        out.append(TelopEvent(t, end, text, event.speaker, event.scene))
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


def relative_luminance(rgb_hex: str) -> float:
    """sRGBの相対輝度(0=黒, 1=白)。座布団に白文字を載せられるかの判定に使う。"""
    rgb = rgb_hex.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(rgb[i:i + 2], 16) / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def warn_speaker_colors(cfg: dict) -> list:
    """話者の色が読みにくい向きになっていないか調べる(警告用)。

    座布団や縁取りに使うなら暗い色、文字色に使うなら明るい色でないと沈む。
    """
    band_on = bool((cfg.get("band") or {}).get("enabled"))
    target = (cfg.get("speaker_color_target") or "auto").lower()
    if target == "auto":
        target = "band" if band_on else "outline"

    checks = []
    if band_on:
        checks.append(("band.color", cfg["band"]["color"], "dark"))
    want = "light" if target in ("text", "both") else "dark"
    for name, value in (cfg.get("speakers") or {}).items():
        checks.append((f"speakers.{name}", value, want))

    problems = []
    for name, value, kind in checks:
        try:
            lum = relative_luminance(value)
        except (ValueError, IndexError):
            continue
        if kind == "dark" and lum > 0.35:
            problems.append(f"{name}={value} (明るすぎ。白文字が沈みます)")
        elif kind == "light" and lum < 0.30:
            problems.append(f"{name}={value} (暗すぎ。文字が読みにくくなります)")
    return problems


def ass_escape(text: str) -> str:
    # 対にならなかった強調マーカーの * は表示せずに落とす
    return (text.replace("{", "(").replace("}", ")")
            .replace("\n", "\\N").replace("*", ""))


# ---------------------------------------------------------------- フォント解決

def font_names_from_file(path: Path) -> tuple:
    """フォントファイルから (ウェイト固有のファミリ名, 総称ファミリ名) を読む。

    例: NotoSansJP-900.ttf は nameID 1 が "Noto Sans JP Black"、
    nameID 16 が "Noto Sans JP"。総称名で指定すると、システムに同名系統の
    別フォント(Noto Sans CJK JP など)があるとそちらに置き換わって
    細く描画されてしまうので、**固有名のほうを使う**。
    """
    return _read_font_names(path)


def font_family_from_file(path: Path) -> str:
    """フォントの識別に使う名前(ウェイト固有名を優先)。読めなければ空文字。"""
    specific, generic = _read_font_names(path)
    return specific or generic


def _read_font_names(path: Path) -> tuple:
    """name テーブルから (nameID 1, nameID 16) を読む。Windows(platform 3)優先。"""
    try:
        with open(path, "rb") as f:
            tag = f.read(4)
            if tag == b"ttcf":                  # TTCは先頭フォントだけ見る
                f.seek(12)
                f.seek(struct.unpack(">I", f.read(4))[0])
                f.read(4)
            elif tag not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                return "", ""
            num_tables = struct.unpack(">H", f.read(2))[0]
            f.read(6)
            name_off = name_len = 0
            for _ in range(num_tables):
                rec = f.read(16)
                if len(rec) < 16:
                    return "", ""
                if rec[:4] == b"name":
                    name_off, name_len = struct.unpack(">II", rec[8:16])
                    break
            if not name_len:
                return "", ""
            f.seek(name_off)
            fmt, count, str_off = struct.unpack(">HHH", f.read(6))
            records = [struct.unpack(">HHHHHH", f.read(12)) for _ in range(count)]
            best = {1: {}, 16: {}}
            for platform, encoding, _lang, name_id, length, offset in records:
                if name_id not in (1, 16):
                    continue
                f.seek(name_off + str_off + offset)
                raw = f.read(length)
                try:
                    value = (raw.decode("utf-16-be") if platform == 3
                             else raw.decode("mac-roman" if platform == 1 else "utf-8"))
                except (UnicodeDecodeError, LookupError):
                    continue
                value = value.strip()
                if not value:
                    continue
                score = 1 if platform == 3 else 0   # Windows名を優先
                if score > best[name_id].get("score", -1):
                    best[name_id] = {"score": score, "value": value}
            return best[1].get("value", ""), best[16].get("value", "")
    except (OSError, struct.error, ValueError):
        return "", ""


def font_weight_from_file(path: Path) -> int:
    """OS/2 テーブルの usWeightClass を読む(400=標準, 700=太字)。不明なら0。"""
    try:
        with open(path, "rb") as f:
            tag = f.read(4)
            if tag == b"ttcf":
                f.seek(12)
                f.seek(struct.unpack(">I", f.read(4))[0])
                f.read(4)
            elif tag not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                return 0
            num_tables = struct.unpack(">H", f.read(2))[0]
            f.read(6)
            for _ in range(num_tables):
                rec = f.read(16)
                if len(rec) < 16:
                    return 0
                if rec[:4] == b"OS/2":
                    offset = struct.unpack(">I", rec[8:12])[0]
                    f.seek(offset + 4)     # version(2) + xAvgCharWidth(2) の次
                    return struct.unpack(">H", f.read(2))[0]
    except (OSError, struct.error, ValueError):
        pass
    return 0


def _iter_dropin(fonts_dir: Path):
    """assets/fonts のフォントを (パス, 固有名, 総称名) で列挙する。"""
    if not fonts_dir.is_dir():
        return
    for path in sorted(fonts_dir.iterdir()):
        if path.suffix.lower() not in (".ttf", ".otf", ".ttc", ".otc"):
            continue
        specific, generic = font_names_from_file(path)
        if specific or generic:
            yield path, specific or generic, generic


def dropin_font_files(fonts_dir: Path = FONTS_DIR) -> dict:
    """assets/fonts のフォントを {小文字の名前: ファイルパス} で返す(別名も登録)。"""
    found = {}
    for path, specific, generic in _iter_dropin(fonts_dir):
        for alias in (specific, generic):
            if alias:
                found.setdefault(alias.lower(), path)
    return found


def font_is_already_bold(family: str) -> bool:
    """その書体が最初から太いか。太字を二重に掛けて字が潰れるのを防ぐ判定。"""
    path = dropin_font_files().get((family or "").lower())
    return bool(path) and font_weight_from_file(path) >= 600


def dropin_fonts(fonts_dir: Path = FONTS_DIR) -> dict:
    """置かれたフォントの {指定に使える名前(小文字): 実際に指定すべき固有名}。

    総称名(「Noto Sans JP」)でも引けるようにしつつ、返す値は必ず固有名
    (「Noto Sans JP Black」)にする。総称名のまま libass に渡すと、
    システムの同系統フォントに置き換わって細く描画されることがあるため。
    """
    found = {}
    for _path, specific, generic in _iter_dropin(fonts_dir):
        for alias in (specific, generic):
            if alias:
                found.setdefault(alias.lower(), specific)
    return found


def installed_font_families() -> set:
    """PCにインストール済みのフォントのファミリ名(小文字)を集める。"""
    names = set()
    fc_list = shutil.which("fc-list")
    if fc_list:
        out = subprocess.run([fc_list, ":", "family"], capture_output=True, text=True)
        for line in out.stdout.splitlines():
            for part in line.split(","):
                if part.strip():
                    names.add(part.strip().lower())
    if sys.platform == "win32":
        try:
            import winreg
            for root, sub in (
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
                (winreg.HKEY_CURRENT_USER,
                 r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            ):
                try:
                    key = winreg.OpenKey(root, sub)
                except OSError:
                    continue
                with key:
                    for i in range(winreg.QueryInfoKey(key)[1]):
                        label = winreg.EnumValue(key, i)[0]
                        # 例: "Meiryo & Meiryo Italic & ... (TrueType)"
                        label = re.sub(r"\s*\((?:True|Open)Type\)\s*$", "", label)
                        for part in label.split("&"):
                            part = part.strip()
                            if part:
                                names.add(part.lower())
        except Exception:  # レジストリが読めなくても致命的ではない
            pass
    return names


def resolve_font_from(candidates: list, quiet: bool = False) -> str:
    """候補のうちPCに実在する最初のフォント名を返す。無ければ先頭候補。"""
    candidates = [c for c in (candidates or []) if c]
    if not candidates:
        return ""
    dropin = dropin_fonts()
    installed = installed_font_families()
    for name in candidates:
        key = name.lower()
        if key in dropin:
            return dropin[key]   # 同梱フォントは必ず固有名で指定する
        if key in installed:
            return name
    if not quiet:
        print(f"[warn] テロップ用フォントが見つかりませんでした。{candidates[0]} を指定して"
              f"続行します(表示が □ になる場合はフォントを入れてください: docs/06参照)",
              file=sys.stderr)
    return candidates[0]


def resolve_font(cfg: dict, quiet: bool = False) -> str:
    """設定の font / font_candidates から実際に使えるフォント名を決める。"""
    st = cfg["style"]
    configured = (st.get("font") or "auto").strip()
    candidates = ([configured] if configured.lower() != "auto"
                  else list(st.get("font_candidates") or []))
    return resolve_font_from(candidates or ["Noto Sans JP Black"], quiet)


# ---------------------------------------------------------------- ASS字幕生成

def _rect(x0: float, y0: float, x1: float, y1: float) -> str:
    return f"m {x0:.0f} {y0:.0f} l {x1:.0f} {y0:.0f} {x1:.0f} {y1:.0f} {x0:.0f} {y1:.0f}"


def _round_rect(x0: float, y0: float, x1: float, y1: float, r: float) -> str:
    """角丸の矩形。円弧は3次ベジェで近似する(係数はASSの描画コマンド用)。"""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    if r < 1.0:
        return _rect(x0, y0, x1, y1)
    k = r * 0.5523  # 円をベジェで近似するときの定数
    return (f"m {x0 + r:.0f} {y0:.0f} l {x1 - r:.0f} {y0:.0f} "
            f"b {x1 - r + k:.0f} {y0:.0f} {x1:.0f} {y0 + r - k:.0f} {x1:.0f} {y0 + r:.0f} "
            f"l {x1:.0f} {y1 - r:.0f} "
            f"b {x1:.0f} {y1 - r + k:.0f} {x1 - r + k:.0f} {y1:.0f} {x1 - r:.0f} {y1:.0f} "
            f"l {x0 + r:.0f} {y1:.0f} "
            f"b {x0 + r - k:.0f} {y1:.0f} {x0:.0f} {y1 - r + k:.0f} {x0:.0f} {y1 - r:.0f} "
            f"l {x0:.0f} {y0 + r:.0f} "
            f"b {x0:.0f} {y0 + r - k:.0f} {x0 + r - k:.0f} {y0:.0f} {x0 + r:.0f} {y0:.0f}")


def estimate_text_width(text: str, font_size: float, spacing: float) -> float:
    """行の表示幅(px)を概算する。座布団をテキスト幅に合わせるのに使う。

    日本語フォントは全角がちょうど1em幅で作られているので、
    display_width(全角=1) × 文字サイズ でかなり正確に見積もれる。
    """
    widest = 0.0
    for line in text.split("\\N"):
        chars = len(line.replace("*", ""))
        widest = max(widest,
                     display_width(line) * font_size + spacing * max(0, chars - 1))
    return widest


def _alpha_tag(opacity: float) -> str:
    return f"\\1a&H{max(0, min(255, int(round((1.0 - opacity) * 255)))):02X}&"


def build_ass(events: list, cfg: dict, width: int, height: int,
              total: float, title_text: str = "", font: str = None) -> str:
    """ニュース番組風のASS字幕を組み立てる。

    寸法は画面高さに対する比率で持ち、解像度が変わっても同じ見た目になる
    (放送のテロップ設計と同じ考え方)。レイヤーは 0=座布団のグラデーション、
    1=上辺アクセント、2=二重縁取りの外側、3=本文、4=番組名バー。
    """
    st, bd, tl = cfg["style"], cfg["band"], cfg["title"]
    if not font:  # 呼び出し側が解決済みの名前を渡さなかった場合の保険
        font = st.get("font") or ""
        if font.lower() in ("", "auto"):
            font = (st.get("font_candidates") or ["Noto Sans JP Black"])[0]

    fs = max(10, round(st["font_size_ratio"] * height))
    outline = round(max(0.5, st["outline_em"] * fs), 1)
    shadow = round(st["shadow_em"] * fs, 1)
    spacing = round(st["tracking_em"] * fs, 1)
    safe_x = st["safe_margin_ratio"] * width

    def wants_bold(font_name: str) -> bool:
        """太字を掛けるべきか。既に太い書体に重ね掛けすると字が潰れる。"""
        setting = st.get("bold", "auto")
        if isinstance(setting, str) and setting.strip().lower() == "auto":
            return not font_is_already_bold(font_name)
        return bool(setting)

    base_bold = wants_bold(font)
    bold = -1 if base_bold else 0
    blur = float(st.get("blur") or 0)

    max_lines = cfg["telop"]["max_lines"]
    line_h = fs * st["line_height_em"]
    pad = bd["padding_em"] * fs
    pad_x = bd.get("padding_x_em", bd["padding_em"] * 1.5) * fs
    accent_h = bd["accent_em"] * fs
    y_bottom = (height if bd["full_bleed"]
                else height - bd["bottom_margin_ratio"] * height)
    title_fs = max(10, round(tl["font_size_ratio"] * height))
    title_pad = round(title_fs * 0.42, 1)
    title_margin = tl["margin_ratio"] * height

    def band_top(n_lines: int, scale: float = 1.0) -> float:
        return y_bottom - (n_lines * line_h + 2 * pad) * scale

    text_color = ass_color(st["text_color"])
    outer = ass_color(st["double_outline_color"])
    shadow_color = ass_color("000000", st.get("shadow_opacity", 0.45))
    # シーンごとの見た目(フォント・拡大率・色)を先に解決しておく
    scene_specs = {}
    for name, spec in (cfg.get("scenes") or {}).items():
        spec = spec or {}
        scene_specs[name] = {
            "font": resolve_font_from(spec.get("font_candidates"), quiet=True) or font,
            "scale": float(spec.get("scale") or 1.0),
            "color": spec.get("color") or st["text_color"],
            "outline_color": spec.get("outline_color") or "",
        }

    def scene_of(ev: TelopEvent) -> dict:
        return scene_specs.get(ev.scene) or {
            "font": font, "scale": 1.0,
            "color": st["text_color"], "outline_color": ""}
    header = f"""[Script Info]
; auto_edit.py generated (news-style telop)
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Telop,{font},{fs},{text_color},{text_color},{ass_color(st['outline_color'])},{shadow_color},{bold},0,0,0,100,100,{spacing},0,1,{outline},{shadow},5,0,0,0,1
Style: TelopEdge,{font},{fs},{outer},{outer},{outer},&H00000000,{bold},0,0,0,100,100,{spacing},0,1,{round(outline * 2.6, 1)},0,5,0,0,0,1
Style: Shape,{font},{fs},&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Title,{font},{title_fs},{ass_color(tl['text_color'])},{ass_color(tl['text_color'])},{ass_color(tl['bg_color'], tl['bg_opacity'])},{ass_color(tl['bg_color'], tl['bg_opacity'])},{bold},0,0,0,100,100,{spacing},0,3,{title_pad},0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header.rstrip("\n")]

    def shape(layer: int, start: str, end: str, color: str, opacity: float,
              path: str) -> None:
        lines.append(
            f"Dialogue: {layer},{start},{end},Shape,,0,0,0,,"
            f"{{\\p1\\pos(0,0)\\1c{ass_color(color)}{_alpha_tag(opacity)}\\bord0\\shad0}}"
            f"{path}{{\\p0}}")

    if title_text:
        x, y = safe_x, title_margin
        # 左端の縦アクセントバー → 番組名ボックス(BorderStyle=3で文字に沿う)。
        # ボックスの高さは「行の高さ + 上下パディング」になるのでバーもそれに合わせる
        bar_w = max(2.0, title_fs * 0.13)
        box_h = title_fs * st["line_height_em"] + title_pad * 2
        shape(4, ass_time(0), ass_time(total), tl["accent_color"], 1.0,
              _rect(x, y, x + bar_w, y + box_h))
        lines.append(
            f"Dialogue: 4,{ass_time(0)},{ass_time(total)},Title,,0,0,0,,"
            f"{{\\an7\\pos({x + bar_w + title_pad:.0f},{y + title_pad:.0f})}}"
            f"{ass_escape(title_text)}")

    em = cfg.get("emphasis") or {}
    speakers = cfg.get("speakers") or {}
    # 話者の色をどこに反映するか: text=文字色 / outline=縁取り / band=座布団
    target = (cfg.get("speaker_color_target") or "auto").lower()
    if target == "auto":
        target = "band" if bd["enabled"] else "outline"

    def speaker_color(ev: TelopEvent, where: str) -> str:
        """この話者の色を where に使うなら返す。使わないなら空。"""
        color = speakers.get(ev.speaker)
        if not color:
            return ""
        return color if target in (where, "both") else ""

    def text_color_of(ev: TelopEvent) -> str:
        scene = scene_of(ev)
        if scene["color"] != st["text_color"]:   # シーン指定を最優先
            return scene["color"]
        return speaker_color(ev, "text") or st["text_color"]

    def speaker_outline(ev: TelopEvent) -> str:
        scene = scene_of(ev)
        if scene["outline_color"]:
            return scene["outline_color"]
        return speaker_color(ev, "outline") or st["outline_color"]

    def event_size(ev: TelopEvent) -> float:
        """このテロップで実際に使う文字サイズ(px)。

        2行になったら小さめの設定に切り替え、それでも画面からはみ出す長さなら
        収まるところまで縮める(\\q2で自動折返しを切っているため保険が要る)。
        """
        ratio = st["font_size_ratio"]
        wrapped = st.get("font_size_ratio_wrapped")
        if wrapped and ev.text.count("\\N") >= 1:
            ratio = wrapped
        size = ratio * height * scene_of(ev)["scale"]
        avail = width * (1 - 2 * st["safe_margin_ratio"])
        est = estimate_text_width(ev.text, size, spacing * size / fs)
        if est > avail:
            size *= avail / est
        # ASSには整数で書き出すので、切り捨てておかないと丸めで幅を超えうる
        return float(max(10, int(size)))

    def body_of(ev: TelopEvent, text: str) -> str:
        """強調部分だけ色を変えた本文を組み立てる。"""
        normal_tags = (f"\\1c{ass_color(text_color_of(ev))}"
                       f"\\3c{ass_color(speaker_outline(ev))}")
        parts = []
        for seg, is_em in emphasis_runs(text, cfg):
            if seg == "\\N":
                parts.append("\\N")
                continue
            escaped = ass_escape(seg)
            if not is_em:
                parts.append(escaped)
                continue
            tags = f"\\1c{ass_color(em.get('color') or 'FFE14D')}"
            if em.get("outline_color"):
                tags += f"\\3c{ass_color(em['outline_color'])}"
            parts.append(f"{{{tags}}}{escaped}{{{normal_tags}}}")
        return "".join(parts)

    steps = max(1, int(bd["gradient_steps"])) if bd["gradient"] > 0 else 1
    for ev in events:
        s, e = ass_time(ev.start), ass_time(ev.end)
        n_lines = min(max_lines, ev.text.count("\\N") + 1) if bd.get("fit_content") \
            else max_lines
        ev_fs = event_size(ev)
        size_scale = ev_fs / fs
        y_top = band_top(n_lines, size_scale)
        band_color = speaker_color(ev, "band") or bd["color"]

        if bd["enabled"]:
            # 座布団の左右: 全幅か、テキスト幅に合わせるか
            if bd.get("fit_width"):
                half = (estimate_text_width(ev.text, ev_fs, spacing * size_scale) / 2
                        + pad_x * size_scale)
                x0, x1 = max(0.0, width / 2 - half), min(float(width), width / 2 + half)
            else:
                x0, x1 = 0.0, float(width)
            radius = bd.get("corner_radius_em", 0) * fs

            if bd["gradient"] > 0:
                # 上ほどわずかに透けるグラデーション。矩形を「下端まで」重ねて
                # 不透明度を積み上げる(隣接矩形を並べると継ぎ目に縞が出るため)。
                # 既存の不透明度Oに追加分aを重ねるとO+a(1-O)になるので逆算する。
                prev = 0.0
                for i in range(steps):
                    ratio = i / max(1, steps - 1) if steps > 1 else 1.0
                    target = bd["opacity"] * (1.0 - bd["gradient"] * (1.0 - ratio))
                    add = 1.0 if prev >= 1.0 else (target - prev) / (1.0 - prev)
                    prev = target
                    if add <= 0.001:
                        continue
                    y = y_top + (y_bottom - y_top) * i / steps
                    shape(0, s, e, band_color, add, _rect(x0, y, x1, y_bottom))
            else:
                shape(0, s, e, band_color, bd["opacity"],
                      _round_rect(x0, y_top, x1, y_bottom, radius))
            if accent_h >= 1:
                shape(1, s, e, bd["accent_color"], 1.0,
                      _rect(x0, y_top - accent_h, x1, y_top))

        cx = width / 2
        common = f"\\q2" + (f"\\blur{blur}" if blur else "")
        # シーン/行数ごとにフォント・サイズ・縁取りを差し替える
        scene = scene_of(ev)
        if scene["font"] != font:
            common += f"\\fn{scene['font']}"
            # シーン用フォントの太さが違う場合は太字指定も合わせる
            if wants_bold(scene["font"]) != base_bold:
                common += "\\b1" if wants_bold(scene["font"]) else "\\b0"
        if abs(size_scale - 1.0) > 0.001:
            common += (f"\\fs{ev_fs:.0f}\\bord{outline * size_scale:.1f}"
                       f"\\shad{shadow * size_scale:.1f}"
                       f"\\fsp{spacing * size_scale:.1f}")
        outline_c = speaker_outline(ev)
        if outline_c != st["outline_color"]:
            common += f"\\3c{ass_color(outline_c)}"
        body_c = text_color_of(ev)
        if body_c != st["text_color"]:
            common += f"\\1c{ass_color(body_c)}"

        # 行ごとに位置を指定する。libassの行送りはフォント任せで詰まりがちなので、
        # line_height_em どおりの行間を自前で作る
        text_lines = ev.text.split("\\N")
        line_h_ev = ev_fs * st["line_height_em"]
        for idx, line_text in enumerate(text_lines):
            body = body_of(ev, line_text)
            if bd["enabled"]:  # 座布団の中央に行ブロックを置く
                center = (y_top + y_bottom) / 2
                cy = center + (idx - (len(text_lines) - 1) / 2) * line_h_ev
                align = 5
            else:              # 座布団なしは最終行の下端を固定する
                cy = st["position_ratio"] * height \
                    - (len(text_lines) - 1 - idx) * line_h_ev
                align = 2
            tags = f"\\an{align}\\pos({cx:.0f},{cy:.0f})" + common
            # 帯なしで映像に直接乗せる場合の可読性確保として二重縁取りを選べる
            if st.get("double_outline"):
                lines.append(f"Dialogue: 2,{s},{e},TelopEdge,,0,0,0,,{{{tags}}}{body}")
            lines.append(f"Dialogue: 3,{s},{e},Telop,,0,0,0,,{{{tags}}}{body}")
    return "\n".join(lines) + "\n"


def build_filter_script(keeps: list, has_ass: bool, ass_name: str,
                        bgm: dict, total: float, fontsdir: str = "") -> str:
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
        opts = f"subtitles={ass_name}"
        if fontsdir:  # 同梱フォントをPCにインストールせずに使う
            opts += f":fontsdir={fontsdir}"
        parts.append(f"[vc]{opts}[vout]")
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

def deep_merge(base: dict, override: dict) -> dict:
    """辞書を再帰的に上書きする(リストは丸ごと差し替え)。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path, preset: str = None) -> dict:
    """設定を読み、preset の上書きを適用する。"""
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    presets = cfg.pop("presets", None) or {}
    name = preset or cfg.get("preset")
    if name:
        if name not in presets:
            sys.exit(f"[error] 知らないpresetです: {name}\n"
                     f"  使えるのは: {', '.join(presets) or '(なし)'}")
        cfg = deep_merge(cfg, presets[name])
    cfg["preset"] = name or ""
    return cfg


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
         "format=duration:stream=codec_type,width,height,r_frame_rate",
         "-of", "json", str(input_path)],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    width = height = 0
    fps = 30.0
    has_audio = False
    for st in info.get("streams", []):
        if st.get("codec_type") == "video" and not width:
            width, height = int(st.get("width", 0)), int(st.get("height", 0))
            num, _, den = (st.get("r_frame_rate") or "30/1").partition("/")
            try:
                if float(den or 1) > 0:
                    fps = float(num) / float(den or 1)
            except ValueError:
                pass
        if st.get("codec_type") == "audio":
            has_audio = True
    return {"duration": float(info["format"]["duration"]), "fps": fps or 30.0,
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
    # フレーム指定があれば秒に直す(「発話の間は2〜3フレーム」を再現するため)
    pad = jc["keep_padding"]
    if jc.get("keep_padding_frames"):
        pad = float(jc["keep_padding_frames"]) / info["fps"]
    silences = detect_silences(input_path, cfg, info["duration"])
    keeps = build_keep_segments(silences, info["duration"], pad,
                                jc["join_gap"], jc["min_keep"])
    if not keeps:
        sys.exit("[error] 全編が無音判定になりました。configs/auto_edit.yaml の "
                 "silence_threshold_db を下げて(例: -45)再実行してください。")
    return {"input": str(input_path), "duration": info["duration"],
            "width": info["width"], "height": info["height"], "fps": info["fps"],
            "keep_segments": [[round(s, 3), round(e, 3)] for s, e in keeps],
            "params": dict(jc, keep_padding_applied=round(pad, 4))}


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
    limit = line_budget(cfg, plan["width"], plan["height"])
    split = []
    for ev in raw_events:
        ev = prepare_event(ev, cfg)  # 「名前:」で話者、行頭「!」でツッコミ扱い
        split.extend(split_telop(ev, limit, tc["max_lines"],
                                 tc["strip_trailing_period"]))
    return remap_events(split, keeps, tc["min_duration"],
                        tc.get("max_duration"), tc.get("reading_speed"))


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
    ass_name, fontsdir = None, ""
    if events or title_text:
        font = resolve_font(cfg)
        print(f"  テロップのフォント: {font}")
        for problem in warn_speaker_colors(cfg):
            print(f"[warn] 話者の色が読みにくい可能性: {problem}", file=sys.stderr)
        ass_name = "telop.ass"
        (workdir / ass_name).write_text(
            build_ass(events, cfg, plan["width"], plan["height"], total,
                      title_text, font=font),
            encoding="utf-8")
        # assets/fonts のフォントは作業フォルダに複製し、相対パスでfontsdirに渡す
        if dropin_fonts():
            local = workdir / "fonts"
            local.mkdir(exist_ok=True)
            for src in FONTS_DIR.iterdir():
                if src.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"):
                    shutil.copy2(src, local / src.name)
            fontsdir = "fonts"

    bgm_cfg = None
    if bgm_path:
        bgm_cfg = {"volume_db": cfg["bgm"]["volume_db"], "fade": cfg["bgm"]["fade"],
                   "ducking": cfg["bgm"]["ducking"]}
    (workdir / "filter.txt").write_text(
        build_filter_script(keeps, ass_name is not None, ass_name or "", bgm_cfg,
                            total, fontsdir),
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


SAMPLE_TELOP = "上位3割の人だけが知っている*たった1つ*の方法をお伝えします"


def cmd_fonts(args) -> int:
    cfg = load_config(Path(args.config), args.preset)
    drop = dropin_fonts()
    installed = installed_font_families()
    print(f"assets/fonts に置かれたフォント: "
          f"{', '.join(drop.values()) if drop else '(なし)'}")
    print("候補フォントの状況 (上から順に使われる):")
    for name in (cfg["style"].get("font_candidates") or []):
        if name.lower() in drop:
            state = "○ assets/fonts にあり"
        elif name.lower() in installed:
            state = "○ インストール済み"
        else:
            state = "×  無し"
        print(f"  {state:<20} {name}")
    print(f"\n実際に使うフォント: {resolve_font(cfg)}")
    print("見た目を確認するには: python scripts/auto_edit.py preview 動画.mp4")
    return 0


def cmd_preview(args) -> int:
    """テロップの見た目だけを静止画1枚で確認する(動画を丸ごと書き出さない)。"""
    cfg = load_config(Path(args.config), args.preset)
    ffmpeg = need_tool("ffmpeg")
    wd = Path(args.output).resolve().parent
    wd.mkdir(parents=True, exist_ok=True)
    frame = wd / "_preview_frame.png"

    if args.input:
        info = probe(Path(args.input))
        width, height = info["width"], info["height"]
        subprocess.run([ffmpeg, "-y", "-v", "error", "-ss",
                        f"{info['duration'] / 2:.2f}", "-i", str(Path(args.input).resolve()),
                        "-frames:v", "1", str(frame)], check=True)
    else:
        width, height = 1920, 1080
        subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
                        f"gradients=s={width}x{height}:c0=0x1b2838:c1=0x4a6d8c:d=1",
                        "-frames:v", "1", str(frame)], check=True)

    tc = cfg["telop"]
    raw = args.text or SAMPLE_TELOP
    # 本番と同じ折り返し・話者判定を通す(プレビューと仕上がりを一致させる)
    prepared = prepare_event(TelopEvent(0.0, 5.0, raw), cfg)
    limit = line_budget(cfg, width, height)
    events = split_telop(prepared, limit, tc["max_lines"],
                         tc["strip_trailing_period"]) or [TelopEvent(0.0, 5.0, raw)]
    title = args.title if args.title is not None else cfg["title"]["text"]
    font = resolve_font(cfg)
    print(f"  テロップのフォント: {font}  /  preset: {cfg.get('preset') or '(なし)'}")
    for problem in warn_speaker_colors(cfg):
        print(f"[warn] 話者の色が読みにくい可能性: {problem}", file=sys.stderr)
    ass = wd / "_preview.ass"
    ass.write_text(build_ass([events[0]], cfg, width, height,
                             5.0, title, font=font), encoding="utf-8")

    opts = f"subtitles={ass.name}"
    if dropin_fonts():
        local = wd / "fonts"
        local.mkdir(exist_ok=True)
        for src in FONTS_DIR.iterdir():
            if src.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"):
                shutil.copy2(src, local / src.name)
        opts += ":fontsdir=fonts"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", frame.name, "-vf", opts,
                    "-frames:v", "1", str(Path(args.output).resolve())],
                   cwd=wd, check=True)
    frame.unlink(missing_ok=True)
    print("プレビュー画像を書き出しました:", args.output)
    return 0


def cmd_analyze(args) -> int:
    cfg = load_config(Path(args.config), args.preset)
    input_path = Path(args.input)
    plan = analyze(input_path, cfg)
    wd = default_workdir(input_path)
    (wd / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print("カット計画を保存しました:", wd / "plan.json")
    print_plan_summary(plan)
    return 0


def cmd_transcribe(args) -> int:
    cfg = load_config(Path(args.config), args.preset)
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
    cfg = load_config(Path(args.config), args.preset)
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
    cfg = load_config(Path(args.config), args.preset)
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
        p.add_argument("--preset", help="テロップ様式 (business / talk / news)")
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

    p = sub.add_parser("preview", help="テロップの見た目を静止画1枚で確認する")
    p.add_argument("input", nargs="?", help="入力動画 (省略時は無地の背景で確認)")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--preset", help="テロップ様式 (business / talk / news)")
    p.add_argument("-o", "--output", default="telop_preview.png")
    p.add_argument("--text", help="サンプル文言 (改行は \\N)")
    p.add_argument("--title", help="左上の番組名バー")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("fonts", help="使えるテロップ用フォントを調べる")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--preset", help="テロップ様式 (business / talk / news)")
    p.set_defaults(func=cmd_fonts)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
