"""音声の計測: ラウドネス(EBU R128)・発話区間・話速・間(ま)・BGM音量差・効果音らしき強調音。

発話区間は次の優先順で決める:
  1. 字幕ファイル(.srt/.vtt。yt-dlpの自動字幕も可)
  2. faster-whisper による文字起こし(任意インストール)
  3. 帯域エネルギーによる簡易VAD(BGMが大きいと精度が落ちる推定値)
"""

import html
import json
import math
import re
from pathlib import Path

import numpy as np
from scipy import signal

from . import ffmpeg_util as ff

VAD_SR = 16000
JP_CHARS = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟA-Za-z0-9０-９]")


# ---------------------------------------------------------------- 字幕・文字起こし

def _ts(s: str) -> float:
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


TS_LINE = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})")
WORD_TS = re.compile(r"<(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})>")
NON_SPEECH = re.compile(r"^(?:[\[［(（][^\]］)）]*[\]］)）]\s*)+$")   # [音楽] [拍手] (笑) など


def parse_subtitles(path) -> list:
    """SRT/VTTを [{start, end, text}] にする。

    YouTube自動字幕の特徴に対応する:
      - 「前の行 + 新しい行」の2行構成で流れる → 前のcueと同じ1行目は捨てる
      - 表示は次の行が出るまで伸びる → 単語ごとの時刻タグ(<00:00:01.200>)から発話の終わりを推定
      - [音楽] [拍手] だけのcueは発話ではない
    手動字幕の2行cueは全行をつなげて使う。
    """
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n{2,}", raw):        # 空行でcueを区切る(空のcueが次を飲み込まない)
        lines = block.split("\n")
        idx = next((i for i, ln in enumerate(lines) if TS_LINE.search(ln)), None)
        if idx is None:                            # WEBVTTヘッダ・NOTEなど
            continue
        m = TS_LINE.search(lines[idx])
        start, end = _ts(m[1]), _ts(m[2])
        text_lines, word_end = [], None
        for line in lines[idx + 1:]:
            tags = WORD_TS.findall(line)
            if tags:   # 単語ごとの時刻 → 最後の単語の開始 + 文字数からの推定長
                last_word = re.sub(r"<[^>]+>", "", re.split(r"<\d[^>]*>", line)[-1]).strip()
                word_end = _ts(tags[-1]) + max(0.3, 0.13 * len(last_word))
            clean = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
            if clean:
                text_lines.append(clean)
        if text_lines and end > start:
            cues.append({"start": start, "end": end, "lines": text_lines, "word_end": word_end})
    out = []
    prev_last = None
    for c in cues:
        ls = c["lines"]
        short = c["end"] - c["start"] < 0.05       # 自動字幕の10msつなぎcue
        if len(ls) > 1 and ls[0] == prev_last:     # 流れる字幕: 持ち越しの1行目を捨てる
            ls = ls[1:]
        prev_last = c["lines"][-1]
        if short:
            continue
        text = "".join(ls)
        if not text or NON_SPEECH.match(text):
            continue
        end = min(c["end"], c["word_end"]) if c["word_end"] else c["end"]
        end = max(end, c["start"] + 0.2)
        if out and out[-1]["text"] == text:
            out[-1]["end"] = round(max(out[-1]["end"], end), 3)
            continue
        out.append({"start": round(c["start"], 3), "end": round(end, 3), "text": text})
    # 自動字幕は次のcue開始まで表示が伸びるため、重なりを詰める
    for a, b in zip(out, out[1:]):
        if a["end"] > b["start"]:
            a["end"] = b["start"]
    return out


def transcribe_whisper(video_path, model_size: str = "small", out_json: Path = None,
                       max_seconds: float = None) -> list:
    """faster-whisper(CPU・int8)で日本語文字起こし。未インストールなら例外。

    max_seconds を指定すると、その時刻を過ぎた所で打ち切る(残りは文字起こししない)。
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("faster-whisper が未インストールです: "
                           + ff.pip_cmd("faster-whisper")) from e
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(video_path), language="ja", vad_filter=True,
                                   beam_size=1)
    out = []
    for sgm in segments:           # 逐次生成されるので途中で止めれば残りは処理されない
        if max_seconds and sgm.start >= max_seconds:
            break
        out.append({"start": round(sgm.start, 3), "end": round(sgm.end, 3),
                    "text": sgm.text.strip()})
    if out_json:
        out_json.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# ---------------------------------------------------------------- 簡易VAD

def energy_vad(y: np.ndarray, sr: int = VAD_SR, hop_s: float = 0.02) -> list:
    """発話帯域(250-3500Hz)のエネルギーと帯域比による簡易発話区間推定。"""
    if len(y) < sr // 2:
        return []
    sos = signal.butter(4, [250, 3500], btype="band", fs=sr, output="sos")
    band = signal.sosfilt(sos, y)
    hop = int(sr * hop_s)
    n = len(y) // hop
    e_band = np.array([np.mean(band[i * hop:(i + 1) * hop] ** 2) for i in range(n)]) + 1e-12
    e_all = np.array([np.mean(y[i * hop:(i + 1) * hop] ** 2) for i in range(n)]) + 1e-12
    db = 10 * np.log10(e_band)
    ratio = e_band / e_all
    floor = np.percentile(db, 20)
    peak = np.percentile(db, 95)
    thr = floor + 0.45 * (peak - floor)
    active = (db > thr) & (ratio > 0.2)
    # 200msの平滑化(音節間の隙間を埋める) → 短すぎる区間を除去
    k = max(1, int(0.2 / hop_s))
    active = np.convolve(active.astype(float), np.ones(k) / k, mode="same") > 0.3
    segs, start = [], None
    for i, a in enumerate(active):
        if a and start is None:
            start = i
        elif not a and start is not None:
            segs.append([start * hop_s, i * hop_s])
            start = None
    if start is not None:
        segs.append([start * hop_s, n * hop_s])
    merged = []
    for s, e in segs:
        if merged and s - merged[-1][1] < 0.15:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [{"start": round(s, 3), "end": round(e, 3), "text": ""}
            for s, e in merged if e - s >= 0.25]


# ---------------------------------------------------------------- 強調音・オンセット

def onset_times(y: np.ndarray, sr: int = VAD_SR) -> np.ndarray:
    """スペクトルフラックスによる音の立ち上がり時刻(秒)。ナレーションの音節も含む。

    長い動画でもメモリを食わないよう、STFTはブロックごとに計算して1次元のフラックスだけ残す
    (signal.stft(boundary="zeros", padded=True) と同じフレーム配置・同じ結果)。
    """
    if len(y) < sr:
        return np.zeros(0)
    nperseg, hop, block = 1024, 256, 8192
    pad = nperseg // 2
    x = np.concatenate([np.zeros(pad, np.float32), np.asarray(y, np.float32),
                        np.zeros(pad, np.float32)])
    x = np.concatenate([x, np.zeros((-(len(x) - nperseg) % hop) % nperseg, np.float32)])
    n = (len(x) - nperseg) // hop + 1
    win = signal.get_window("hann", nperseg).astype(np.float32)
    win /= win.sum()
    frames = np.lib.stride_tricks.sliding_window_view(x, nperseg)[::hop]
    flux = np.empty(n - 1, np.float32)
    prev = None
    for s0 in range(0, n, block):
        mag = np.log1p(40 * np.abs(np.fft.rfft(frames[s0:s0 + block] * win, axis=1))).T
        if prev is not None:
            mag = np.concatenate([prev, mag], axis=1)
        d = np.maximum(0, np.diff(mag, axis=1)).sum(axis=0)
        o = s0 - 1 if prev is not None else 0
        flux[o:o + len(d)] = d
        prev = mag[:, -1:]
    t = np.arange(n) * hop / sr
    flux = flux / (np.percentile(flux, 99) + 1e-9)
    hop_t = hop / sr
    w = max(3, int(0.5 / hop_t))
    base = signal.medfilt(flux, kernel_size=w | 1)
    peaks, _ = signal.find_peaks(flux, height=base + 0.15, distance=max(1, int(0.1 / hop_t)))
    return t[1:][peaks]


def loud_events(t: np.ndarray, momentary: np.ndarray, integrated: float) -> list:
    """周囲2秒の中央値より8LU以上大きい瞬間(効果音・強調音の候補)。"""
    if len(t) < 10 or integrated is None:
        return []
    m = np.asarray(momentary)
    rad = 20  # 0.1秒刻み × 20 = ±2秒
    events = []
    last = -10.0
    for i in range(len(m)):
        lo, hi = max(0, i - rad), min(len(m), i + rad)
        med = np.median(m[lo:hi])
        if m[i] - med > 8.0 and m[i] > integrated - 10 and t[i] - last > 0.5:
            events.append(round(float(t[i]), 2))
            last = t[i]
    return events


# ---------------------------------------------------------------- 掛け合い(話者)推定

def _frames(x: np.ndarray, frame: int, hop: int) -> np.ndarray:
    n = 1 + max(0, (len(x) - frame) // hop)
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


def _mel_filterbank(sr: int, n_fft: int, n_mels: int = 26, fmin: float = 80, fmax: float = 7600):
    def hz2mel(f):
        return 2595 * np.log10(1 + f / 700)

    def mel2hz(m):
        return 700 * (10 ** (m / 2595) - 1)

    mels = np.linspace(hz2mel(fmin), hz2mel(min(fmax, sr / 2)), n_mels + 2)
    bins = np.floor((n_fft + 1) * mel2hz(mels) / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1))
    for m in range(1, n_mels + 1):
        a, b, c = bins[m - 1], bins[m], bins[m + 1]
        if b > a:
            fb[m - 1, a:b] = (np.arange(a, b) - a) / (b - a)
        if c > b:
            fb[m - 1, b:c] = (c - np.arange(b, c)) / (c - b)
    return fb


def voice_features(y: np.ndarray, sr: int, start: float, end: float):
    """区間の声の特徴: [log(基本周波数の中央値), MFCC平均(c1〜c12)]。短すぎ/無声ならNone。"""
    seg = y[int(start * sr):int(end * sr)]
    if len(seg) < int(0.3 * sr):
        return None
    frame, hop = int(0.04 * sr), int(0.02 * sr)
    fr = _frames(seg, frame, hop) * np.hanning(frame)[None, :]
    rms = np.sqrt((fr ** 2).mean(axis=1))
    voiced = rms > max(1e-4, 0.3 * np.median(rms[rms > 0]) if (rms > 0).any() else 1e-4)
    if voiced.sum() < 5:
        return None
    f = fr[voiced]
    spec = np.fft.rfft(f, n=2 * frame, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, :frame]
    lo, hi = int(sr / 400), int(sr / 65)
    lag = lo + np.argmax(ac[:, lo:hi], axis=1)
    strength = ac[np.arange(len(ac)), lag] / (ac[:, 0] + 1e-12)
    f0 = sr / lag[strength > 0.3]
    if len(f0) < 3:
        return None
    n_fft = 512
    fr2 = _frames(seg, n_fft, n_fft // 2) * np.hanning(n_fft)[None, :]
    pw = np.abs(np.fft.rfft(fr2, axis=1)) ** 2
    mel = np.log(pw @ _mel_filterbank(sr, n_fft).T + 1e-10)
    from scipy.fft import dct
    mfcc = dct(mel, type=2, axis=1, norm="ortho")[:, 1:13]
    return np.concatenate([[math.log(float(np.median(f0)))], mfcc.mean(axis=0)])


def dialogue_stats(y: np.ndarray, sr: int, segments: list, duration: float) -> dict:
    """発話区間を声の特徴で2グループに分け、掛け合いのテンポを推定する(話者分離の簡易版)。

    字幕の1行に2人の声が混ざることもあるので目安の値。自作動画も同じ方法で測るので比較には使える。
    """
    feats, segs = [], []
    for sgm in sorted(segments, key=lambda x: x["start"]):
        v = voice_features(y, sr, sgm["start"], sgm["end"])
        if v is not None:
            feats.append(v)
            segs.append(sgm)
    empty = {"n_voices": None, "turns_per_min": None, "main_voice_share": None,
             "turn_len_median": None}
    if len(feats) < 4:
        return empty
    X = np.array(feats)
    Xs = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-9)
    Xs[:, 0] *= 3.0     # 声の高さを重視(ナレーター/専門家役の区別に最も効く)
    import cv2
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-4)
    cv2.setRNGSeed(0)
    _, lab, cen = cv2.kmeans(Xs.astype(np.float32), 2, None, crit, 5, cv2.KMEANS_PP_CENTERS)
    lab = lab.ravel()
    # 2グループの声の高さの差が小さければ1人とみなす(半音で約2つ未満)
    f0_gap = abs(X[lab == 0, 0].mean() - X[lab == 1, 0].mean()) if len(set(lab)) == 2 else 0.0
    if f0_gap < math.log(2 ** (2 / 12)) or min(np.bincount(lab, minlength=2)) < 2:
        lab = np.zeros(len(lab), int)
    durs = np.array([s["end"] - s["start"] for s in segs])
    share = max(durs[lab == k].sum() for k in set(lab)) / durs.sum()
    turns = int((np.diff(lab) != 0).sum())
    runs, cur = [], durs[0]
    for i in range(1, len(lab)):
        if lab[i] == lab[i - 1]:
            cur += durs[i]
        else:
            runs.append(cur)
            cur = durs[i]
    runs.append(cur)
    return {
        "n_voices": int(len(set(lab))),
        "turns_per_min": round(turns / max(duration / 60, 1e-6), 2),
        "main_voice_share": round(float(share), 3),
        "turn_len_median": round(float(np.median(runs)), 2),
    }


def first_question_time(segments: list):
    """最初の「問いかけ」(？や「〜か」で終わる台詞)が始まる時刻。つかみの速さの目安。"""
    for sgm in sorted(segments, key=lambda x: x["start"]):
        t = sgm.get("text", "").strip()
        if t and (t.endswith(("？", "?", "…？", "か", "か。", "かな")) or "？" in t):
            return round(sgm["start"], 2)
    return None


# ---------------------------------------------------------------- まとめ

def speech_stats(segments: list, duration: float) -> dict:
    segs = sorted([(s["start"], s["end"], s.get("text", "")) for s in segments])
    if not segs:
        return {"speech_ratio": 0.0, "chars_per_sec": None, "gap_median": None,
                "gap_p90": None, "n_segments": 0}
    merged = []
    for s, e, txt in segs:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2] += txt
        else:
            merged.append([s, e, txt])
    speech_time = sum(e - s for s, e, _ in merged)
    chars = sum(len(JP_CHARS.findall(t)) for _, _, t in merged)
    gaps = [b[0] - a[1] for a, b in zip(merged, merged[1:]) if b[0] - a[1] > 0.05]
    return {
        "speech_ratio": round(speech_time / duration, 3) if duration else None,
        "chars_per_sec": round(chars / speech_time, 2) if chars and speech_time else None,
        "gap_median": round(float(np.median(gaps)), 3) if gaps else None,
        "gap_p90": round(float(np.percentile(gaps, 90)), 3) if gaps else None,
        "n_segments": len(merged),
    }


def bgm_relative_db(t: np.ndarray, momentary: np.ndarray, segments: list):
    """発話の無い「間」の音量 − 発話中の音量 (dB)。BGMの控えめさの指標。"""
    if not segments or len(t) == 0:
        return None
    t = np.asarray(t)
    m = np.asarray(momentary)
    in_speech = np.zeros(len(t), bool)
    in_gap = np.zeros(len(t), bool)
    segs = sorted((s["start"], s["end"]) for s in segments)
    for s, e in segs:
        in_speech |= (t >= s + 0.2) & (t <= e)
    for (s0, e0), (s1, _) in zip(segs, segs[1:]):
        if s1 - e0 >= 0.6:
            # ebur128の瞬時値は400ms窓なので、発話終わりの余韻を避けて内側だけ使う
            in_gap |= (t >= e0 + 0.45) & (t <= s1 - 0.05)
    valid = m > -70
    sp = m[in_speech & valid]
    gap_all = m[in_gap & ~in_speech]
    if len(sp) < 10 or len(gap_all) < 3:
        return None             # 判定に足る「間」が無い
    gp = gap_all[gap_all > -70]
    if len(gp) < 0.5 * len(gap_all):
        return -60.0            # 間がほぼ無音 = BGM無し
    return round(float(np.median(gp) - np.median(sp)), 2)


def analyze_audio(video_path, info: ff.VideoInfo, transcript: list = None) -> dict:
    if not info.has_audio:
        return {"has_audio": False}
    loud = ff.ebur128(video_path)
    y = ff.read_audio(video_path, sr=VAD_SR, channels=1)
    duration = info.duration or (len(y) / VAD_SR)
    source = "transcript"
    segments = transcript
    if not segments:
        segments = energy_vad(y)
        source = "energy_vad"
    onsets = onset_times(y)
    t, m = np.asarray(loud["t"]), np.asarray(loud["momentary"])
    silent = float(np.mean(m < -50)) if len(m) else None
    minutes = max(duration / 60.0, 1e-6)
    ev = loud_events(t, m, loud["integrated"])
    # レポート用に1秒刻みへ間引いた短期ラウドネス
    st = np.asarray(loud["short_term"])
    curve = [[round(float(t[i]), 1), round(float(st[i]), 1)] for i in range(0, len(t), 10)]
    return {
        "has_audio": True,
        "lufs_integrated": loud["integrated"], "lra": loud["lra"], "true_peak": loud["true_peak"],
        "silence_ratio": None if silent is None else round(silent, 3),
        "onsets_per_min": round(len(onsets) / minutes, 1),
        "loud_events": ev, "loud_events_per_min": round(len(ev) / minutes, 2),
        "speech_source": source,
        **speech_stats(segments, duration),
        "bgm_rel_db": bgm_relative_db(t, m, segments),
        "first_question_s": first_question_time(segments) if source == "transcript" else None,
        **dialogue_stats(y, VAD_SR, segments, duration),
        "loudness_curve": curve,
        "segments": segments,
    }
