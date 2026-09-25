# プロジェクト: Live2D量産 × AITuber自動運用パイプライン

一枚絵（外部生成）からLive2Dモデルを半自動量産し、AITuberKitで
チャット自動応答つきYouTube配信（ほぼ放置運用）を行うプロジェクト。

## ユーザーについて

- 日本語話者。非エンジニア寄り。**1ステップずつ、確認を取りながら**進めること
- 配信PC: Windows / RTX 3050 Laptop (VRAM 4GB) / PowerShell
- **完全無料方針**（有料サービスの提案は明示的に求められた時のみ）
- Claude Pro/Maxサブスクリプション利用（API課金なし）

## 現在の状態（2026-09-25時点）

ソフトウェア部分は完成・検証済み。残タスクは実機作業のみ:

- [ ] 参考動画 https://youtu.be/lZFXcN2tA4s をこのPCで分析（docs/06）: `fetch` → `analyze` → report.md と代表フレームを読んで制作仕様を作る。
      クラウド環境ではYouTubeへの接続がネットワーク設定で遮断され未実施。
      ユーザーによると、この動画は**フリー動画素材を組み合わせて作られている**
      → 素材の長さ・つなぎ方（shots.csv）・色の統一感・字幕/ナレーション/BGMを重点に見る。
      制作パイプライン（素材集め→組み立て→字幕→ナレーション→音量調整）は、分析結果をユーザーと確認してから作る
- [ ] Kaggle登録 → `notebooks/see_through_free_gpu.ipynb` で一枚絵をレイヤー分解（ユーザーの一枚絵が必要）
- [ ] 分解PSDを `scripts/normalize_psd.py inspect` で検品 → 未分類があれば `configs/layer_mapping.yaml` に追記
- [ ] Style-Bert-VITS2をこのPCにセットアップ（docs/04 Step3。VRAM 4GBなので合成はCPUフォールバック許容）
- [ ] AITuberKitをセットアップ（`scripts/setup_aituber.sh init` はbash用。**Windowsネイティブでは手順を読み替えて実行**: clone → npm install → .env作成 → `configs/aituberkit.env.example` の値を反映）
- [ ] Cubism Editor PROトライアルで1体目のマスターリグ作成（GUI作業。docs/03のチェックリストに沿ってユーザーを誘導）
- [ ] Gemini APIキー・YouTube Data API v3キーの取得誘導 → .env設定
- [ ] OBS設定（クロマキー）→ テスト配信

## リポジトリ構成

- `docs/01〜05`: 工程順のドキュメント（発注仕様→See-through→Cubism→AITuber運用→ローカル移行）
- `docs/06`: 参考動画のフレーム分析（目標値の作り方・指標の読み方）
- `scripts/normalize_psd.py`: PSDレイヤー正規化（inspect / normalize）。GPU不要
- `scripts/batch_decompose.py`: 一括処理（`--normalize-only` はローカルで使う）
- `scripts/analyze_video.py`: 参考動画の分析（fetch / analyze / frames / compare）。GPU不要・ffmpeg必須。
  取得した動画と分析結果は `work/`（.gitignore済み。**コミットしない**）
- `configs/layer_mapping.yaml`: See-through V3実タグ体系に較正済み（ソース調査で検証）
- `configs/aituberkit.env.example`: AITuberKit用env（変数名は本家.env.exampleに対し検証済み）
- `tests/run_selftest.py`: 正規化のラウンドトリップ検証 ／ `tests/run_video_selftest.py`: 動画分析の検証（合成動画）。
  **Pythonコード変更時は両方必ず実行**

## 重要な技術的前提（再調査不要）

- See-throughはVRAM 8GB必須 → このPCでは動かない。Kaggle/Colab無料枠を使う（docs/02）
- AITuberKitのLive2Dモデルは `public/live2d/<名前>/` 直下に .model3.json。Cubism Coreは手動DL必須
- AITuberKitランタイムはCubism 3/4系。**Cubism 5新機能は使わない**でリグを作る
- pytoshop書き出しPSDはunicode名に終端NULが付く既知問題 → normalize_psd.pyが除去済み
- ライセンス: AITuberKit非商用無料・**Live2D機能は現在商用不可**／SBV2はAGPL／声モデルはクレジット表記（docs/04の表参照）
- pytoshop 1.2.1 は `six` を使うのに依存に宣言していない → requirements.txt に明記済み
- 動画分析: ffmpeg は `winget install --id Gyan.FFmpeg -e`。YouTube取得の yt-dlp は Deno が必要で、
  `yt-dlp[default,deno]` で venv 内に入る（yt-dlp は venv の Scripts 内の deno を自動で見つける）
- クロスフェード判定は「中間コマを前後の画の混合で再現した残差」で行う（パン・ズームと区別するため。docs/06）。
  判定基準を変えたら tests/run_video_selftest.py の合成動画（ディゾルブ・動く素材同士・パン・ズーム）で確認

## 作業方針

- 変更は小さくコミット。コミットメッセージは日本語可
- ドキュメントと実装がズレたら必ず両方更新
- ユーザーへの説明は結論から。専門用語は一言添える
