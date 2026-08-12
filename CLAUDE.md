# プロジェクト: Live2D量産 × AITuber自動運用パイプライン

一枚絵（外部生成）からLive2Dモデルを半自動量産し、AITuberKitで
チャット自動応答つきYouTube配信（ほぼ放置運用）を行うプロジェクト。

## ユーザーについて

- 日本語話者。非エンジニア寄り。**1ステップずつ、確認を取りながら**進めること
- 配信PC: Windows / RTX 3050 Laptop (VRAM 4GB) / PowerShell
- **完全無料方針**（有料サービスの提案は明示的に求められた時のみ）
- Claude Pro/Maxサブスクリプション利用（API課金なし）

## 現在の状態（2026-07-19時点）

ソフトウェア部分は完成・検証済み。残タスクは実機作業のみ:

- [ ] Kaggle登録 → `notebooks/see_through_free_gpu.ipynb` で一枚絵をレイヤー分解（ユーザーの一枚絵が必要）
- [ ] 分解PSDを `scripts/normalize_psd.py inspect` で検品 → 未分類があれば `configs/layer_mapping.yaml` に追記
- [ ] Style-Bert-VITS2をこのPCにセットアップ（docs/04 Step3。VRAM 4GBなので合成はCPUフォールバック許容）
- [ ] AITuberKitをセットアップ（`scripts/setup_aituber.sh init` はbash用。**Windowsネイティブでは手順を読み替えて実行**: clone → npm install → .env作成 → `configs/aituberkit.env.example` の値を反映）
- [ ] Cubism Editor PROトライアルで1体目のマスターリグ作成（GUI作業。docs/03のチェックリストに沿ってユーザーを誘導）
- [ ] Gemini APIキー・YouTube Data API v3キーの取得誘導 → .env設定
- [ ] OBS設定（クロマキー）→ テスト配信
- [ ] 自動編集を実機で試す: `winget install Gyan.FFmpeg` + `pip install -r requirements-autoedit.txt` → 録画で `scripts/auto_edit.py run`（docs/06）

## リポジトリ構成

- `docs/01〜06`: 工程順のドキュメント（発注仕様→See-through→Cubism→AITuber運用→ローカル移行→自動編集）
- `scripts/normalize_psd.py`: PSDレイヤー正規化（inspect / normalize）。GPU不要
- `scripts/batch_decompose.py`: 一括処理（`--normalize-only` はローカルで使う）
- `configs/layer_mapping.yaml`: See-through V3実タグ体系に較正済み（ソース調査で検証）
- `configs/aituberkit.env.example`: AITuberKit用env（変数名は本家.env.exampleに対し検証済み）
- `scripts/auto_edit.py`: 録画のニュース風自動編集（ジェットカット+テロップ+BGM）。設定は `configs/auto_edit.yaml`。テロップ様式は放送実務の目安に較正済み（根拠と出典はdocs/06末尾）
- `tests/run_selftest.py`: 正規化のラウンドトリップ検証。**Pythonコード変更時は必ず実行**
- `tests/run_autoedit_selftest.py`: 自動編集の検証（ffmpegがあれば合成動画で統合検証まで）。auto_edit変更時は必ず実行

## 重要な技術的前提（再調査不要）

- See-throughはVRAM 8GB必須 → このPCでは動かない。Kaggle/Colab無料枠を使う（docs/02）
- AITuberKitのLive2Dモデルは `public/live2d/<名前>/` 直下に .model3.json。Cubism Coreは手動DL必須
- AITuberKitランタイムはCubism 3/4系。**Cubism 5新機能は使わない**でリグを作る
- pytoshop書き出しPSDはunicode名に終端NULが付く既知問題 → normalize_psd.pyが除去済み
- ライセンス: AITuberKit非商用無料・**Live2D機能は現在商用不可**／SBV2はAGPL／声モデルはクレジット表記（docs/04の表参照）
- 自動編集は ffmpeg(要別途インストール)+faster-whisper(MIT)。テロップ焼き込みはASS字幕をlibassで描画、エンコードはNVENC失敗時にlibx264へ自動フォールバック（実測検証済み）。文字起こしSRTはカット前タイムライン基準で、renderが写像する
- テロップのフォントは `font: auto` で自動選択（`assets/fonts/`の同梱 → PCインストール済みの順に候補を探す）。同梱フォントはffmpegに `subtitles=...:fontsdir=` で渡すためインストール不要
- 帯のグラデーションは矩形を「下端まで」重ねて不透明度を積み上げる。矩形を隣接させると継ぎ目に横縞が出る（実際に出たので対策済み・テストで固定）

## 作業方針

- 変更は小さくコミット。コミットメッセージは日本語可
- ドキュメントと実装がズレたら必ず両方更新
- ユーザーへの説明は結論から。専門用語は一言添える
