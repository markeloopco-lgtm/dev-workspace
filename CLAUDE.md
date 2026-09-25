# プロジェクト: Live2D量産 × AITuber自動運用パイプライン ＋ 解説動画の作風分析・制作（videolab）

1. 一枚絵（外部生成）からLive2Dモデルを半自動量産し、AITuberKitで
   チャット自動応答つきYouTube配信（ほぼ放置運用）を行う
2. **videolab**: 参考動画（VAIENCE等の科学解説CG動画）をフレーム単位で計測して作風を数値化し、
   同じテンポ・品質のオリジナル動画を台本YAMLから自動で組み立てて採点する（docs/06・07）

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

## videolab の状態（2026-09-25時点）

ソフトウェアは完成・合成データで検証済み（`tests/run_video_selftest.py`）。実機作業が残り:

- [ ] Windowsで `.venv-video` を作り `requirements-video.txt` を入れる → `vlab doctor`（docs/06 Step1）
- [ ] Gemini APIキー → `.env` → `vlab watch <URL>` で参考動画2〜3本の構成・映像の出どころを把握
- [ ] （本人が規約を理解した上で）`vlab fetch` → `vlab analyze` → 3〜5本を `aggregate` して
      `configs/style_profile.yaml` を実測値に置き換える（現在は**仮の初期値**）
- [ ] VOICEVOXをインストール → `vlab voices` で話者IDを確認 → サンプル台本を `--check` 付きで書き出し
- [ ] （任意）Blender 5.2 LTS → 3DCG宇宙シーンの所要時間を `--draft` で測る
- [ ] オリジナルのキャラクター・チャンネル設定を決め、`prompts/episode_writer.md` で1本目の台本

## リポジトリ構成

- `docs/01〜05`: 工程順のドキュメント（発注仕様→See-through→Cubism→AITuber運用→ローカル移行）
- `scripts/normalize_psd.py`: PSDレイヤー正規化（inspect / normalize）。GPU不要
- `scripts/batch_decompose.py`: 一括処理（`--normalize-only` はローカルで使う）
- `configs/layer_mapping.yaml`: See-through V3実タグ体系に較正済み（ソース調査で検証）
- `configs/aituberkit.env.example`: AITuberKit用env（変数名は本家.env.exampleに対し検証済み）
- `tests/run_selftest.py`: 正規化のラウンドトリップ検証。**Pythonコード変更時は必ず実行**
- `docs/06〜07`: videolab（参考動画のフレーム分析 → 目標スタイル → 台本から制作・採点）
- `scripts/vlab.py`: videolabのコマンド（doctor / watch / fetch / analyze / frames / aggregate /
  compare / annotate / purge / voices / new-episode / produce）
- `videolab/`: 本体。`analyze.py`(フレーム計測・カット/ディゾルブ/暗転検出・カメラワーク推定)、
  `audio.py`(ラウドネス・話速・間・BGM差・掛け合い推定)、`profile.py`(プロファイル・集約・採点)、
  `report.py`(HTMLレポート)、`produce/`(TTS・タイムライン・宇宙シーン・合成・ミックス)
- `configs/style_profile.yaml`: 目標スタイル（数値のみ。aggregateで実測値に置き換える）
- `episodes/`: 台本YAML（書式は `episodes/README.md`）、`prompts/`: Claude Code向け指示書
- `tests/run_video_selftest.py`: 正解つき合成動画での解析精度＋制作ラウンドトリップ検証。
  **videolabのコード変更時は必ず実行**（`--quick` で宇宙シーン抜きの短縮版）

## 重要な技術的前提（再調査不要）

- See-throughはVRAM 8GB必須 → このPCでは動かない。Kaggle/Colab無料枠を使う（docs/02）
- AITuberKitのLive2Dモデルは `public/live2d/<名前>/` 直下に .model3.json。Cubism Coreは手動DL必須
- AITuberKitランタイムはCubism 3/4系。**Cubism 5新機能は使わない**でリグを作る
- pytoshop書き出しPSDはunicode名に終端NULが付く既知問題 → normalize_psd.pyが除去済み
- ライセンス: AITuberKit非商用無料・**Live2D機能は現在商用不可**／SBV2はAGPL／声モデルはクレジット表記（docs/04の表参照）

### videolab の前提（再調査不要・2026-09調査）

- VAIENCEの制作: 求人票より Premiere（ナレーションに合わせたカット・テロップ）＋ After Effects。
  3DCGソフトは非公開、AI生成の公表なし。声は人間の声優2名の掛け合い。単発回は約10分
- **YouTube規約はダウンロードを禁止**（契約違反・アカウント停止リスク）。著作権は30条の4（情報解析）で
  統計目的なら可と考えられるが、法的助言ではない。→ `watch`（URLのままGemini）を先に勧める。
  `fetch` は同意確認つき・720p上限。`refs/` `analysis/` はGitに入れない。制作は `refs/` `analysis/` の素材を拒否
- 参考動画の映像・音声・台詞を制作に使わない。声優の声を真似たモデルを作らない。キャラ・ロゴ・名前を似せない
- yt-dlpはJSランタイム(deno)が必要 → `pip install "yt-dlp[default,deno]"`。解析はH.264で取る（AV1回避）
- opencv-pythonは非headless版（scenedetect/rapidocrと衝突するため）。OpenCV 5.0で動作確認済み
- Gemini: モデル名はエイリアス（watch=`gemini-flash-latest` / annotate=`gemini-flash-lite-latest`）。
  無料枠の回数はAI Studioで確認（固定値を書かない）。無料枠の入力はGoogleの改善に使われる → annotateは同意制
- Blender: 5.2 LTSを想定。EEVEEのID は5.x `BLENDER_EEVEE` / 4.2〜4.5 `BLENDER_EEVEE_NEXT`。
  コンポジターAPIは5.0で変わったので使わない
- VOICEVOX: ちび式じい・玄野武宏・麒ヶ島宗麟は商用可（「VOICEVOX:名前」表記）。青山龍星は事業利用に事前確認
- YouTube収益化: 2026-07から「汎用的・繰り返しの多いコンテンツ」と「AIペルソナが健康・法律・金融の助言」は対象外
- PowerShellの実行ポリシーで Activate.ps1 が動かないことがある → `.venv-video\Scripts\python.exe` を直接使う

## 作業方針

- 変更は小さくコミット。コミットメッセージは日本語可
- ドキュメントと実装がズレたら必ず両方更新
- ユーザーへの説明は結論から。専門用語は一言添える
