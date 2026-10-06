#!/usr/bin/env python3
"""VoiceStudio長文読み上げ: 長い台本を短く分割してAPIに送り、1つのWAVにつなぐ。

VoiceStudioの /v1/audio/speech は1回4,096文字までで、長いほど処理時間切れも
起きやすい。このスクリプトは句点（。！？）単位で短いかたまりに分け、1つずつ
生成してからつなぐ。途中で失敗しても、もう一度同じコマンドを実行すれば
生成済みのかたまりは飛ばして続きから再開する。

標準ライブラリのみ（追加インストール不要）。macOS / Windows / Linux で動く。

使い方:
  python3 voicestudio_long_tts.py 台本.txt 出力.wav
  python3 voicestudio_long_tts.py 台本.txt 出力.wav --voice <声のID>

台本の `# 見出し` 行は章の区切りとして扱い、読み上げない。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

# response_format=pcm は 24kHz / 16bit / モノラルの生データ（VoiceStudio API仕様）
SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2
CHANNELS = 1

SENTENCE_RE = re.compile(r"[^。！？!?\n]*[。！？!?]+[」』）)]*|[^。！？!?\n]+")
HEADING_RE = re.compile(r"^\s*#\s+")


def split_script(text: str, max_chars: int) -> list[tuple[str, str]]:
    """台本を (かたまり, 直後の区切り種別) のリストにする。

    区切り種別: "chapter"（見出し）/ "paragraph"（空行）/ "sentence"
    """
    chunks: list[tuple[str, str]] = []
    buf = ""

    def flush(kind: str) -> None:
        nonlocal buf
        if buf.strip():
            chunks.append((buf.strip(), kind))
        elif chunks and kind != "sentence":
            # 直前のかたまりの区切りを強い方に上書き
            prev_text, prev_kind = chunks[-1]
            order = ["sentence", "paragraph", "chapter"]
            if order.index(kind) > order.index(prev_kind):
                chunks[-1] = (prev_text, kind)
        buf = ""

    paragraphs = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for para in paragraphs:
        lines = []
        for line in para.split("\n"):
            if HEADING_RE.match(line):
                flush("chapter")
                continue
            if line.strip():
                lines.append(line.strip())
        for sentence in SENTENCE_RE.findall("\n".join(lines)):
            sentence = sentence.strip()
            if not sentence:
                continue
            # 1文が上限を超える場合は読点で、それでも長ければ機械的に切る
            pieces = [sentence]
            if len(sentence) > max_chars:
                pieces = _hard_split(sentence, max_chars)
            for piece in pieces:
                if buf and len(buf) + len(piece) > max_chars:
                    flush("sentence")
                buf += piece
        flush("paragraph")
    flush("chapter")
    return chunks


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    parts = re.findall(r"[^、，,]*[、，,]?", sentence)
    out: list[str] = []
    cur = ""
    for p in parts:
        if not p:
            continue
        while len(p) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            out.append(p[:max_chars])
            p = p[max_chars:]
        if cur and len(cur) + len(p) > max_chars:
            out.append(cur)
            cur = ""
        cur += p
    if cur:
        out.append(cur)
    return out


def synthesize(args: argparse.Namespace, text: str) -> bytes:
    body = {
        "model": args.model,
        "voice": args.voice,
        "input": text,
        "language": args.language,
        "response_format": "pcm",
        "speed": args.speed,
    }
    req = urllib.request.Request(
        args.url.rstrip("/") + "/v1/audio/speech",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        data = resp.read()
    if not data:
        raise RuntimeError("空の音声が返ってきました")
    if len(data) % SAMPLE_WIDTH:
        data = data[:-1]
    return data


def error_message(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            msg = detail.get("error", {}).get("message") or detail.get("detail")
        except Exception:
            msg = None
        return f"HTTP {exc.code}: {msg or exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"VoiceStudioに接続できません（アプリは起動していますか？）: {exc.reason}"
    return str(exc)


def main() -> int:
    ap = argparse.ArgumentParser(description="VoiceStudioで長い台本を読み上げて1つのWAVにする")
    ap.add_argument("script", type=Path, help="台本テキスト（UTF-8）")
    ap.add_argument("output", type=Path, help="出力WAVファイル")
    ap.add_argument("--voice", default="default", help="声のID（/v1/audio/voices で確認）")
    ap.add_argument("--model", default="omnivoice")
    ap.add_argument("--language", default="ja")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--url", default="http://localhost:3900")
    ap.add_argument("--max-chars", type=int, default=200, help="1回に送る最大文字数")
    ap.add_argument("--timeout", type=int, default=600, help="1回あたりの待ち時間（秒）")
    ap.add_argument("--retries", type=int, default=2, help="失敗時の再試行回数")
    ap.add_argument("--pause-sentence", type=int, default=250, help="文の間の無音（ms）")
    ap.add_argument("--pause-paragraph", type=int, default=600, help="段落の間の無音（ms）")
    ap.add_argument("--pause-chapter", type=int, default=1200, help="章の間の無音（ms）")
    ap.add_argument("--dry-run", action="store_true", help="分割結果だけ表示して終了")
    args = ap.parse_args()

    text = args.script.read_text(encoding="utf-8")
    chunks = split_script(text, args.max_chars)
    if not chunks:
        print("台本が空です", file=sys.stderr)
        return 1

    if args.dry_run:
        for i, (c, kind) in enumerate(chunks, 1):
            print(f"[{i}/{len(chunks)}] ({len(c)}字, 後ろ={kind}) {c}")
        return 0

    # かたまりごとの音声を保存しておき、再実行時は続きから
    work = args.output.with_name(args.output.stem + "_parts")
    work.mkdir(parents=True, exist_ok=True)
    total = len(chunks)
    started = time.time()
    for i, (chunk, _) in enumerate(chunks, 1):
        part = work / f"{i:04d}.pcm"
        meta = work / f"{i:04d}.txt"
        if part.exists() and meta.exists() and meta.read_text(encoding="utf-8") == chunk:
            print(f"[{i}/{total}] 生成済み → スキップ")
            continue
        for attempt in range(args.retries + 1):
            try:
                t0 = time.time()
                pcm = synthesize(args, chunk)
                part.write_bytes(pcm)
                meta.write_text(chunk, encoding="utf-8")
                print(f"[{i}/{total}] OK（{time.time() - t0:.0f}秒）{chunk[:30]}…")
                break
            except Exception as exc:  # noqa: BLE001
                msg = error_message(exc)
                if attempt < args.retries:
                    print(f"[{i}/{total}] 失敗、再試行します: {msg}")
                    time.sleep(3)
                else:
                    print(f"\n[{i}/{total}] で止まりました: {msg}", file=sys.stderr)
                    print(f"問題の文章: {chunk}", file=sys.stderr)
                    print("同じコマンドをもう一度実行すると、ここから再開します。", file=sys.stderr)
                    return 1

    pauses = {
        "sentence": args.pause_sentence,
        "paragraph": args.pause_paragraph,
        "chapter": args.pause_chapter,
    }
    bytes_per_ms = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS // 1000
    with wave.open(str(args.output), "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(SAMPLE_WIDTH)
        out.setframerate(SAMPLE_RATE)
        for i, (_, kind) in enumerate(chunks, 1):
            out.writeframes((work / f"{i:04d}.pcm").read_bytes())
            if i < total:
                out.writeframes(b"\x00" * (bytes_per_ms * pauses[kind]))

    with wave.open(str(args.output), "rb") as w:
        seconds = w.getnframes() / w.getframerate()
    print(f"\n完成: {args.output}（{int(seconds // 60)}分{int(seconds % 60)}秒、"
          f"処理{int((time.time() - started) // 60)}分）")
    print(f"途中ファイル {work} は不要なら削除してOKです。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
