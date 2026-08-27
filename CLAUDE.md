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
- [ ] ショート1本目を通しで作る（docs/07。立ち絵PNG＋SBV2のWAVがあれば配信システム抜きで完結。
      **ユーザーの現在の関心はショート量産**。参考動画は「人気声優・◯◯さん(39)、
      最高すぎると話題に」というタイトルの声優話題系ショート＝ネットの反応まとめ型と判明。
      ただし実在人物の写真転載・ゴシップはリスクがあるため、オリジナルキャラが
      話題を紹介する形への置き換えを提案中。映像そのものは未確認）
- [ ] チャンネル企画を固める（docs/06 Step 0の表を埋める。キャラ設定＝AITuberKitの
      システムプロンプトなので、ここが決まらないと配信内容が決まらない）
- [ ] YouTubeチャンネル開設＋ライブ配信の有効化（docs/06。**有効化に最大24時間**
      かかるのでモデル完成を待たず先に着手させる）

## リポジトリ構成

- `docs/01〜06`: 工程順のドキュメント（発注仕様→See-through→Cubism→AITuber運用→ローカル移行→チャンネル開設）
- `docs/07_shorts_pipeline.md`: ショート量産ルート（配信とは独立。立ち絵PNG+WAVだけで完結）
- `scripts/normalize_psd.py`: PSDレイヤー正規化（inspect / normalize）。GPU不要
- `scripts/batch_decompose.py`: 一括処理（`--normalize-only` はローカルで使う）
- `scripts/make_short.py`: 台本YAML+WAV → 縦型MP4。ffmpeg必要・GPU不要。**変更時は `tests/test_make_short.py` を必ず実行**
- `configs/layer_mapping.yaml`: See-through V3実タグ体系に較正済み（ソース調査で検証）
- `configs/aituberkit.env.example`: AITuberKit用env（変数名は本家.env.exampleに対し検証済み）
- `tests/run_selftest.py`: 正規化のラウンドトリップ検証。**Pythonコード変更時は必ず実行**
- `tests/test_make_short.py`: ショート書き出しの実書き出し検証（ffmpegが要る）

## 重要な技術的前提（再調査不要）

- See-throughはVRAM 8GB必須 → このPCでは動かない。Kaggle/Colab無料枠を使う（docs/02）
- AITuberKitのLive2Dモデルは `public/live2d/<名前>/` 直下に .model3.json。Cubism Coreは手動DL必須
- AITuberKitランタイムはCubism 3/4系。**Cubism 5新機能は使わない**でリグを作る
- pytoshop書き出しPSDはunicode名に終端NULが付く既知問題 → normalize_psd.pyが除去済み
- ライセンス: AITuberKit非商用無料・**Live2D機能は現在商用不可**／SBV2はAGPL／声モデルはクレジット表記（docs/04の表参照）

## 作業方針

- 変更は小さくコミット。コミットメッセージは日本語可
- ドキュメントと実装がズレたら必ず両方更新
- ユーザーへの説明は結論から。専門用語は一言添える
