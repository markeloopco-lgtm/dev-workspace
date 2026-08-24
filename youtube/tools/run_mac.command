#!/bin/bash
# ダブルクリックで実行できます（Mac用）。
# サジェストを集めて、企画30本と突き合わせたレポートまで一気に作ります。

cd "$(dirname "$0")" || exit 1

echo "============================================"
echo " サジェスト収集ツール（Mac版）"
echo "============================================"
echo

# --- Python3 があるか確認 -------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ python3 が見つかりませんでした。"
  echo
  echo "次のコマンドをターミナルで実行してインストールしてください（無料・5〜10分）:"
  echo
  echo "    xcode-select --install"
  echo
  echo "インストール後、このファイルをもう一度ダブルクリックしてください。"
  echo
  read -r -p "Enterキーで閉じます " _
  exit 1
fi

echo "✅ python3 を確認しました（$(python3 -V 2>&1)）"
echo

# --- 収集モードを選ぶ -----------------------------------------------------
echo "どちらで実行しますか？"
echo "  1) お試し   … 1〜2分。ちゃんと動くかの確認用"
echo "  2) 本番     … 15〜25分。企画に使う本番データを取ります"
echo
read -r -p "番号を入力してEnter [1/2]: " mode
echo

case "$mode" in
  2) FETCH_OPTS="" ;    LABEL="本番" ;;
  *) FETCH_OPTS="--shallow" ; LABEL="お試し" ;;
esac

echo "--- $LABEL モードで収集を開始します ---"
echo

python3 suggest_kit.py fetch --seeds seeds.txt --out ../keywords/suggest.csv $FETCH_OPTS
FETCH_STATUS=$?

if [ $FETCH_STATUS -ne 0 ]; then
  echo
  echo "❌ サジェストを取得できませんでした。"
  echo
  echo "よくある原因:"
  echo "  ・病院や職場のWi-Fiは外部通信を制限していることがあります"
  echo "    → 自宅のWi-Fi、またはスマホのテザリングで試してください"
  echo "  ・短時間に叩きすぎた場合は、少し時間を空けてから再実行してください"
  echo
  read -r -p "Enterキーで閉じます " _
  exit 1
fi

echo
echo "--- 企画30本と突き合わせています ---"
echo

python3 suggest_kit.py match \
  --suggest ../keywords/suggest.csv \
  --plans ../keywords/plan_keywords.tsv \
  --out ../keywords/report.md
MATCH_STATUS=$?

if [ $MATCH_STATUS -ne 0 ]; then
  echo
  echo "❌ 突き合わせに失敗しました。"
  read -r -p "Enterキーで閉じます " _
  exit 1
fi

echo
echo "============================================"
echo " ✅ 完了しました"
echo "============================================"
echo
echo "できたファイル:"
echo "  ・$(cd .. && pwd)/keywords/suggest.csv   ← 集めたサジェスト"
echo "  ・$(cd .. && pwd)/keywords/report.md     ← 突き合わせレポート"
echo
echo "この2つをClaudeに渡すと、企画30本のタイトルを実データで書き直せます。"
echo

# Finderで場所を開く
open "$(cd .. && pwd)/keywords" 2>/dev/null

read -r -p "Enterキーで閉じます " _
