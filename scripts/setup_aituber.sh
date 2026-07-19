#!/usr/bin/env bash
# AITuberKit導入と自作Live2Dモデル組み込みのヘルパー。配信用PCで実行する。
#
# usage:
#   ./scripts/setup_aituber.sh init <install-dir>
#       AITuberKitをclone→npm install→.env作成→configs/aituberkit.env.exampleの値を反映
#   ./scripts/setup_aituber.sh add-model <cubism-export-dir> <aituber-dir>
#       Cubism書き出しフォルダ(.model3.json入り)を public/live2d/ に配置し.envを更新
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_TEMPLATE="$REPO_ROOT/configs/aituberkit.env.example"
AITUBER_REPO="https://github.com/tegnike/aituber-kit.git"

log()  { printf '\033[1;36m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# テンプレートのKEY="VALUE"行を.envに反映する(既存キーは置換、無ければ追記)
apply_env_template() {
  local template="$1" envfile="$2" key
  [ -f "$envfile" ] || die ".envが見つかりません: $envfile"
  while IFS= read -r line; do
    case "$line" in
      [A-Z_]*=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    if grep -q "^${key}=" "$envfile"; then
      sed -i.bak "s|^${key}=.*|${line}|" "$envfile"
    else
      printf '%s\n' "$line" >> "$envfile"
    fi
  done < "$template"
  rm -f "${envfile}.bak"
  log ".envにテンプレートを反映しました: $envfile"
}

cmd_init() {
  local dir="${1:?install-dirを指定してください}"
  command -v git >/dev/null || die "gitが必要です"
  command -v node >/dev/null || die "Node.jsが必要です (24.x)"
  command -v npm >/dev/null || die "npmが必要です (11.6.2+)"

  local node_major
  node_major="$(node -v | sed 's/^v//' | cut -d. -f1)"
  if [ "$node_major" != "24" ]; then
    warn "AITuberKitはNode.js 24.xを要求します (現在: $(node -v))。nvm等で切り替えを推奨"
  fi

  if [ -d "$dir/.git" ]; then
    log "既存のclone検出: $dir (cloneをスキップ)"
  else
    log "AITuberKitをcloneします → $dir"
    git clone "$AITUBER_REPO" "$dir"
  fi

  cd "$dir"
  log "npm install 実行中(数分かかります)"
  npm install
  [ -f .env ] || cp .env.example .env
  apply_env_template "$ENV_TEMPLATE" "$dir/.env"

  if [ ! -f public/scripts/live2dcubismcore.min.js ]; then
    warn "Cubism Coreが未配置です。ライセンス上同梱できないため手動配置が必要:"
    warn "  1. https://www.live2d.com/sdk/download/web/ からCubism SDK for Webを取得(規約同意)"
    warn "  2. Core/live2dcubismcore.min.js を $dir/public/scripts/ にコピー"
  fi

  log "残りの手動設定:"
  log "  - $dir/.env の GOOGLE_API_KEY (Gemini) と NEXT_PUBLIC_YOUTUBE_API_KEY を記入"
  log "  - モデル配置: ./scripts/setup_aituber.sh add-model <cubism-export-dir> $dir"
  log "  - 起動: cd $dir && npm run dev → http://localhost:3000"
}

cmd_add_model() {
  local src="${1:?Cubism書き出しフォルダを指定してください}"
  local dir="${2:?AITuberKitのディレクトリを指定してください}"
  [ -d "$dir/public" ] || die "AITuberKitのディレクトリに見えません: $dir"

  local model3
  model3="$(find "$src" -maxdepth 1 -name '*.model3.json' | head -1)"
  [ -n "$model3" ] || die "直下に .model3.json が見つかりません: $src (Cubismの組み込み用書き出し一式を指定)"

  local name dest
  name="$(basename "$src")"
  dest="$dir/public/live2d/$name"
  if [ -e "$dest" ]; then
    die "配置先が既に存在します: $dest (上書きする場合は先に削除してください)"
  fi
  mkdir -p "$dir/public/live2d"
  cp -r "$src" "$dest"

  local model_path="/live2d/$name/$(basename "$model3")"
  if [ -f "$dir/.env" ]; then
    if grep -q '^NEXT_PUBLIC_SELECTED_LIVE2D_PATH=' "$dir/.env"; then
      sed -i.bak "s|^NEXT_PUBLIC_SELECTED_LIVE2D_PATH=.*|NEXT_PUBLIC_SELECTED_LIVE2D_PATH=\"$model_path\"|" "$dir/.env"
      rm -f "$dir/.env.bak"
    else
      printf 'NEXT_PUBLIC_SELECTED_LIVE2D_PATH="%s"\n' "$model_path" >> "$dir/.env"
    fi
    log ".envのモデルパスを更新: $model_path"
  else
    warn ".envが無いためモデルパスは未設定。設定UIから選択するか、.env作成後に再実行"
  fi

  log "モデル配置完了: $dest"
  log "表情/モーション名がモデルと合っているか .env の *_EMOTIONS / *_MOTION_GROUP を確認してください"
  log "(モデルのmodel3.jsonのFileReferences.Expressions / Motions のキーに合わせる)"
}

case "${1:-}" in
  init)      shift; cmd_init "$@" ;;
  add-model) shift; cmd_add_model "$@" ;;
  *)
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
