#!/usr/bin/env python3
"""Claude Code のローカル会話ログから「自分が入力したプロンプト」だけを時系列で抜き出す。

Claude Code (CLI / デスクトップ) は会話ログを次の場所に JSONL で保存する:
  macOS / Linux : ~/.claude/projects/<プロジェクト名>/<セッションID>.jsonl
  Windows       : %USERPROFILE%\\.claude\\projects\\<プロジェクト名>\\<セッションID>.jsonl

使い方 (例: LINE構築に関係するセッションだけを 1 本の Markdown にまとめる):
  python3 scripts/extract_claude_prompts.py --grep LINE --grep タナカ --grep 診断 -o tanaka_line_prompts.md

  --grep     指定した語をどれか 1 つでも含むセッションだけを対象にする (複数指定可、大文字小文字は無視)
  --since    この日付以降のセッションだけ (例: 2026-08-01)
  --until    この日付以前のセッションだけ
  --root     ログの置き場所を変えたいとき (既定: ~/.claude/projects)
  --session  セッションID (ファイル名) の一部を指定して 1 本だけ出す
  -o         出力先 Markdown。省略時は標準出力

出力: セッションごとに見出しを付け、ユーザー入力を古い順に番号付きで並べた Markdown。
ツール実行結果や /コマンド など「人が打っていない」ユーザーメッセージは除外する。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
COMMAND_TAG = re.compile(r"<command-name>|<local-command-stdout>|<system-reminder>", re.S)


def iter_text(content) -> str | None:
    """user メッセージの content から人間が打った本文だけを返す。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        if parts:
            return "\n".join(parts)
    return None


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_session(path: Path) -> dict:
    prompts: list[tuple[datetime | None, str]] = []
    summary = None
    cwd = None
    first_ts = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "summary" and rec.get("summary"):
                summary = rec["summary"]
                continue
            if rec.get("type") != "user" or rec.get("isMeta"):
                continue
            msg = rec.get("message") or {}
            if msg.get("role") != "user":
                continue
            text = iter_text(msg.get("content"))
            if not text or COMMAND_TAG.search(text):
                continue
            text = text.strip()
            if not text:
                continue
            ts = parse_ts(rec.get("timestamp"))
            if first_ts is None and ts is not None:
                first_ts = ts
            cwd = cwd or rec.get("cwd")
            prompts.append((ts, text))
    return {
        "path": path,
        "session_id": path.stem,
        "summary": summary,
        "cwd": cwd,
        "started": first_ts,
        "prompts": prompts,
    }


def matches(session: dict, needles: list[str]) -> bool:
    if not needles:
        return True
    hay = "\n".join(t for _, t in session["prompts"])
    hay = (hay + "\n" + (session["summary"] or "")).lower()
    return any(n.lower() in hay for n in needles)


def fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "時刻不明"
    return ts.astimezone().strftime("%Y-%m-%d %H:%M")


def render(sessions: list[dict], needles: list[str]) -> str:
    out = ["# Claude Code 入力プロンプト記録", ""]
    out.append(f"抽出日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if needles:
        out.append(f"絞り込み語: {', '.join(needles)}")
    out.append(f"対象セッション数: {len(sessions)}")
    out.append("")
    for s in sessions:
        title = s["summary"] or s["session_id"]
        out.append(f"## {fmt_ts(s['started'])}  {title}")
        out.append("")
        out.append(f"- セッションID: `{s['session_id']}`")
        if s["cwd"]:
            out.append(f"- 作業ディレクトリ: `{s['cwd']}`")
        out.append(f"- ログファイル: `{s['path']}`")
        out.append(f"- 入力数: {len(s['prompts'])}")
        out.append("")
        for i, (ts, text) in enumerate(s["prompts"], 1):
            out.append(f"### {i}. {fmt_ts(ts)}")
            out.append("")
            out.append("```text")
            out.append(text.replace("```", "'''"))
            out.append("```")
            out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--grep", action="append", default=[], help="この語を含むセッションだけ")
    ap.add_argument("--since", type=str, default=None, help="YYYY-MM-DD")
    ap.add_argument("--until", type=str, default=None, help="YYYY-MM-DD")
    ap.add_argument("--session", type=str, default=None, help="セッションIDの一部")
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()

    if not args.root.exists():
        print(f"ログフォルダが見つかりません: {args.root}", file=sys.stderr)
        return 1

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None
    until = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc) if args.until else None

    sessions = []
    for path in sorted(args.root.glob("*/*.jsonl")):
        if args.session and args.session not in path.stem:
            continue
        s = load_session(path)
        if not s["prompts"]:
            continue
        if since and s["started"] and s["started"] < since:
            continue
        if until and s["started"] and s["started"] > until:
            continue
        if not matches(s, args.grep):
            continue
        sessions.append(s)

    sessions.sort(key=lambda s: (s["started"] or datetime.min.replace(tzinfo=timezone.utc)))
    text = render(sessions, args.grep)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"{len(sessions)} セッション / {sum(len(s['prompts']) for s in sessions)} プロンプトを {args.output} に書き出しました")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
