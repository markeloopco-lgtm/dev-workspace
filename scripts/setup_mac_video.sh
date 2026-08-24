#!/usr/bin/env bash
# MacBook側 動画パイプライン環境セットアップ
#
# 役割分担:
#   クラウド無料GPU(Kaggle/Colab) = 動画の生成    -> notebooks/video_gen_free_gpu.ipynb
#   このMac                        = 素材の仕上げ  -> scripts/finish_reel.py
#
# 使い方:  bash scripts/setup_mac_video.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

# --- 0) 環境の確認 -----------------------------------------------------------
say "環境を確認しています"
[ "$(uname -s)" = "Darwin" ] || die "このスクリプトはmacOS専用です（検出: $(uname -s)）"

ARCH="$(uname -m)"
echo "  macOS : $(sw_vers -productVersion)"
echo "  チップ: ${ARCH}"
if [ "$ARCH" != "arm64" ]; then
  warn "Apple Silicon以外です。仕上げ処理は動きますが動作確認はしていません"
fi

# --- 1) Homebrew -------------------------------------------------------------
say "Homebrew を確認しています"
if ! command -v brew >/dev/null 2>&1; then
  # Apple Silicon は /opt/homebrew、Intel は /usr/local
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$candidate" ] && eval "$("$candidate" shellenv)" && break
  done
fi

if ! command -v brew >/dev/null 2>&1; then
  warn "Homebrew が入っていません。次の1行をターミナルに貼って入れてから、もう一度このスクリプトを実行してください:"
  echo
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  echo
  die "Homebrew未導入のため中断しました"
fi
echo "  brew  : $(brew --prefix)"

# --- 2) ffmpeg ---------------------------------------------------------------
say "ffmpeg を確認しています（動画の変換・結合に必須）"
if command -v ffmpeg >/dev/null 2>&1; then
  echo "  導入済み: $(ffmpeg -version | head -1)"
else
  echo "  インストールします（数分かかります）"
  brew install ffmpeg
fi

# --- 3) Python 仮想環境 ------------------------------------------------------
say "Python 仮想環境 (.venv) を用意しています"
PY=""
for c in python3.12 python3.11 python3; do
  command -v "$c" >/dev/null 2>&1 && PY="$c" && break
done
[ -n "$PY" ] || die "python3 が見つかりません。'brew install python@3.12' を実行してください"
echo "  python: $("$PY" --version) ($(command -v "$PY"))"

[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
echo "  依存パッケージを導入しました"

# --- 4) 作業フォルダ ---------------------------------------------------------
say "作業フォルダを作成しています"
mkdir -p video_input video_out
touch video_input/.gitkeep video_out/.gitkeep
echo "  video_input/  <- Kaggleから落としたクリップを置く"
echo "  video_out/    <- 仕上がった投稿用の動画が出る"

# --- 5) 動作確認 -------------------------------------------------------------
say "動作確認"
python scripts/finish_reel.py --selftest

cat <<'NEXT'

===========================================================
 セットアップ完了
===========================================================

次にやること:

  1. Kaggle でノートブックを実行して動画を生成する
       notebooks/video_gen_free_gpu.ipynb
       （右パネルで Accelerator: GPU T4 x2 / Internet: ON）

  2. 出力zipを展開して video_input/ に置く

  3. 投稿用に仕上げる
       source .venv/bin/activate
       python scripts/finish_reel.py video_input/clip_001.mp4

詳しい手順: docs/06_ai_video_pipeline.md
NEXT
