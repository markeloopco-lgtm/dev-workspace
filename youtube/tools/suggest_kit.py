#!/usr/bin/env python3
"""YouTube/Googleサジェスト収集 → 企画30本との突き合わせツール。

外部ライブラリ不要（標準ライブラリのみ）。

使い方（PowerShell / bash 共通）:

    # 1. サジェストを取ってくる（数分かかります）
    python suggest_kit.py fetch --seeds seeds.txt --out ../keywords/suggest.csv

    # 2. 企画30本と突き合わせる
    python suggest_kit.py match --suggest ../keywords/suggest.csv \\
        --plans ../keywords/plan_keywords.tsv --out ../keywords/report.md

    # 動作確認（ネット接続不要）
    python suggest_kit.py selftest
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# サジェストを深掘りするための後置文字。ひらがな＋英字＋数字。
EXPANDERS = list("あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわ")
EXPANDERS += list("abcdefghijklmnopqrstuvwxyz")
EXPANDERS += list("0123456789")


# --------------------------------------------------------------------------
# 取得
# --------------------------------------------------------------------------

def parse_suggest_payload(raw: str) -> list[str]:
    """Googleサジェスト(client=firefox)のJSONから候補文字列だけを取り出す。

    形式: ["クエリ", ["候補1", "候補2", ...], ...]
    候補がリスト形式で返る場合（client違い）も拾えるようにしてある。
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    out: list[str] = []
    for item in data[1]:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, list) and item and isinstance(item[0], str):
            out.append(item[0])
    return out


def fetch_suggest(query: str, source: str, timeout: float = 10.0) -> list[str]:
    """source は "yt"（YouTube検索）か "web"（Google検索）。"""
    params = {"client": "firefox", "hl": "ja", "q": query}
    if source == "yt":
        params["ds"] = "yt"
    url = f"{SUGGEST_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return parse_suggest_payload(raw)


def cmd_fetch(args: argparse.Namespace) -> int:
    seeds = [
        line.strip()
        for line in Path(args.seeds).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not seeds:
        print("シードキーワードが空です", file=sys.stderr)
        return 1

    sources = ["yt", "web"] if args.web else ["yt"]
    queries: list[tuple[str, str]] = []
    for seed in seeds:
        queries.append((seed, seed))
        if not args.shallow:
            for suffix in EXPANDERS:
                queries.append((seed, f"{seed} {suffix}"))

    total = len(queries) * len(sources)
    print(f"シード{len(seeds)}件 / 総リクエスト{total}件 (推定 {total * args.delay / 60:.1f}分)")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 完了済みクエリの記録。これがあるので --resume で続きから再開できる。
    progress_path = out.with_name(out.name + ".progress")

    seen: set[tuple[str, str]] = set()
    finished: set[tuple[str, str]] = set()
    resuming = bool(args.resume and out.exists())

    if resuming:
        with out.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("source") and row.get("suggestion"):
                    seen.add((row["source"], row["suggestion"]))
        if progress_path.exists():
            for line in progress_path.read_text(encoding="utf-8").splitlines():
                if "\t" in line:
                    src, qry = line.split("\t", 1)
                    finished.add((src, qry))
        remaining = total - len(finished)
        print(f"再開: 既存{len(seen)}語を引き継ぎ、残り{remaining}件から続けます")
    else:
        progress_path.unlink(missing_ok=True)

    saved = len(seen)
    done = 0
    skipped = 0
    errors = 0
    interrupted = False

    # 途中で止まっても結果が残るよう、1語ずつ書き出して定期的にフラッシュする。
    with out.open("a" if resuming else "w", encoding="utf-8-sig", newline="") as fh, \
            progress_path.open("a", encoding="utf-8") as pf:
        writer = csv.DictWriter(fh, fieldnames=["source", "suggestion", "seed", "rank"])
        if not resuming:
            writer.writeheader()
            fh.flush()
        try:
            for source in sources:
                for seed, query in queries:
                    done += 1
                    if (source, query) in finished:
                        skipped += 1
                        continue
                    try:
                        suggestions = fetch_suggest(query, source, timeout=args.timeout)
                    except (urllib.error.URLError, TimeoutError, OSError) as exc:
                        errors += 1
                        if errors <= 5:
                            print(f"  取得失敗 ({query}): {exc}", file=sys.stderr)
                        if errors == 20:
                            print("  失敗が20件を超えました。ネットワークかレート制限を確認してください。",
                                  file=sys.stderr)
                        time.sleep(args.delay)
                        continue
                    for rank, text in enumerate(suggestions, start=1):
                        key = (source, text)
                        if key in seen:
                            continue
                        seen.add(key)
                        writer.writerow(
                            {"source": source, "suggestion": text, "seed": seed, "rank": rank}
                        )
                        saved += 1
                    # 成功したクエリだけ記録する（失敗は再開時にやり直す）
                    pf.write(f"{source}\t{query}\n")
                    if done % 25 == 0:
                        fh.flush()
                        pf.flush()
                        print(f"  {done}/{total} 件完了 / 収集{saved}語（ここまで保存済み）")
                    time.sleep(args.delay)
        except KeyboardInterrupt:
            interrupted = True
            print("\n中断しました。ここまでの結果は保存されています。", file=sys.stderr)

    status = "中断" if interrupted else "完了"
    note = f" / スキップ {skipped} 件" if skipped else ""
    print(f"{status}: {saved}語を {out} に保存（{done}/{total}件処理 / 失敗 {errors} 件{note}）")
    if interrupted:
        print(f"続きから再開するには、同じコマンドに --resume を足してください。")
    if not saved:
        print("1語も取れていません。ネットワーク制限やプロキシを確認してください。", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# 突き合わせ
# --------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    """全角半角を正規化して空白で分割。空白なしの語も1トークンとして扱う。"""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = normalized.replace("　", " ")
    return [t for t in normalized.split() if t]


def covers(plan_kw: str, suggestion: str) -> bool:
    """企画のKWの全トークンがサジェストに含まれていれば「カバー済み」と判定。"""
    sug = unicodedata.normalize("NFKC", suggestion).lower().replace(" ", "")
    return all(token in sug for token in tokenize(plan_kw))


def load_plans(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        return [row for row in reader if row.get("plan_id")]


def load_suggestions(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def build_report(plans: list[dict[str, str]], suggestions: list[dict[str, str]]) -> str:
    texts = [row["suggestion"] for row in suggestions]
    # 上位表示されたサジェストほど検索需要が大きいとみなし、rankで重み付け。
    weight: Counter[str] = Counter()
    for row in suggestions:
        try:
            rank = int(row.get("rank") or 99)
        except ValueError:
            rank = 99
        weight[row["suggestion"]] += max(1, 11 - rank)

    lines: list[str] = []
    lines.append("# サジェスト × 企画30本 突き合わせレポート")
    lines.append("")
    lines.append(f"- 収集サジェスト: {len(texts)}語（ユニーク {len(set(texts))}語）")
    lines.append(f"- 企画: {len(plans)}本")
    lines.append("")

    lines.append("## 1. 企画ごとのサジェスト裏付け")
    lines.append("")
    lines.append("`ヒット数` が多いほど、実際に検索されている語に近い企画です。")
    lines.append("0 の企画はキーワードを見直してください。")
    lines.append("")
    lines.append("| 企画 | メインKW | ヒット数 | 実際のサジェスト例 |")
    lines.append("|---|---|---|---|")

    matched_texts: set[str] = set()
    scored: list[tuple[int, dict[str, str], list[str]]] = []
    for plan in plans:
        hits = [t for t in set(texts) if covers(plan["main_kw"], t)]
        for sub in (plan.get("sub_kw") or "").split("|"):
            if sub.strip():
                hits += [t for t in set(texts) if covers(sub.strip(), t)]
        hits = sorted(set(hits), key=lambda t: -weight[t])
        matched_texts.update(hits)
        scored.append((len(hits), plan, hits))

    for count, plan, hits in scored:
        examples = " / ".join(hits[:3]) if hits else "**該当なし**"
        lines.append(f"| {plan['plan_id']} | {plan['main_kw']} | {count} | {examples} |")

    lines.append("")
    lines.append("## 2. 企画でカバーできていない検索需要（新規企画の候補）")
    lines.append("")
    lines.append("30本のどれにも当てはまらなかったサジェストを、需要が大きい順に並べています。")
    lines.append("上位に来ているものは、企画を1本追加する価値があります。")
    lines.append("")
    lines.append("| 順位 | サジェスト | スコア |")
    lines.append("|---|---|---|")

    uncovered = [(t, w) for t, w in weight.most_common() if t not in matched_texts]
    for i, (text, score) in enumerate(uncovered[:60], start=1):
        lines.append(f"| {i} | {text} | {score} |")
    if not uncovered:
        lines.append("| - | （すべてカバー済み） | - |")

    lines.append("")
    lines.append("## 3. 需要が大きいサジェスト TOP50（カバー有無を問わず）")
    lines.append("")
    lines.append("| 順位 | サジェスト | スコア | 企画あり |")
    lines.append("|---|---|---|---|")
    for i, (text, score) in enumerate(weight.most_common(50), start=1):
        mark = "○" if text in matched_texts else "—"
        lines.append(f"| {i} | {text} | {score} | {mark} |")

    lines.append("")
    lines.append("## 4. シード別 サジェスト TOP12")
    lines.append("")
    lines.append("どのテーマにどれだけ需要が集まっているかが見えます。")
    lines.append("`—` は企画でカバーできていない語です。")
    lines.append("")

    by_seed: dict[str, set[str]] = {}
    for row in suggestions:
        by_seed.setdefault(row.get("seed", ""), set()).add(row["suggestion"])

    for seed in sorted(by_seed, key=lambda s: -sum(weight[t] for t in by_seed[s])):
        items = sorted(by_seed[seed], key=lambda t: -weight[t])[:12]
        total = sum(weight[t] for t in by_seed[seed])
        lines.append(f"### {seed}（{len(by_seed[seed])}語 / 合計スコア {total}）")
        lines.append("")
        for text in items:
            mark = "○" if text in matched_texts else "—"
            lines.append(f"- {mark} {text}（{weight[text]}）")
        lines.append("")

    return "\n".join(lines)


def cmd_match(args: argparse.Namespace) -> int:
    plans = load_plans(Path(args.plans))
    suggestions = load_suggestions(Path(args.suggest))
    if args.source:
        before = len(suggestions)
        suggestions = [r for r in suggestions if r.get("source") == args.source]
        label = "YouTube検索" if args.source == "yt" else "Google検索"
        print(f"{label}のみに絞り込み: {before}件 → {len(suggestions)}件")
    if not suggestions:
        print("サジェストCSVが空です。先に fetch を実行してください。", file=sys.stderr)
        return 1
    report = build_report(plans, suggestions)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"レポートを {out} に書き出しました")
    return 0


# --------------------------------------------------------------------------
# セルフテスト
# --------------------------------------------------------------------------

def cmd_selftest(_args: argparse.Namespace) -> int:
    failures: list[str] = []

    def check(label: str, actual: object, expected: object) -> None:
        if actual != expected:
            failures.append(f"{label}: 期待 {expected!r} / 実際 {actual!r}")

    payload = '["看護師",["看護師 辞めたい","看護師 副業","看護師 あるある"]]'
    check("parse:基本", parse_suggest_payload(payload),
          ["看護師 辞めたい", "看護師 副業", "看護師 あるある"])
    check("parse:入れ子形式", parse_suggest_payload('["q",[["候補A",0],["候補B",0]]]'),
          ["候補A", "候補B"])
    check("parse:壊れたJSON", parse_suggest_payload("<html>error</html>"), [])
    check("parse:候補なし", parse_suggest_payload('["q",[]]'), [])

    check("covers:語順違いでも一致", covers("看護師 辞めたい", "辞めたい 看護師 3年目"), True)
    check("covers:部分一致", covers("フリーランス看護師", "フリーランス看護師 収入"), True)
    check("covers:無関係は不一致", covers("看護師 開業届", "看護師 副業 バレる"), False)
    check("covers:全角半角の正規化", covers("看護師 ３年目", "看護師 3年目 辞めたい"), True)

    plans = [
        {"plan_id": "B1", "cluster": "B", "main_kw": "フリーランス看護師",
         "sub_kw": "看護師 フリーランス", "title": "テスト"},
        {"plan_id": "F1", "cluster": "F", "main_kw": "看護師 開業届",
         "sub_kw": "", "title": "テスト"},
    ]
    suggestions = [
        {"source": "yt", "suggestion": "フリーランス看護師 収入", "seed": "s", "rank": "1"},
        {"source": "yt", "suggestion": "看護師 夜勤 つらい", "seed": "s", "rank": "1"},
        {"source": "yt", "suggestion": "看護師 夜勤 つらい", "seed": "s2", "rank": "2"},
    ]
    report = build_report(plans, suggestions)
    check("report:B1がヒット1件", "| B1 | フリーランス看護師 | 1 |" in report, True)
    check("report:F1は該当なし", "| F1 | 看護師 開業届 | 0 | **該当なし** |" in report, True)
    check("report:未カバー語を検出", "看護師 夜勤 つらい" in report.split("## 2.")[1], True)

    # --- 中断と再開（ネットワークは差し替えて検証） ---
    import tempfile

    real_fetch, real_expanders = fetch_suggest, EXPANDERS[:]
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        seeds_file = tmpdir / "seeds.txt"
        seeds_file.write_text("看護師\n医療\n", encoding="utf-8")
        out_file = tmpdir / "s.csv"
        calls: list[str] = []
        limit = [3]

        def stub(query: str, source: str, timeout: float = 10.0) -> list[str]:
            calls.append(query)
            if len(calls) > limit[0]:
                raise KeyboardInterrupt
            return [f"{query} A", f"{query} B"]

        globals()["fetch_suggest"] = stub
        globals()["EXPANDERS"] = ["あ", "い", "う", "え"]
        try:
            opts = argparse.Namespace(seeds=str(seeds_file), out=str(out_file), delay=0,
                                      timeout=3, shallow=False, web=False, resume=False)
            cmd_fetch(opts)
            first = out_file.read_text(encoding="utf-8-sig").strip().splitlines()
            check("resume:中断時も保存される", len(first) > 1, True)

            calls.clear()
            limit[0] = 999
            opts.resume = True
            cmd_fetch(opts)
            second = out_file.read_text(encoding="utf-8-sig").strip().splitlines()
            check("resume:済みクエリをスキップ", len(calls), 7)
            check("resume:ヘッダが重複しない",
                  sum(1 for line in second if line.startswith("source,")), 1)
            check("resume:行が重複しない", len(second[1:]), len(set(second[1:])))
            check("resume:前回分が残る", len(second) > len(first), True)
        finally:
            globals()["fetch_suggest"] = real_fetch
            globals()["EXPANDERS"] = real_expanders

    plan_file = Path(__file__).resolve().parent.parent / "keywords" / "plan_keywords.tsv"
    if plan_file.exists():
        real_plans = load_plans(plan_file)
        check("plan_keywords.tsv:企画が読める", len(real_plans) >= 30, True)
        ids = [p["plan_id"] for p in real_plans]
        check("plan_keywords.tsv:ID重複なし", len(set(ids)), len(ids))
        missing = [p["plan_id"] for p in real_plans if not p.get("main_kw")]
        check("plan_keywords.tsv:main_kw欠損なし", missing, [])
    else:
        failures.append("plan_keywords.tsv が見つかりません")

    if failures:
        print("セルフテスト失敗:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("セルフテスト全項目パス")
    return 0


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="サジェスト収集と企画突き合わせ")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="サジェストを収集してCSVに保存")
    p_fetch.add_argument("--seeds", default="seeds.txt", help="シードキーワードのファイル")
    p_fetch.add_argument("--out", default="../keywords/suggest.csv", help="出力先CSV")
    p_fetch.add_argument("--delay", type=float, default=0.4, help="リクエスト間隔(秒)")
    p_fetch.add_argument("--timeout", type=float, default=10.0, help="タイムアウト(秒)")
    p_fetch.add_argument("--shallow", action="store_true", help="後置文字の展開をせず高速に取る")
    p_fetch.add_argument("--web", action="store_true", help="Google検索のサジェストも取る")
    p_fetch.add_argument("--resume", action="store_true",
                         help="前回の続きから再開する（既存CSVに追記）")
    p_fetch.set_defaults(func=cmd_fetch)

    p_match = sub.add_parser("match", help="サジェストと企画30本を突き合わせる")
    p_match.add_argument("--suggest", default="../keywords/suggest.csv")
    p_match.add_argument("--plans", default="../keywords/plan_keywords.tsv")
    p_match.add_argument("--out", default="../keywords/report.md")
    p_match.add_argument("--source", choices=["yt", "web"],
                         help="yt=YouTube検索のみ / web=Google検索のみ（動画企画はytで見る）")
    p_match.set_defaults(func=cmd_match)

    p_test = sub.add_parser("selftest", help="ネット接続なしで動作確認")
    p_test.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
