"""ナレーション音声合成(TTS)。すべて無料・ローカルで動くエンジンに対応。

  voicevox : VOICEVOX ENGINE (既定 http://127.0.0.1:50021) — 生成音声の公開時は「VOICEVOX:キャラ名」表記が必要
  aivis    : AivisSpeech Engine (VOICEVOX互換API、既定 http://127.0.0.1:10101)
  sbv2     : Style-Bert-VITS2 server_fastapi.py (既定 http://127.0.0.1:5000)
  dummy    : 音声エンジン無しで動作確認するための仮音声(話速どおりの長さの小さな「ざわざわ音」)

合成結果は renders/cache/tts/ にキャッシュする(同じ台詞・同じ設定なら再合成しない)。
"""

import hashlib
import io
import json
import math
import re
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

DEFAULT_URLS = {
    "voicevox": "http://127.0.0.1:50021",
    "aivis": "http://127.0.0.1:10101",
    "sbv2": "http://127.0.0.1:5000",
}
COUNT_CHARS = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟA-Za-z0-9０-９]")

# プロキシ設定があってもローカルのエンジンには直接つなぐ
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TTSError(RuntimeError):
    pass


def _http(url: str, data: bytes = None, headers: dict = None, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001 - 接続失敗の原因をそのまま伝える
        raise TTSError(f"音声エンジンに接続できません: {url.split('?')[0]}\n  ({e})\n"
                       "  エンジン(VOICEVOX / AivisSpeech / Style-Bert-VITS2)を起動しているか確認してください。"
                       " 音声無しで試すなら --tts dummy") from e


def count_chars(text: str) -> int:
    return len(COUNT_CHARS.findall(text))


def _decode_wav(data: bytes):
    y, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    return y.mean(axis=1), sr


class VoicevoxTTS:
    """VOICEVOX / AivisSpeech (同じAPI)。"""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def synth(self, text: str, cfg: dict):
        speaker = int(cfg.get("speaker", 13))
        q = _http(f"{self.base}/audio_query?" + urllib.parse.urlencode(
            {"text": text, "speaker": speaker}), data=b"")
        query = json.loads(q)
        query["speedScale"] = float(cfg.get("_speed", 1.0))
        query["pitchScale"] = float(cfg.get("pitch", 0.0))
        query["intonationScale"] = float(cfg.get("intonation", 1.0))
        query["volumeScale"] = float(cfg.get("volume", 1.0))
        query["prePhonemeLength"] = float(cfg.get("pre", 0.05))
        query["postPhonemeLength"] = float(cfg.get("post", 0.05))
        wav = _http(f"{self.base}/synthesis?speaker={speaker}",
                    data=json.dumps(query).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
        return _decode_wav(wav)

    def speakers(self) -> list:
        """[(id, 'キャラ名(スタイル名)')]"""
        data = json.loads(_http(f"{self.base}/speakers", timeout=10))
        out = []
        for sp in data:
            for st in sp.get("styles", []):
                out.append((st["id"], f"{sp['name']}({st['name']})"))
        return out

    def credit_name(self, cfg: dict) -> str:
        try:
            sid = int(cfg.get("speaker", 13))
            for i, name in self.speakers():
                if i == sid:
                    return name.split("(")[0]
        except Exception:
            pass
        return f"speaker {cfg.get('speaker')}"


class SBV2TTS:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def synth(self, text: str, cfg: dict):
        params = {
            "text": text, "model_id": int(cfg.get("model_id", 0)),
            "speaker_id": int(cfg.get("speaker_id", 0)), "style": cfg.get("style", "Neutral"),
            "length": round(1.0 / max(0.3, float(cfg.get("_speed", 1.0))), 3),
            "language": "JP",
        }
        if "style_weight" in cfg:
            params["style_weight"] = float(cfg["style_weight"])
        wav = _http(f"{self.base}/voice?" + urllib.parse.urlencode(params))
        return _decode_wav(wav)

    def credit_name(self, cfg: dict) -> str:
        return cfg.get("credit", f"Style-Bert-VITS2 model {cfg.get('model_id', 0)}")


class DummyTTS:
    """エンジン無しの仮音声。長さ = 文字数 ÷ 話速(文字/秒)。"""

    def __init__(self, cps: float = 7.0, sr: int = 24000):
        self.cps, self.sr = cps, sr

    def synth(self, text: str, cfg: dict):
        n = max(1, count_chars(text))
        dur = n / (self.cps * float(cfg.get("_speed", 1.0))) + 0.1
        t = np.arange(int(dur * self.sr)) / self.sr
        seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        spk = str(cfg.get("speaker", 0)).encode("utf-8")
        base = 110 + 40 * (int(hashlib.md5(spk).hexdigest()[:6], 16) % 5)
        voiced = sum((1.0 / k) * np.sin(2 * math.pi * base * k * t + rng.random() * 6)
                     for k in range(1, 12))
        syll = 0.5 + 0.5 * np.sin(2 * math.pi * self.cps * 0.8 * t) ** 2
        env = np.minimum(1, np.minimum(t, dur - t) / 0.05).clip(0, 1)
        return (0.08 * voiced * syll * env).astype(np.float32), self.sr

    def credit_name(self, cfg: dict) -> str:
        return "(仮音声)"


def make_engine(engine: str, url: str = None, cps: float = 7.0):
    if engine in ("voicevox", "aivis"):
        return VoicevoxTTS(url or DEFAULT_URLS[engine])
    if engine == "sbv2":
        return SBV2TTS(url or DEFAULT_URLS["sbv2"])
    if engine == "dummy":
        return DummyTTS(cps=cps)
    raise ValueError(f"未知のTTSエンジン: {engine} (voicevox / aivis / sbv2 / dummy)")


class Narrator:
    """話者設定ごとにエンジンを使い分け、キャッシュ付きで合成する。"""

    def __init__(self, voices: dict, cache_dir: Path, force_engine: str = None,
                 target_cps: float = None):
        self.voices = voices or {}
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.force_engine = force_engine
        self.target_cps = target_cps
        self._engines = {}

    def voice_cfg(self, speaker: str) -> dict:
        cfg = dict(self.voices.get(speaker) or self.voices.get("default") or {"engine": "voicevox"})
        if self.force_engine:
            cfg["engine"] = self.force_engine
        return cfg

    def engine_for(self, cfg: dict):
        key = (cfg.get("engine", "voicevox"), cfg.get("url"))
        if key not in self._engines:
            self._engines[key] = make_engine(key[0], key[1], cps=self.target_cps or 7.0)
        return self._engines[key]

    def synth(self, speaker: str, text: str, speed: float = 1.0):
        cfg = self.voice_cfg(speaker)
        cfg["_speed"] = round(float(speed), 4)
        key = json.dumps({"t": text, "c": cfg}, ensure_ascii=False, sort_keys=True)
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        path = self.cache_dir / f"{h}.wav"
        if path.exists():
            y, sr = sf.read(str(path), dtype="float32")
            return y, sr
        y, sr = self.engine_for(cfg).synth(text, cfg)
        y = trim_silence(y, sr)
        sf.write(str(path), y, sr)
        return y, sr

    def credit(self, speaker: str) -> str:
        cfg = self.voice_cfg(speaker)
        name = cfg.get("credit") or self.engine_for(cfg).credit_name(cfg)
        eng = cfg.get("engine", "voicevox")
        if eng == "voicevox":
            return f"VOICEVOX:{name}"
        if eng == "aivis":
            return f"AivisSpeech:{name}"
        return name


def trim_silence(y: np.ndarray, sr: int, thr_db: float = -45.0, keep: float = 0.03) -> np.ndarray:
    """前後の無音を詰める(台詞間の「間」はタイムライン側で制御するため)。"""
    if len(y) == 0:
        return y
    win = max(1, int(0.01 * sr))
    env = np.sqrt(np.convolve(y ** 2, np.ones(win) / win, mode="same"))
    peak = env.max() if env.max() > 0 else 1.0
    idx = np.where(20 * np.log10(env / peak + 1e-12) > thr_db)[0]
    if len(idx) == 0:
        return y
    a = max(0, idx[0] - int(keep * sr))
    b = min(len(y), idx[-1] + int(keep * sr))
    return y[a:b]
