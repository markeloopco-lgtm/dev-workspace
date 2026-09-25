"""音声ミックス: ナレーション + BGM(ダッキング) + 効果音 → ラウドネス正規化 → リミッター。

音量の目標はスタイルプロファイルから取る:
  audio.bgm_rel_db       … 台詞の無い「間」でのBGM音量(ナレーション比)
  audio.lufs_integrated  … 完成品の統合ラウドネス(YouTubeは-14LUFS付近に揃えて再生する)
"""

import math
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile as sf
from scipy import ndimage, signal

from .. import ffmpeg_util as ff

SR = 48000


def resample(y: np.ndarray, sr: int, target: int = SR) -> np.ndarray:
    if sr == target:
        return y.astype(np.float32)
    g = math.gcd(sr, target)
    return signal.resample_poly(y, target // g, sr // g, axis=0).astype(np.float32)


def loudness(y: np.ndarray, sr: int = SR):
    """統合ラウドネス(LUFS)。0.4秒未満や無音はRMSから概算する。"""
    if y.ndim == 1:
        y = np.stack([y, y], axis=1)
    if len(y) == 0 or np.max(np.abs(y)) < 1e-6:
        return None
    if len(y) < int(0.5 * sr):
        rms = float(np.sqrt(np.mean(y ** 2)))
        return 20 * math.log10(rms + 1e-9) - 0.691 + 3.0
    val = pyloudnorm.Meter(sr).integrated_loudness(y)
    return None if not np.isfinite(val) else float(val)


def _true_peaks(y: np.ndarray, block: int = SR * 10, pad: int = 64) -> np.ndarray:
    """各サンプル位置のトゥルーピーク(4倍オーバーサンプリング)。メモリ節約のため分割処理。"""
    n = len(y)
    out = np.empty(n, np.float32)
    for s0 in range(0, n, block):
        a, b = max(0, s0 - pad), min(n, s0 + block + pad)
        os_ = np.abs(signal.resample_poly(y[a:b], 4, 1, axis=0)).max(axis=1)
        os_ = os_[: (b - a) * 4].reshape(-1, 4).max(axis=1)
        e = min(n, s0 + block)
        out[s0:e] = os_[s0 - a:s0 - a + (e - s0)]
    return np.maximum(out, np.abs(y).max(axis=1))


def true_peak_limit(y: np.ndarray, ceiling_db: float = -1.0, sr: int = SR) -> np.ndarray:
    """トゥルーピークを見て、先読み付きのゲイン低減をかける(ブリックウォール・リミッター)。"""
    lim = 10 ** (ceiling_db / 20)
    need = np.minimum(1.0, lim / np.maximum(_true_peaks(y), 1e-9))
    if need.min() >= 1.0:
        return np.clip(y, -0.999, 0.999)
    win = max(1, int(0.003 * sr))
    g = ndimage.minimum_filter1d(need, size=4 * win + 1)
    g = np.convolve(g, np.ones(win) / win, mode="same")
    g = np.minimum(g, ndimage.minimum_filter1d(need, size=2 * win + 1))
    out = y * g[:, None].astype(np.float32)
    return np.clip(out, -0.999, 0.999)


def _envelope(active: np.ndarray, sr: int, attack: float, release: float) -> np.ndarray:
    """0/1の発話区間を、立ち上がりattack秒・戻りrelease秒でなめらかにする。"""
    step = max(1, int(sr * 0.01))
    a = active[::step].astype(np.float32)
    out = np.zeros_like(a)
    ka, kr = 0.01 / max(attack, 1e-3), 0.01 / max(release, 1e-3)
    v = 0.0
    for i, x in enumerate(a):
        v += (x - v) * (ka if x > v else kr)
        out[i] = v
    return np.repeat(out, step)[: len(active)]


def _trim_quiet(y: np.ndarray, thr_db: float = -45.0) -> np.ndarray:
    """曲の頭と終わりの無音(フェードの消え際)を切る。ループのつなぎ目で音が途切れないように。"""
    env = np.abs(y).max(axis=1)
    peak = env.max() if len(env) else 0.0
    if peak <= 0:
        return y
    idx = np.where(env > peak * 10 ** (thr_db / 20))[0]
    return y[idx[0]:idx[-1] + 1] if len(idx) else y


def loop_to(y: np.ndarray, n: int, xf_s: float = 2.0, sr: int = SR) -> np.ndarray:
    """BGMを長さnまでループする。つなぎ目は等パワーのクロスフェード(ブツ切れ・無音を防ぐ)。"""
    if len(y) >= n:
        return y[:n].copy()
    y = _trim_quiet(y)
    xf = max(1, min(int(xf_s * sr), len(y) // 4))
    th = np.linspace(0, np.pi / 2, xf, dtype=np.float32)[:, None]
    fade_in, fade_out = np.sin(th), np.cos(th)
    out = np.zeros((n + len(y), y.shape[1]), np.float32)
    pos, k = 0, 0
    while pos < n:
        seg = y.copy()
        if k:
            seg[:xf] *= fade_in
        seg[-xf:] *= fade_out
        out[pos:pos + len(y)] += seg
        pos += len(y) - xf
        k += 1
    return out[:n]


def mix(plan, style: dict, out_wav: Path, duck_db: float = 4.0) -> dict:
    n = int(math.ceil(plan.duration * SR)) + SR // 10
    speech = np.zeros((n, 2), np.float32)
    active = np.zeros(n, bool)
    for ln in plan.lines:
        y = resample(ln.audio, ln.sr)
        a = int(ln.start * SR)
        b = min(n, a + len(y))
        speech[a:b] += y[: b - a, None]
        active[a:b] = True
    L_speech = loudness(speech) or -20.0

    bgm_rel = style.get("audio", {}).get("bgm_rel_db")
    bgm_rel = -18.0 if bgm_rel is None or bgm_rel <= -50 else float(bgm_rel)
    music = np.zeros((n, 2), np.float32)
    duck = _envelope(active, SR, 0.12, 0.45)
    warnings = []
    for bg in plan.bgm:
        y = ff.read_audio(bg["file"], sr=SR, channels=2)
        if len(y) == 0:
            warnings.append(f"BGMの音声を読めませんでした(無視します): {bg['file']}")
            continue
        a, b = int(bg["start"] * SR), min(n, int(bg["end"] * SR))
        seg_len = b - a
        if seg_len <= 0:
            continue
        track = loop_to(y, seg_len)
        fade = min(int(1.5 * SR), seg_len // 3)
        ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
        track[:fade] *= ramp
        track[-fade:] *= ramp[::-1]
        rel = float(bg["volume_db"]) if bg.get("volume_db") is not None else bgm_rel
        L_b = loudness(track) or -30.0
        gain = 10 ** ((L_speech + rel - L_b) / 20)
        g_duck = 10 ** (-duck_db * duck[a:b] / 20)
        music[a:b] += track * gain * g_duck[:, None]

    effects = np.zeros((n, 2), np.float32)
    for se in plan.se:
        y = ff.read_audio(se["file"], sr=SR, channels=2)
        if len(y) == 0:
            warnings.append(f"効果音の音声を読めませんでした(無視します): {se['file']}")
            continue
        a = int(se["t"] * SR)
        off = max(0, -a)                 # 動画の開始より前に始まる効果音は頭を切る
        a = max(0, a)
        b = min(n, a + len(y) - off)
        if b <= a:
            continue
        rel = float(se["volume_db"]) if se.get("volume_db") is not None else -4.0
        L_e = loudness(y) or -20.0
        effects[a:b] += y[off:off + b - a] * 10 ** ((L_speech + rel - L_e) / 20)

    out = speech + music + effects
    target = style.get("audio", {}).get("lufs_integrated")
    target = -14.0 if target is None else float(target)
    L_all = loudness(out)
    if L_all is not None:
        out = out * 10 ** ((target - L_all) / 20)
    out = true_peak_limit(out, -1.0)
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), out, SR, subtype="PCM_24")
    for wmsg in warnings:
        print(f"      警告: {wmsg}", flush=True)
    return {"speech_lufs": round(L_speech, 2), "final_lufs": _r(loudness(out)), "warnings": warnings,
            "target_lufs": target, "bgm_rel_db": bgm_rel}


def _r(x):
    return None if x is None else round(x, 2)
