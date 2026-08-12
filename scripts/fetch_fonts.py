#!/usr/bin/env python3
"""テロップ用の日本語フォントを Google Fonts から assets/fonts/ に取ってくる。

Google Fonts は全て SIL Open Font License なので、商用の動画配信でも無料で使える。
インストール作業は不要で、assets/fonts/ に置くだけで auto_edit.py が拾う。

usage:
  python scripts/fetch_fonts.py list                 # 選べるフォント一覧
  python scripts/fetch_fonts.py get ゴシック太        # 名前(別名)で1つ取得
  python scripts/fetch_fonts.py get --all-recommended # ビジネス系の定番をまとめて取得
"""

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
CSS_URL = "https://fonts.googleapis.com/css2?family={family}:wght@{weight}"
# Google Fonts は User-Agent が古いと ttf を返す(新しいと woff2 になる)
UA = "Mozilla/4.0"

# (別名, Google Fontsのファミリ名, ウェイト, 説明, おすすめか)
CATALOG = [
    ("ゴシック太",     "Noto Sans JP",          900, "源ノ角ゴシック相当の一番太い字。定番", True),
    ("ゴシック中",     "Noto Sans JP",          700, "同じ書体のボールド。落ち着いた太さ", True),
    ("UDゴシック",     "BIZ UDPGothic",         700, "Windows標準と同じ読みやすさ重視の書体", True),
    ("モダン太",       "Zen Kaku Gothic New",   900, "現代的で角が整った太ゴシック", True),
    ("モダン中",       "Zen Kaku Gothic New",   700, "同じ書体のボールド", False),
    ("エムプラス太",   "M PLUS 1p",             900, "YouTubeで多用される直線的なゴシック", True),
    ("エムプラス中",   "M PLUS 1p",             800, "同じ書体のやや細め", False),
    ("丸ゴシック太",   "M PLUS Rounded 1c",     800, "角が丸く柔らかい印象。親しみやすさ重視", True),
    ("丸ゴシック中",   "Zen Maru Gothic",       900, "別系統の丸ゴシック。かわいらしい", False),
    ("インパクト",     "Dela Gothic One",       400, "極太の見出し用。サムネや強調に", True),
    ("手書き風",       "Yusei Magic",           400, "手書き風。ツッコミやボケ向き", True),
    ("ポップ",         "RocknRoll One",         400, "軽快でポップな印象", False),
    ("明朝太",         "Shippori Mincho B1",    800, "明朝の太字。真面目・格式が要るとき", True),
    ("明朝中",         "Noto Serif JP",         900, "もう一つの明朝。落ち着いた印象", False),
    ("楷書風",         "Klee One",              600, "教科書体風。やわらかく誠実な印象", False),
    ("すっきり",       "Murecho",               800, "細部が素直で読みやすいゴシック", False),
    ("小杉丸",         "Kosugi Maru",           400, "軽い丸ゴシック。字面が大きめ", False),
    ("小杉",           "Kosugi",                400, "標準的な角ゴシック", False),
    ("さわらび",       "Sawarabi Gothic",       400, "やや細めで上品な角ゴシック", False),
    ("レトロ",         "Reggae One",            400, "レトロで目を引く見出し用", False),
]


def css_for(family: str, weight: int) -> str:
    url = CSS_URL.format(family=family.replace(" ", "+"), weight=weight)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read().decode("utf-8")


def download(family: str, weight: int, out_dir: Path) -> Path:
    css = css_for(family, weight)
    urls = re.findall(r"url\((https://[^)]+\.ttf)\)", css)
    if not urls:
        raise RuntimeError(f"{family} {weight} のttfが見つかりませんでした")
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{family.replace(' ', '')}-{weight}.ttf"
    req = urllib.request.Request(urls[-1], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as res:
        data = res.read()
    if len(data) < 10000:
        raise RuntimeError(f"{family} のダウンロードが不完全です ({len(data)} bytes)")
    dest.write_bytes(data)
    return dest


def cmd_list(args) -> int:
    print(f"{'別名':<12} {'ウェイト':>6}  説明")
    for alias, family, weight, desc, rec in CATALOG:
        mark = "★" if rec else "  "
        print(f"{mark}{alias:<12} {weight:>6}  {desc}")
    print("\n★ = ビジネス系の定番 (--all-recommended でまとめて取得)")
    return 0


def cmd_get(args) -> int:
    if args.all:
        targets = list(CATALOG)
    elif args.all_recommended:
        targets = [c for c in CATALOG if c[4]]
    elif args.name:
        targets = [c for c in CATALOG
                   if args.name in (c[0], c[1]) or args.name.lower() == c[1].lower()]
        if not targets:
            print(f"[error] 知らないフォントです: {args.name}\n"
                  f"  python scripts/fetch_fonts.py list で一覧を見てください",
                  file=sys.stderr)
            return 1
    else:
        print("[error] フォント名か --all-recommended を指定してください", file=sys.stderr)
        return 1

    out_dir = Path(args.output) if args.output else FONTS_DIR
    ok = 0
    for alias, family, weight, _desc, _rec in targets:
        try:
            path = download(family, weight, out_dir)
            print(f"  取得: {alias:<12} → {path.name}")
            ok += 1
        except (urllib.error.URLError, RuntimeError, OSError) as e:
            print(f"[warn] {alias} ({family}) の取得に失敗: {e}", file=sys.stderr)
    if not ok:
        print("[error] 1つも取得できませんでした。ネットワークを確認してください。\n"
              "  手動で入れる場合は assets/fonts/README.md を参照。", file=sys.stderr)
        return 1
    print(f"\n{ok}件を {out_dir} に置きました。")
    print("使えるようになったか確認: python scripts/auto_edit.py fonts")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="選べるフォントの一覧")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("get", help="フォントを取得して assets/fonts/ に置く")
    p.add_argument("name", nargs="?", help="別名 または Google Fonts のファミリ名")
    p.add_argument("--all-recommended", action="store_true",
                   help="ビジネス系の定番をまとめて取得")
    p.add_argument("--all", action="store_true", help="一覧のフォントを全部取得")
    p.add_argument("-o", "--output", help="置き場所 (省略時: assets/fonts/)")
    p.set_defaults(func=cmd_get)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
