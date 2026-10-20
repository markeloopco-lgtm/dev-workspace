#!/usr/bin/env python3
"""編集プラン（Markdown）を作る／読み込んで、カット位置とテロップを作る。

1) 下書きを作る（文字起こしから。言いよどみは自動でカット済みにする）
    python video_edit/build_edit.py --name <名前> --init
   → video_edit/projects/<名前>/edit_plan.md

2) edit_plan.md を直したら反映する
    python video_edit/build_edit.py --name <名前>
   → edit.json（カット位置）, telops.ass（テロップ）

edit_plan.md の書き方:
    - 012 (1:23) 今日は**ジャーナリング**について~~えー~~お話しします
      ・行頭の番号（と時刻）は消さない。番号が音声の位置を表す
      ・~~ ~~ で囲んだ部分はカット（音声も映像も消える）。行ごと消してもカット
      ・文字は自由に直してよい（テロップの文字だけが変わる。音声は ~~ ~~ 以外は消えない）
      ・**太字** は強調色のテロップ、「／」でテロップを強制改行
      ・［メモ：…］はテロップに出ない
    ### 見出し     … 次の文から画面左上に見出しを出す（「### -」で見出しを消す）
    > 大きい文字   … 直前の文の頭で、画面中央に大きく出す
"""

from __future__ import annotations

import argparse
import bisect
import math
import re
import wave
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from vedit_common import PROJECTS_DIR, WORK_DIR, load_json, log, parse_rate, save_json, setup_console, tc

DEFAULTS = {
    "間を詰める": ("0.35", "秒。これより長い無音はカットして詰める"),
    "前の余白": ("0.08", "秒。話し始めの前に残す長さ"),
    "後ろの余白": ("0.15", "秒。話し終わりの後に残す長さ"),
    "ズーム切替": ("1.1", "倍。カットの継ぎ目で寄り/引きを切り替える。1で無し"),
    "ズーム間隔": ("6", "秒。同じ画角がこれ以上続いたら次のカットで切り替える"),
    "ズーム中心": ("0.5, 0.42", "寄るときの中心（横, 縦）。0〜1"),
    "テロップ文字数": ("18", "1枚のテロップの最大文字数"),
    "テーマ色": ("#E8738A", "見出しの色"),
    "強調色": ("#FFE14D", "**太字** の色"),
    "縁取り色": ("#3B2F2F", "テロップの縁の色"),
    "強調ワード": ("", "カンマ区切り。全テロップで自動的に強調する言葉"),
    "オープニングタイトル": ("", "冒頭3秒に大きく出すタイトル。空なら無し"),
    "大テロップの長さ": ("1.6", "秒。「> 」の大きいテロップを出す長さ"),
    "見出しの登場演出": ("あり", "あり/なし。見出しが変わる時に画面中央にも大きく出す"),
    "音量": ("-14", "LUFS。YouTube向けの標準は -14"),
}
FILLER_RE = re.compile(r"(えー+と?|えっと|えーっと|ええと|あのー+|あの〜|うーん|んー+|あー+|まぁ+)[、,]?")
PUNCT_END = "。？！?!"
MEMO_RE = re.compile(r"[［\[]\s*メモ\s*[：:][^］\]]*[］\]]")
LINE_RE = re.compile(r"^\s*[-*]\s*(\d{1,4})\s*(?:[（(][^）)]*[）)])?\s*(.*)$")
NO_LINE_START = set("、。ーっゃゅょぁぃぅぇぉッャュョァィゥェォ！？!?）」』")


# ---------------------------------------------------------------- 文字起こし → 文

def load_sentences(tr: dict, max_len=45):
    toks = []
    for seg in tr["segments"]:
        for w in seg["words"]:
            t = w["word"]
            t = (" " + t.strip()) if (t.startswith(" ") and t.strip()[:1].isascii() and t.strip()[:1].isalnum()) else t.strip()
            if not t:
                continue
            s, e = float(w["start"]), float(w["end"])
            toks.append({"t": t, "s": s, "e": max(e, s + 0.04), "p": w.get("p", 1.0), "seg": seg["id"]})
        if toks:
            toks[-1]["seg_end"] = True
    sents, cur = [], []

    def flush():
        if cur:
            if cur[0]["t"].startswith(" "):
                cur[0]["t"] = cur[0]["t"].lstrip()
            sents.append({"id": len(sents) + 1, "tokens": list(cur)})
            cur.clear()
    for i, tk in enumerate(toks):
        cur.append(tk)
        text_len = sum(len(x["t"]) for x in cur)
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        gap = (nxt["s"] - tk["e"]) if nxt else 99
        if tk["t"][-1:] in PUNCT_END or gap > 0.7 or tk.get("seg_end") or (text_len >= max_len and tk["t"][-1:] in "、,"):
            flush()
    flush()
    for s in sents:
        s["text"] = "".join(t["t"] for t in s["tokens"])
    return sents


def char_token_index(sent):
    idx = []
    for k, t in enumerate(sent["tokens"]):
        idx += [k] * len(t["t"])
    return idx


# ---------------------------------------------------------------- プラン（Markdown）

def write_plan(path: Path, name: str, src: dict, sents, tr: dict):
    L = [f"# 編集プラン：{name}", "",
         f"元動画: {src.get('file')}（{tc(src.get('duration', 0))[:-3]}） / 文字起こし: {tr.get('model')}", "",
         "## 使い方", "",
         "- 行頭の番号と時刻は消さないでください（音声の位置を表します）",
         "- カットしたい部分は ~~ ~~ で囲みます（行ごと消してもカット）",
         "- 文字は自由に直せます（テロップの文字が変わるだけで、音声は消えません）",
         "- **太字** は強調色のテロップになります。「／」でテロップを区切れます",
         "- 「### 」で始まる行は画面左上の見出しになります（次の文から。「### -」で消す）",
         "- 「> 」で始まる行は、直前の文の頭で画面中央に大きく出ます",
         "- ［メモ：…］はテロップに出ません", "",
         "## 設定", ""]
    for k, (v, note) in DEFAULTS.items():
        L.append(f"- {k}: {v}（{note}）")
    L += ["", "## 本編", ""]
    for i, s in enumerate(sents):
        text = s["text"]
        out, pos = [], 0
        for m in FILLER_RE.finditer(text):
            # 文頭か「、」の直後にある言いよどみだけをカット候補にする（「あの人」などを誤って消さない）
            if m.start() == 0 or text[m.start() - 1] in "、,。":
                out.append(text[pos:m.start()])
                out.append(f"~~{m.group(0)}~~")
                pos = m.end()
        out.append(text[pos:])
        line = "".join(out)
        memos = []
        avgp = float(np.mean([t["p"] for t in s["tokens"]])) if s["tokens"] else 1.0
        if avgp < 0.45:
            memos.append("聞き取りあやしい")
        if i + 1 < len(sents):
            a, b = norm(s["text"]), norm(sents[i + 1]["text"])
            if len(a) >= 4 and SequenceMatcher(None, a, b[:len(a) + 4], autojunk=False).ratio() > 0.6:
                memos.append("言い直し候補（次の文とほぼ同じ）")
        memo = f" ［メモ：{'、'.join(memos)}］" if memos else ""
        L.append(f"- {s['id']:03d} ({tc(s['tokens'][0]['s'])[2:7]}) {line}{memo}")
    L.append("")
    path.write_text("\n".join(L), encoding="utf-8")


def norm(t: str) -> str:
    t = FILLER_RE.sub("", t)
    return re.sub(r"[、。！？!?\s・…ー〜]", "", t)


def parse_plan(path: Path):
    settings = {k: v for k, (v, _) in DEFAULTS.items()}
    items = []
    section = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and not line.startswith("###"):
            section = m.group(1)
            continue
        if section == "設定":
            m = re.match(r"^\s*[-*]\s*([^:：]+)[:：]\s*(.*)$", line)
            if m:
                key = m.group(1).strip()
                val = re.split(r"[（(]", m.group(2), maxsplit=1)[0].strip()
                settings[key] = val
        elif section == "本編":
            if line.startswith("###"):
                items.append({"type": "heading", "text": line.lstrip("#").strip()})
            elif line.lstrip().startswith(">"):
                items.append({"type": "impact", "text": line.lstrip()[1:].strip()})
            else:
                m = LINE_RE.match(line)
                if m:
                    items.append({"type": "sentence", "id": int(m.group(1)), "raw": m.group(2)})
    return settings, items


def parse_markup(raw: str):
    """~~カット~~ / **強調** / ／改行 を解釈して、文字ごとの印を返す"""
    s = MEMO_RE.sub("", raw).strip()
    chars, struck, emph, brk = [], [], [], set()
    st = em = False
    i = 0
    while i < len(s):
        if s.startswith("~~", i):
            st = not st
            i += 2
            continue
        if s.startswith("**", i):
            em = not em
            i += 2
            continue
        if s[i] == "／":
            brk.add(len(chars))
            i += 1
            continue
        chars.append(s[i])
        struck.append(st)
        emph.append(em)
        i += 1
    return "".join(chars), struck, emph, brk


def align(orig: str, edited: str):
    """元の文字起こしと直した文の文字の対応（orig→edited, edited→orig）"""
    o2e = [None] * len(orig)
    e2o = [None] * len(edited)
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, orig, edited, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                o2e[i1 + k] = j1 + k
                e2o[j1 + k] = i1 + k
        elif tag == "replace":
            for k in range(i2 - i1):
                o2e[i1 + k] = j1 + min(j2 - j1 - 1, int(k * (j2 - j1) / (i2 - i1)))
            for k in range(j2 - j1):
                e2o[j1 + k] = i1 + min(i2 - i1 - 1, int(k * (i2 - i1) / (j2 - j1)))
    # 対応の無い文字は近い方の対応を使う
    for arr in (e2o,):
        last = None
        for k in range(len(arr)):
            if arr[k] is None:
                arr[k] = last
            else:
                last = arr[k]
        nxt = None
        for k in range(len(arr) - 1, -1, -1):
            if arr[k] is None:
                arr[k] = nxt
            else:
                nxt = arr[k]
    return o2e, e2o


# ---------------------------------------------------------------- テロップの分割

def chunk_text(text: str, forced: set, maxc: int, minc: int = 6):
    score = {}
    for i, ch in enumerate(text):
        if ch in "、。！？!?…":
            score[i + 1] = 3
        elif ch in "はがをにでともへやねよ":
            score[i + 1] = max(score.get(i + 1, 0), 1)
    for m in re.finditer(r"(から|けど|ので|けれど|ですが|ますが|って|たら|れば|ても|では|には|とか|ながら|ですね|ますね)", text):
        score[m.end()] = max(score.get(m.end(), 0), 2)
    res, a = [], 0
    n = len(text)
    while a < n:
        nxt_forced = [p for p in sorted(forced) if a < p < n]
        limit = nxt_forced[0] if nxt_forced else n
        if limit - a <= maxc + 2:
            res.append((a, limit))
            a = limit
            continue
        best = None
        for p in range(a + minc, min(a + maxc, limit) + 1):
            key = (score.get(p, 0), p)
            if best is None or key > best:
                best = key
        p = best[1] if best and best[0] > 0 else a + maxc
        while p < min(n, a + maxc + 2) and text[p] in NO_LINE_START:
            p += 1
        res.append((a, p))
        a = p
    return res


def display_clean(s: str) -> str:
    s = s.replace("。", "")
    s = re.sub(r"[、,]+$", "", s.strip())
    s = re.sub(r"^[、,]+", "", s)
    return s.replace("、", "　").replace(",", "　").strip()


# ---------------------------------------------------------------- 音量（話し始め・終わりの補正）

class Energy:
    def __init__(self, wav_path: Path):
        with wave.open(str(wav_path), "rb") as wf:
            sr = wf.getframerate()
            x = np.frombuffer(wf.readframes(wf.getnframes()), "<i2").astype(np.float32) / 32768
            if wf.getnchannels() > 1:
                x = x.reshape(-1, wf.getnchannels()).mean(axis=1)
        hop = sr // 100
        n = len(x) // hop
        rms = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
        self.db = 20 * np.log10(rms + 1e-9)
        floor, loud = np.percentile(self.db, 10), np.percentile(self.db, 95)
        self.thr = max(floor + 10, loud - 32)
        self.voiced = self.db > self.thr

    def refine(self, s, e, lo, hi):
        """単語の時刻(s,e)を、実際に声が出ている範囲に合わせて少し補正する（lo〜hiの範囲内で）"""
        v = self.voiced
        i = int(s * 100)
        a = max(int(lo * 100), i - 25)
        k = i
        while k - 1 >= a and k - 1 < len(v) and v[k - 1]:
            k -= 1
        if k == i:  # 開始位置が無音なら、声が出始めるところまで進める（最大0.3秒）
            j = i
            while j < min(len(v), i + 30) and not v[j]:
                j += 1
            if j < min(len(v), i + 30):
                k = j
        s2 = k / 100
        i = int(math.ceil(e * 100))
        b = min(int(hi * 100), i + 35, len(v))
        k = i
        while k < b and v[k]:
            k += 1
        if k == i:
            j = i - 1
            while j > max(int(s2 * 100), i - 30) and j < len(v) and not v[j]:
                j -= 1
            if j > max(int(s2 * 100), i - 30):
                k = j + 1
        e2 = k / 100
        return (s2, e2) if e2 > s2 else (s, e)


# ---------------------------------------------------------------- 組み立て

def fnum(v, default):
    try:
        return float(str(v).strip())
    except ValueError:
        return default


def build(name: str):
    proj = PROJECTS_DIR / name
    src = load_json(proj / "source.json")
    tr = load_json(proj / "transcript.json")
    sents = {s["id"]: s for s in load_sentences(tr)}
    settings, items = parse_plan(proj / "edit_plan.md")
    fps = float(parse_rate(src.get("output_fps") or src["fps_str"]))
    max_pause = fnum(settings["間を詰める"], 0.35)
    pad_b, pad_a = fnum(settings["前の余白"], 0.08), fnum(settings["後ろの余白"], 0.15)
    energy = None
    wav = WORK_DIR / name / "audio16k.wav"
    if wav.exists():
        energy = Energy(wav)
    else:
        log("[注意] audio16k.wav が無いので、話し始め・終わりの補正なしで計算します")

    # 1) 文ごとに、残す単語・消す単語と、テロップ文字を決める
    kept, cut = [], []
    sent_info = {}
    seen = set()
    for it in items:
        if it["type"] != "sentence":
            continue
        s = sents.get(it["id"])
        if not s or it["id"] in seen:
            continue
        seen.add(it["id"])
        orig = s["text"]
        text, struck, emph, brk = parse_markup(it["raw"])
        o2e, e2o = align(orig, text)
        cidx = char_token_index(s)
        tok_struck = [0] * len(s["tokens"])
        tok_len = [len(t["t"]) for t in s["tokens"]]
        for i in range(len(orig)):
            j = o2e[i]
            if j is not None:
                st = struck[j]
            else:  # 元の文字が消されている → 前後が両方カットならカット
                before = next((struck[o2e[k]] for k in range(i - 1, -1, -1) if o2e[k] is not None), False)
                after = next((struck[o2e[k]] for k in range(i + 1, len(orig)) if o2e[k] is not None), False)
                st = before and after
            tok_struck[cidx[i]] += 1 if st else 0
        whole = bool(text) and all(struck)
        for k, t in enumerate(s["tokens"]):
            (cut if (whole or not text or tok_struck[k] * 2 > tok_len[k]) else kept).append(t)
        sent_info[it["id"]] = {"text": text, "struck": struck, "emph": emph, "brk": brk, "e2o": e2o,
                               "cidx": cidx, "whole_cut": whole or not text}
    for sid, s in sents.items():
        if sid not in seen:  # プランから行ごと消された文
            cut += s["tokens"]
    kept.sort(key=lambda t: t["s"])
    cut_iv = sorted((t["s"], t["e"]) for t in cut)
    cut_starts = [c[0] for c in cut_iv]
    cut_ends = sorted(c[1] for c in cut_iv)

    def bounds(s, e):
        """(s,e)の外側で一番近いカット済みの言葉の端（余白がそこへ食い込まないように）"""
        i = bisect.bisect_left(cut_starts, e - 1e-6)
        hi = cut_starts[i] if i < len(cut_starts) else src["duration"]
        j = bisect.bisect_right(cut_ends, s + 1e-6) - 1
        lo = cut_ends[j] if j >= 0 else 0.0
        return lo, hi

    def cut_between(a, b):
        i = bisect.bisect_left(cut_starts, a - 1e-6)
        return i < len(cut_starts) and cut_starts[i] < b

    # 2) 残す単語を「間」で区切ってまとまりにする（間に消した言葉があっても区切る）
    runs = []
    for t in kept:
        if runs and t["s"] - runs[-1]["e"] <= max_pause and not cut_between(runs[-1]["e"], t["s"]):
            runs[-1]["e"] = max(runs[-1]["e"], t["e"])
        else:
            runs.append({"s": t["s"], "e": t["e"]})
    segs = []
    for r in runs:
        lo, hi = bounds(r["s"], r["e"])
        s, e = (energy.refine(r["s"], r["e"], lo, hi) if energy else (r["s"], r["e"]))
        s, e = max(lo, s - pad_b), min(hi, e + pad_a)
        f0, f1 = int(math.floor(s * fps)), int(math.ceil(e * fps))
        if segs and f0 <= segs[-1][1]:
            segs[-1][1] = max(segs[-1][1], f1)
        else:
            segs.append([f0, f1])
    max_f = int(src["duration"] * fps)
    segs = [[max(0, a), min(max_f, b)] for a, b in segs if min(max_f, b) - max(0, a) >= 2]

    # 3) 寄り/引きの切り替え
    zf = fnum(settings["ズーム切替"], 1.1)
    zint = fnum(settings["ズーム間隔"], 6)
    try:
        zc = [float(v) for v in re.split(r"[,、\s]+", settings["ズーム中心"].strip()) if v][:2]
    except ValueError:
        zc = [0.5, 0.42]
    zc = (zc + [0.42])[:2] if zc else [0.5, 0.42]
    out_segs, cum, zoomed, since = [], 0, False, 0.0
    for i, (a, b) in enumerate(segs):
        if i > 0 and zf > 1.001 and since >= zint:
            zoomed, since = not zoomed, 0.0
        out_segs.append({"f0": a, "f1": b, "o0": cum, "zoom": zf if zoomed else 1.0})
        cum += b - a
        since += (b - a) / fps
    out_frames = cum

    f0s = [sg["f0"] for sg in out_segs]

    def to_out(t, side):
        """元動画の時刻 → 書き出し後の時刻（カットされた所なら、開始は直後・終了は直前に寄せる）"""
        f = t * fps
        i = bisect.bisect_right(f0s, f) - 1
        if i >= 0 and f < out_segs[i]["f1"]:
            return (out_segs[i]["o0"] + f - out_segs[i]["f0"]) / fps
        if side == "end" and i >= 0:
            return (out_segs[i]["o0"] + out_segs[i]["f1"] - out_segs[i]["f0"]) / fps
        if i + 1 < len(out_segs):
            return out_segs[i + 1]["o0"] / fps
        return out_frames / fps

    # 4) テロップ
    maxc = int(fnum(settings["テロップ文字数"], 18))
    words = [w.strip() for w in re.split(r"[,、，]", settings["強調ワード"]) if w.strip()]
    events = []
    first_out = {}
    for sid, inf in sent_info.items():
        if inf["whole_cut"]:
            continue
        s = sents[sid]
        keep_idx = [j for j in range(len(inf["text"])) if not inf["struck"][j]]
        disp = "".join(inf["text"][j] for j in keep_idx)
        emph = [inf["emph"][j] for j in keep_idx]
        for w in words:
            for m in re.finditer(re.escape(w), disp):
                for j in range(m.start(), m.end()):
                    emph[j] = True
        brk = {sum(1 for j in keep_idx if j < b) for b in inf["brk"]}
        for a, b in chunk_text(disp, brk, maxc):
            seg_chars = [keep_idx[j] for j in range(a, b)]
            oi = [inf["e2o"][j] for j in seg_chars if inf["e2o"][j] is not None]
            if not oi:
                continue
            toks = [s["tokens"][inf["cidx"][min(i, len(inf["cidx"]) - 1)]] for i in (min(oi), max(oi))]
            t0, t1 = to_out(toks[0]["s"], "start"), to_out(toks[1]["e"], "end")
            piece = display_clean_marked(disp[a:b], emph[a:b])
            if piece and t1 > t0:
                events.append({"start": t0, "end": t1, "style": "Main", "text": piece})
        if s["tokens"]:
            kept_toks = [s["tokens"][inf["cidx"][inf["e2o"][j]]] for j in keep_idx if inf["e2o"][j] is not None]
            if kept_toks:
                first_out[sid] = to_out(kept_toks[0]["s"], "start")
    events.sort(key=lambda e: e["start"])
    total = out_frames / fps
    for i, ev in enumerate(events):
        nxt = events[i + 1]["start"] if i + 1 < len(events) else total
        if nxt - ev["end"] < 0.6:
            ev["end"] = nxt
        ev["end"] = min(max(ev["end"], ev["start"] + 0.6), nxt if nxt > ev["start"] else ev["end"])

    # 見出し・大テロップ・タイトル
    extra = []
    heads = []
    last_sid = None
    pending_head = None
    for it in items:
        if it["type"] == "heading":
            pending_head = it["text"]
        elif it["type"] == "sentence":
            if pending_head is not None and it["id"] in first_out:
                heads.append((first_out[it["id"]], pending_head))
                pending_head = None
            if it["id"] in first_out:
                last_sid = it["id"]
        elif it["type"] == "impact" and last_sid in first_out:
            t0 = first_out[last_sid]
            extra.append({"start": t0, "end": min(total, t0 + fnum(settings["大テロップの長さ"], 1.6)),
                          "style": "Impact", "text": pop() + markup_emph(it["text"], settings)})
    show_card = settings["見出しの登場演出"].strip() not in ("なし", "無し", "off", "0")
    for i, (t0, text) in enumerate(heads):
        t1 = heads[i + 1][0] if i + 1 < len(heads) else total
        if text.strip() in ("-", "ー", ""):
            continue
        extra.append({"start": t0, "end": t1, "style": "Heading", "text": text})
        if show_card:
            extra.append({"start": t0, "end": min(t1, t0 + 1.5), "style": "Card", "text": pop() + text})
    title = settings["オープニングタイトル"].strip()
    if title:
        extra.append({"start": 0.0, "end": min(total, 3.0), "style": "Title", "text": pop() + title.replace("／", r"\N")})
    write_ass(proj / "telops.ass", events + extra, settings)

    edit = {"version": 1, "name": name, "source": src,
            "output": {"width": 1920, "height": 1080, "fps": src.get("output_fps") or src["fps_str"]},
            "zoom": {"factor": zf, "cx": zc[0], "cy": zc[1]},
            "audio": {"loudness": fnum(settings["音量"], -14)},
            "segments": out_segs, "out_frames": out_frames, "out_sec": round(total, 3)}
    save_json(proj / "edit.json", edit)
    src_sec = src["duration"]
    log(f"[完了] 元 {tc(src_sec)} → 仕上がり {tc(total)}（{100 * total / max(src_sec, 1e-6):.0f}%）"
        f" / つなぎ目 {max(0, len(out_segs) - 1)}か所 / テロップ {len(events)}枚 / 見出し {len(heads)}")
    return edit


def pop():
    """ぽんっと出る動き"""
    return r"{\fscx70\fscy70\t(0,110,\fscx108\fscy108)\t(110,190,\fscx100\fscy100)}"


def ass_color(hex_rgb: str, alpha="00"):
    h = hex_rgb.strip().lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


EMPH_ON = "\x01"
EMPH_OFF = "\x02"


def display_clean_marked(s: str, emph):
    """句読点を整えて、強調部分に印を付けた文字列にする"""
    pairs = [(ch, em) for ch, em in zip(s, emph) if ch != "。"]
    while pairs and (pairs[0][0] in "、, 　"):
        pairs.pop(0)
    while pairs and (pairs[-1][0] in "、, 　"):
        pairs.pop()
    out, prev = [], False
    for ch, em in pairs:
        if em != prev:
            out.append(EMPH_ON if em else EMPH_OFF)
            prev = em
        out.append("　" if ch in "、," else ch)
    if prev:
        out.append(EMPH_OFF)
    return "".join(out)


def markup_emph(text: str, settings):
    plain, _, emph, _ = parse_markup(text)
    return display_clean_marked(plain, emph)


def write_ass(path: Path, events, st):
    acc, emc, edge = ass_color(st["テーマ色"]), ass_color(st["強調色"]), ass_color(st["縁取り色"])
    font_main, font_bold = "Rounded Mplus 1c ExtraBold", "Rounded Mplus 1c Black"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font_main},76,&H00FFFFFF,&H000000FF,{edge},&H64000000,0,0,0,0,100,100,1,0,1,7,2,2,60,60,56,1
Style: Heading,{font_bold},50,&H00FFFFFF,&H000000FF,{acc},&H00000000,0,0,0,0,100,100,2,0,3,16,0,7,48,48,40,1
Style: Card,{font_bold},104,&H00FFFFFF,&H000000FF,{acc},&H64000000,0,0,0,0,100,100,2,0,1,10,3,5,80,80,0,1
Style: Impact,{font_bold},120,{emc},&H000000FF,{edge},&H64000000,0,0,0,0,100,100,2,0,1,11,3,5,80,80,0,1
Style: Title,{font_bold},112,&H00FFFFFF,&H000000FF,{acc},&H64000000,0,0,0,0,100,100,2,0,1,11,3,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def t(x):
        cs = int(round(max(0.0, x) * 100))
        h, cs = divmod(cs, 360000)
        m, cs = divmod(cs, 6000)
        s, cs = divmod(cs, 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
    layer = {"Main": 1, "Heading": 0, "Card": 2, "Impact": 3, "Title": 3}
    on = "{\\1c&H" + emc[4:] + "&}"      # 文中の色指定は &HBBGGRR& 形式
    off = "{\\1c&HFFFFFF&}"
    lines = []
    for ev in sorted(events, key=lambda e: (e["start"], layer.get(e["style"], 1))):
        txt = ev["text"].replace("\n", r"\N")
        if ev["style"] == "Impact":  # 大テロップは全体が強調色。強調部分は白抜きに
            txt = txt.replace(EMPH_ON, off).replace(EMPH_OFF, on)
        else:
            txt = txt.replace(EMPH_ON, on).replace(EMPH_OFF, off)
        lines.append(f"Dialogue: {layer.get(ev['style'], 1)},{t(ev['start'])},{t(ev['end'])},{ev['style']},,0,0,0,,{txt}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8-sig")


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser(description="編集プラン(Markdown)からカット位置とテロップを作る")
    ap.add_argument("--name", required=True)
    ap.add_argument("--init", action="store_true", help="文字起こしから edit_plan.md の下書きを作る")
    ap.add_argument("--force", action="store_true", help="--init で既存の edit_plan.md を上書きする")
    args = ap.parse_args(argv)
    proj = PROJECTS_DIR / args.name
    if args.init:
        plan = proj / "edit_plan.md"
        if plan.exists() and not args.force:
            raise SystemExit(f"{plan} は既にあります（上書きするなら --force）")
        tr = load_json(proj / "transcript.json")
        sents = load_sentences(tr)
        write_plan(plan, args.name, load_json(proj / "source.json"), sents, tr)
        log(f"[完了] 下書き {plan}（{len(sents)}文）")
    build(args.name)


if __name__ == "__main__":
    main()
