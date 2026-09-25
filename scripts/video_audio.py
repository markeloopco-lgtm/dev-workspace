#!/usr/bin/env python3
"""動画の音声の編集的特徴を測る。

- ラウドネス: EBU R128 (YouTubeの音量正規化と同じ尺度)
- 発話と無音: 声の区間と、その間の「間」の長さ(ジェットカットの詰め具合)
- 効果音: 立ち上がりの鋭い音(オンセット)が、カットやテロップ出現と同時に鳴っている割合
- BGM: 声が途切れた所に残っている音の大きさと音色(音楽か雑音か)
"""

import numpy as np

import video_io

SR = 16000
HOP = 160   # 10ms
WIN = 400   # 25ms


def _frames(y, win, hop):
    n = 1 + max(0, (len(y) - win) // hop)
    return np.lib.stride_tricks.as_strided(y, shape=(n, win), strides=(y.strides[0] * hop, y.strides[0]),
                                           writeable=False)


def _runs(flags):
    d = np.diff(np.concatenate([[0], flags.astype(np.int8), [0]]))
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0] - 1))


def _fill_short(flags, value, max_len):
    """flagsのうちvalueの連続区間でmax_len以下のものを反転する"""
    out = flags.copy()
    for a, b in _runs(flags == value):
        if b - a + 1 <= max_len:
            out[a:b + 1] = not value
    return out


def rms_db(y):
    fr = _frames(y, WIN, HOP)
    return 20 * np.log10(np.sqrt((fr.astype(np.float64) ** 2).mean(axis=1)) + 1e-9)


def voice_activity(db):
    """発話区間の推定。背景(BGM)の床と声の大きさの中間をしきい値にする"""
    floor = float(np.percentile(db, 10))
    speech = float(np.percentile(db, 80))
    if speech - floor < 6:
        return None, floor, speech, None
    thr = floor + 0.45 * (speech - floor)
    act = db > thr
    act = _fill_short(act, False, 4)   # 40ms以下の途切れは声の一部
    act = _fill_short(act, True, 4)    # 40ms以下の音はノイズ
    return act, floor, speech, thr


def onset_strength(y, n_fft=512):
    """スペクトルフラックス(音の立ち上がりの強さ) 10ms刻み"""
    win = np.hanning(n_fft).astype(np.float32)
    fr = _frames(y, n_fft, HOP)
    flux = np.zeros(len(fr), np.float32)
    prev = None
    for a in range(0, len(fr), 8192):
        spec = np.log1p(10 * np.abs(np.fft.rfft(fr[a:a + 8192] * win, axis=1)))
        if prev is not None:
            spec_prev = np.vstack([prev[None], spec[:-1]])
        else:
            spec_prev = np.vstack([spec[:1], spec[:-1]])
        flux[a:a + len(spec)] = np.maximum(spec - spec_prev, 0).sum(axis=1)
        prev = spec[-1]
    return flux


def pick_onsets(flux, min_gap=5):
    """ロバストz値つきでオンセットのピークを拾う"""
    med = float(np.median(flux))
    mad = float(np.median(np.abs(flux - med))) + 1e-6
    z = (flux - med) / (1.4826 * mad)
    peaks = []
    for i in range(1, len(z) - 1):
        if z[i] >= 4 and z[i] >= z[max(0, i - min_gap):i + min_gap + 1].max():
            if not peaks or i - peaks[-1] > min_gap:
                peaks.append(i)
    return np.array(peaks, int), z


def spectral_flatness(y_seg, n_fft=1024):
    if len(y_seg) < n_fft:
        return None
    fr = _frames(y_seg, n_fft, n_fft // 2) * np.hanning(n_fft)
    p = np.abs(np.fft.rfft(fr, axis=1)) ** 2 + 1e-12
    return float(np.median(np.exp(np.log(p).mean(axis=1)) / p.mean(axis=1)))


def analyze(info, visual_events: dict, fps: float) -> dict:
    """visual_events: {"cut": [秒,...], "overlay_in": [...], ...} 効果音の同期率を種類別に出す"""
    y = video_io.load_audio(info, SR)
    if y is None or len(y) < SR:
        return {"present": False}
    out = {"present": True, "duration": round(len(y) / SR, 2)}
    loud = video_io.loudness(info)
    out["loudness"] = {k: loud.get(k) for k in ("integrated_lufs", "lra_lu", "true_peak_dbfs")}
    out["loudness_curve"] = {"t": loud.get("t", []), "momentary": loud.get("momentary", [])}

    db = rms_db(y)
    t_of = lambda i: i * HOP / SR
    act, floor, speech, thr = voice_activity(db)
    out["levels"] = {"floor_dbfs": round(floor, 1), "speech_dbfs": round(speech, 1)}
    gaps = []
    if act is not None:
        segs = _runs(act)
        out["speech_ratio"] = round(float(act.mean()), 3)
        # 先頭・末尾の無音は編集の「間」ではないので除く
        for (a0, a1), (b0, b1) in zip(segs[:-1], segs[1:]):
            g = (a1 + 1, b0 - 1)
            dur = (g[1] - g[0] + 1) * HOP / SR
            if dur >= 0.08:
                gaps.append({"start": round(t_of(g[0]), 3), "end": round(t_of(g[1] + 1), 3), "dur": round(dur, 3)})
        out["speech_segments"] = [[round(t_of(a), 3), round(t_of(b + 1), 3)] for a, b in segs]
    out["gaps"] = gaps
    if gaps:
        d = np.array([g["dur"] for g in gaps])
        out["gap_stats"] = {
            "count": len(d), "per_min": round(len(d) / (len(y) / SR / 60), 1),
            "median": round(float(np.median(d)), 3), "p10": round(float(np.percentile(d, 10)), 3),
            "p90": round(float(np.percentile(d, 90)), 3),
            "share_le_0_3s": round(float((d <= 0.3).mean()), 3),
        }

    # BGM: 声の途切れた所に何が鳴っているか
    bed = []
    for g in gaps:
        if g["dur"] >= 0.12:
            a, b = int((g["start"] + 0.02) * SR), int((g["end"] - 0.02) * SR)
            if b > a:
                bed.append(y[a:b])
    if bed:
        bed_y = np.concatenate(bed)
        bed_db = 20 * np.log10(np.sqrt(float((bed_y.astype(np.float64) ** 2).mean())) + 1e-9)
        flat = spectral_flatness(bed_y)
        per_gap = [20 * np.log10(np.sqrt(float((s.astype(np.float64) ** 2).mean())) + 1e-9) for s in bed]
        present = bed_db > -55
        out["bgm"] = {
            "present": bool(present),
            "level_dbfs": round(bed_db, 1),
            "below_speech_db": round(speech - bed_db, 1),
            "flatness": None if flat is None else round(flat, 3),
            "tonal": None if flat is None else bool(flat < 0.3),
            "continuity": round(float(np.mean(np.abs(np.array(per_gap) - bed_db) < 6)), 3),
        }

    # 効果音: 強いオンセットとカット・テロップの同時性
    flux = onset_strength(y)
    peaks, z = pick_onsets(flux)
    if len(peaks):
        # 立ち上がりのピークの中で飛び抜けて強いもの(中央値+3MAD)を効果音候補とする
        pz = z[peaks]
        med = float(np.median(pz))
        mad = float(np.median(np.abs(pz - med))) + 1e-6
        strong_thr = max(8.0, med + 3 * 1.4826 * mad)
        strong = peaks[pz >= strong_thr]
    else:
        strong_thr, strong = 8.0, peaks
    st_t = strong * HOP / SR
    out["onsets"] = {"strong_threshold_z": round(strong_thr, 1), "strong_count": int(len(strong)),
                     "strong_times": [round(float(t), 3) for t in st_t]}
    pre, post = 2.0 / fps + 0.02, 3.0 / fps + 0.02   # 映像イベントの2フレーム前〜3フレーム後
    dur = len(y) / SR
    sync = {}
    for name, times in visual_events.items():
        times = [t for t in times if 0.2 < t < dur - 0.2]
        if not times:
            continue
        hit = [bool(np.any((st_t >= t - pre) & (st_t <= t + post))) for t in times]
        sync[name] = {"events": len(times), "with_se": int(sum(hit)), "rate": round(float(np.mean(hit)), 3)}
    # 偶然一致する確率(強いオンセットがランダムな時刻の窓に入る確率)
    rng = np.random.default_rng(0)
    rand_t = rng.uniform(0.2, max(0.3, dur - 0.2), 2000)
    chance = float(np.mean([np.any((st_t >= t - pre) & (st_t <= t + post)) for t in rand_t])) if len(st_t) else 0.0
    out["se_sync"] = {"by_event": sync, "chance_rate": round(chance, 3)}
    if len(strong):
        # 効果音の大きさ: 強いオンセット直後30msのRMSと声の大きさの差
        lv = []
        for i in strong:
            seg = y[i * HOP:i * HOP + int(0.03 * SR)]
            if len(seg):
                lv.append(20 * np.log10(np.sqrt(float((seg.astype(np.float64) ** 2).mean())) + 1e-9))
        out["se_level_vs_speech_db"] = round(float(np.median(lv)) - speech, 1) if lv else None
    return out
