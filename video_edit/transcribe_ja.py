#!/usr/bin/env python3
"""日本語専用の文字起こし（sherpa-onnx + ReazonSpeech。無料・ローカル・GPU不要・高速）。

    python video_edit/transcribe_ja.py --name <プロジェクト名> [--models work/asr_models]

transcribe.py（Whisper）と同じ形式の transcript.json を書くので、後の工程はそのまま使える。
Whisper のモデル置き場（Hugging Face）に届かない環境や、速さが欲しいときに使う。
74秒の音声がCPUで約3秒。句読点は出ないので、話の「間」で文を区切る。

必要なもの（初回だけ）:
    pip install sherpa-onnx
    モデル（約740MB）を GitHub の k2-fsa/sherpa-onnx の asr-models リリースから取得して --models に展開:
      sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01.tar.bz2
      silero_vad.onnx
"""

from __future__ import annotations

import argparse
import time
import wave
from pathlib import Path

import numpy as np

from vedit_common import PROJECTS_DIR, WORK_DIR, log, save_json, setup_console, tc

MODEL_DIR = "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01"
SR = 16000
MAX_CHUNK = 30.0      # 秒。長い区間はこれ以下に切る。短く切りすぎると文脈が減って言葉を落とす
MIN_CHUNK = 15.0      # 秒。切るときは最低これだけは残す（実測: 12秒で切ると抜けが増えた）
SENT_GAP = 0.55       # 秒。言葉と言葉の間がこれ以上空いたら文を区切る


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if w.getframerate() != SR or w.getnchannels() != 1:
            raise SystemExit(f"{path} は 16kHz モノラルではありません（prepare.py で作り直してください）")
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0


def speech_regions(x: np.ndarray, vad_model: Path):
    """発話区間を [(開始サンプル, 終了サンプル), ...] で返す"""
    import sherpa_onnx
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(vad_model)
    cfg.silero_vad.min_silence_duration = 0.25
    cfg.silero_vad.min_speech_duration = 0.2
    cfg.silero_vad.threshold = 0.5
    cfg.sample_rate = SR
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=600)
    out = []

    def drain():
        while not vad.empty():
            st = vad.front.start
            out.append((st, st + len(vad.front.samples)))
            vad.pop()

    win = cfg.silero_vad.window_size
    for i in range(0, len(x), win):
        vad.accept_waveform(x[i:i + win])
        drain()
    vad.flush()
    drain()
    return out


def split_long(x: np.ndarray, a: int, b: int):
    """長い区間を、MIN_CHUNK〜MAX_CHUNK の範囲で一番音の小さい所で切る"""
    hop = int(SR * 0.02)
    out = []
    while (b - a) / SR > MAX_CHUNK:
        lo, hi = a + int(MIN_CHUNK * SR), a + int(MAX_CHUNK * SR)
        frames = [(float(np.sqrt(np.mean(x[i:i + hop] ** 2))), i) for i in range(lo, hi - hop, hop)]
        cut = min(frames)[1] + hop // 2
        out.append((a, cut))
        a = cut
    out.append((a, b))
    return out


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser(description="日本語専用の文字起こし（ReazonSpeech）")
    ap.add_argument("--name", required=True)
    ap.add_argument("--models", type=Path, default=WORK_DIR / "asr_models", help="モデルを置いたフォルダ")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args(argv)

    import sherpa_onnx
    md = args.models / MODEL_DIR
    vad_model = args.models / "silero_vad.onnx"
    for p in (md / "tokens.txt", vad_model):
        if not p.exists():
            raise SystemExit(f"モデルがありません: {p}\n  このファイルの先頭の説明に従って取得してください。")
    wav = WORK_DIR / args.name / "audio16k.wav"
    out = PROJECTS_DIR / args.name / "transcript.json"
    x = read_wav(wav)
    dur = len(x) / SR

    log("[文字起こし] ReazonSpeech を準備中…")
    rec = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(md / "encoder-epoch-99-avg-1.int8.onnx"), decoder=str(md / "decoder-epoch-99-avg-1.onnx"),
        joiner=str(md / "joiner-epoch-99-avg-1.int8.onnx"), tokens=str(md / "tokens.txt"),
        num_threads=args.threads, decoding_method="greedy_search")
    t0 = time.time()
    chunks = [c for a, b in speech_regions(x, vad_model) for c in split_long(x, a, b)]

    # 区間ごとに認識し、言葉ごとの時刻を元動画の時刻に直す
    words = []
    for a, b in chunks:
        st = rec.create_stream()
        st.accept_waveform(SR, x[a:b])
        rec.decode_stream(st)
        r = st.result
        toks, ts = list(r.tokens or []), list(r.timestamps or [])
        # 区間の最初の1文字だけ区間の頭に付き、残りが数秒後に続くことがある（「こ」…「れね」）。
        # 1文字目は次の文字の直前に寄せる
        if len(ts) >= 2 and ts[1] - ts[0] > 0.8:
            ts[0] = ts[1] - 0.12
        base, end = a / SR, b / SR
        for i, (tok, t) in enumerate(zip(toks, ts)):
            s = base + float(t)
            e = base + float(ts[i + 1]) if i + 1 < len(ts) else min(end, s + 0.3)
            e = max(e, s + 0.04)
            words.append({"start": round(s, 3), "end": round(min(e, s + 0.6), 3), "word": tok, "p": 0.9,
                          "chunk_end": i + 1 == len(toks)})

    # 間が空いた所・区間の切れ目で文を区切る
    segs, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None or w["chunk_end"] or nxt["start"] - w["end"] >= SENT_GAP:
            segs.append({"id": len(segs), "start": cur[0]["start"], "end": cur[-1]["end"],
                         "text": "".join(t["word"] for t in cur),
                         "words": [{k: t[k] for k in ("start", "end", "word", "p")} for t in cur]})
            cur = []
    # 2文字以下の断片（「て」「い」など、時刻だけ少し離れた語尾）は直前の文にくっつける
    merged = []
    for sg in segs:
        if merged and len(sg["text"]) <= 2:
            prev = merged[-1]
            prev["text"] += sg["text"]
            prev["words"] += sg["words"]
            prev["end"] = sg["end"]
        else:
            merged.append(sg)
    if merged and len(merged[0]["text"]) <= 2 and len(merged) > 1:   # 先頭の断片は次の文へ
        first = merged.pop(0)
        merged[0]["text"] = first["text"] + merged[0]["text"]
        merged[0]["words"] = first["words"] + merged[0]["words"]
        merged[0]["start"] = first["start"]
    segs = [dict(sg, id=i) for i, sg in enumerate(merged)]
    save_json(out, {"model": "reazonspeech-zipformer-2024-08-01", "language": "ja", "duration": dur,
                    "prompt": "", "segments": segs})
    log(f"[完了] {len(segs)}区間 / {tc(dur)}の音声を {time.time() - t0:.1f}秒で → {out}")


if __name__ == "__main__":
    main()
