# プロジェクト: Live2D量産 × AITuber自動運用パイプライン

一枚絵（外部生成）からLive2Dモデルを半自動量産し、AITuberKitで
チャット自動応答つきYouTube配信（ほぼ放置運用）を行うプロジェクト。

追加ライン: **金融解説チャンネルの動画自動生成**（docs/06）。
海外の解説系チャンネルをコンセプト参考に、日本語の長尺解説動画
（台本Gemini→音声SBV2→モーショングラフィックス描画→YouTube投稿）を無料スタックで量産する。

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

金融解説チャンネル（docs/06・2026-07-25追加。パイプラインは実装済み・デモ検証済み）:

- [ ] ユーザーPCでffmpeg導入 → `make_finance_video.py demo` で見た目確認
- [ ] GOOGLE_API_KEY設定 → `script --next` で台本生成テスト（内容の人力チェック必須）
- [ ] SBV2セットアップ後 → 音声つきで1本フル生成
- [ ] YouTube OAuth設定（client_secret.json）→ 初投稿（API審査未了はprivate固定）
- [ ] タスクスケジューラで日次自動化（docs/06 Step6）

## リポジトリ構成

- `docs/01〜05`: 工程順のドキュメント（発注仕様→See-through→Cubism→AITuber運用→ローカル移行）
- `docs/06`: 金融解説チャンネル自動生成（台本→音声→動画→投稿→日次自動化）
- `scripts/make_finance_video.py`: 解説動画パイプライン（demo/script/tts/render/thumbnail/all）。GPU不要・要ffmpeg
- `scripts/upload_youtube.py`: YouTube自動投稿（OAuth・サムネ・AI開示フラグつき）
- `configs/finance_channel.yaml`: チャンネル設定＋ネタ帳（topics）。色・フォント・声・免責文はここ
- `scripts/normalize_psd.py`: PSDレイヤー正規化（inspect / normalize）。GPU不要
- `scripts/batch_decompose.py`: 一括処理（`--normalize-only` はローカルで使う）
- `configs/layer_mapping.yaml`: See-through V3実タグ体系に較正済み（ソース調査で検証）
- `configs/aituberkit.env.example`: AITuberKit用env（変数名は本家.env.exampleに対し検証済み）
- `tests/run_selftest.py`: 正規化のラウンドトリップ検証。**Pythonコード変更時は必ず実行**

## 重要な技術的前提（再調査不要）

- See-throughはVRAM 8GB必須 → このPCでは動かない。Kaggle/Colab無料枠を使う（docs/02）
- AITuberKitのLive2Dモデルは `public/live2d/<名前>/` 直下に .model3.json。Cubism Coreは手動DL必須
- AITuberKitランタイムはCubism 3/4系。**Cubism 5新機能は使わない**でリグを作る
- pytoshop書き出しPSDはunicode名に終端NULが付く既知問題 → normalize_psd.pyが除去済み
- ライセンス: AITuberKit非商用無料・**Live2D機能は現在商用不可**／SBV2はAGPL／声モデルはクレジット表記（docs/04の表参照）
- 動画自動生成はPillow直描き＋ffmpegパイプ（moviepy等は不使用）。SBV2は `GET /voice` を叩く
- YouTube APIの審査(audit)未了プロジェクトからの投稿は**強制private**。翻訳転載・構成丸写しはNG（docs/06冒頭）

## 作業方針

- 変更は小さくコミット。コミットメッセージは日本語可
- ドキュメントと実装がズレたら必ず両方更新
- ユーザーへの説明は結論から。専門用語は一言添える
