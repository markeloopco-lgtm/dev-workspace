#!/usr/bin/env python3
"""音声を文字起こしする（無料・ローカル実行の Whisper。単語ごとの時刻つき）。

    python video_edit/transcribe.py --name <プロジェクト名> [--model large-v3-turbo] [--prompt "専門用語…"]

入力: work/<name>/audio16k.wav
出力: video_edit/projects/<name>/transcript.json
初回はモデル(約1.6GB)を Hugging Face から自動ダウンロードする。
"""

import argparse
import os
import time

from vedit_common import PROJECTS_DIR, WORK_DIR, log, save_json, setup_console, tc

# 「えー」「あのー」などの言いよどみも文字に起こさせるための書き出し例（Whisperは放っておくと省略しがち）
DEFAULT_PROMPT = "えー、こんにちは。あのー、今日はですね、えっと、お話ししていきたいと思います。"


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser(description="音声を単語の時刻つきで文字起こしする")
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--prompt", default="", help="専門用語・固有名詞など（認識精度が上がる）")
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args(argv)

    from faster_whisper import WhisperModel

    wav = WORK_DIR / args.name / "audio16k.wav"
    out = PROJECTS_DIR / args.name / "transcript.json"
    log(f"[文字起こし] モデル {args.model} を準備中…")
    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=args.threads)
    prompt = (args.prompt + "。" if args.prompt else "") + DEFAULT_PROMPT
    t0 = time.time()
    segs, info = model.transcribe(
        str(wav), language="ja", word_timestamps=True, beam_size=5, initial_prompt=prompt,
        condition_on_previous_text=False, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 250})
    result = []
    last = 0.0
    for s in segs:
        result.append({
            "id": len(result), "start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
            "words": [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word, "p": round(w.probability, 3)}
                      for w in (s.words or [])],
        })
        if s.end - last >= 60:
            last = s.end
            el = time.time() - t0
            log(f"[文字起こし] {tc(s.end)} / {tc(info.duration)} 経過{el / 60:.1f}分")
    save_json(out, {"model": args.model, "language": info.language, "duration": info.duration,
                    "prompt": prompt, "segments": result})
    log(f"[完了] {len(result)}区間 → {out}（{(time.time() - t0) / 60:.1f}分）")


if __name__ == "__main__":
    main()
