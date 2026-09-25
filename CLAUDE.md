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

- [ ] 参考動画 https://youtu.be/pn5xLGnvl4M を分析し、**同じ編集・テロップを `auto_edit.py` で付けられるようにする**（docs/06 → docs/07）。
      クラウド環境ではYouTubeが遮断され未実施（ユーザーの希望でこのPCで行う）。手順:
      1. **何も入れる前に既存環境を確認**（過去のローカル作業で導入済みの可能性が高い）:
         `ffmpeg -version`・`yt-dlp --version`・`deno --version`・`python --version`、リポジトリ直下の `.venv` の有無。
         足りない物だけ入れる（入れ方は docs/06「準備」・docs/07「セットアップ」）
      2. `analyze_video.py fetch` → `analyze` → テロップが出入りする瞬間を `frames` で1コマずつ見る
      3. テロップ（書体の系統・大きさ・文字色・縁取り・影・座布団・位置・1行の文字数・出方/消え方）と
         編集（カットの詰め方・切り替えの頻度・BGM・音量）を言葉と数値にまとめ、ユーザーと確認
      4. `configs/auto_edit.yaml` の `presets:` に新しいpresetとして書く（書体は近い無料フォントを `fetch_fonts.py` で）
         → `auto_edit.py preview` の画像を参考フレームと並べて調整
      5. auto_edit.py に無い演出（ポップ表示・ズーム・効果音など）が要る場合は、ユーザーに確認してから必要な分だけ追加
      真似るのは様式（作り方）だけ。参考動画の文字・映像・音声そのものは使わない（docs/06「注意」）
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
- [ ] 自動編集を実機で試す: `winget install Gyan.FFmpeg` + `pip install -r requirements-autoedit.txt` → 録画で `scripts/auto_edit.py run`（docs/07）

## リポジトリ構成

- `docs/01〜05`: 工程順のドキュメント（発注仕様→See-through→Cubism→AITuber運用→ローカル移行）
- `docs/06`: 参考動画のフレーム分析（目標値の作り方・指標の読み方）／`docs/07`: 録画の自動編集（ジェットカット+テロップ+BGM）
- `scripts/normalize_psd.py`: PSDレイヤー正規化（inspect / normalize）。GPU不要
- `scripts/batch_decompose.py`: 一括処理（`--normalize-only` はローカルで使う）
- `scripts/analyze_video.py`: 参考動画の分析（fetch / analyze / frames / compare）。GPU不要・ffmpeg必須。
  取得した動画と分析結果は `work/`（.gitignore済み。**コミットしない**）
- `configs/layer_mapping.yaml`: See-through V3実タグ体系に較正済み（ソース調査で検証）
- `configs/aituberkit.env.example`: AITuberKit用env（変数名は本家.env.exampleに対し検証済み）
- `scripts/auto_edit.py`: 録画の自動編集（ジェットカット+テロップ+BGM）。設定は `configs/auto_edit.yaml`。テロップ様式は preset で切替（talk=対談・既定 / business=ビジネス系YouTube / news=報道番組風）。各presetの数値は実在チャンネルの調査に較正済み（根拠と出典はdocs/07末尾）
- `scripts/fetch_fonts.py`: テロップ用の無料フォント（Google Fonts）を `assets/fonts/` に取得
- `tests/run_selftest.py`: 正規化のラウンドトリップ検証 ／ `tests/run_video_selftest.py`: 動画分析の検証（合成動画）／
  `tests/run_autoedit_selftest.py`: 自動編集の検証（ffmpegがあれば合成動画で統合検証まで）。**Pythonコード変更時は3つとも必ず実行**

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
- 動画分析ツールはこの `scripts/analyze_video.py`（fetch/analyze/frames/compare）に一本化。
  ブランチ `claude/youtube-editing-automation-t3wy9f` の同名スクリプト（テロップ帯の出現検出つき）は別実装なので混ぜない。
  テロップの出入りの自動検出が必要になったら、現行版へ機能として移植する
- 自動編集は ffmpeg(要別途インストール)+faster-whisper(MIT)。テロップ焼き込みはASS字幕をlibassで描画、エンコードはNVENC失敗時にlibx264へ自動フォールバック（実測検証済み）。文字起こしSRTはカット前タイムライン基準で、renderが写像する
- テロップのフォントは `font: auto` で自動選択（`assets/fonts/`の同梱 → PCインストール済みの順に候補を探す）。同梱フォントはffmpegに `subtitles=...:fontsdir=` で渡すためインストール不要
- 帯のグラデーションは矩形を「下端まで」重ねて不透明度を積み上げる。矩形を隣接させると継ぎ目に横縞が出る（実際に出たので対策済み・テストで固定）
- presetは `configs/auto_edit.yaml` 末尾の `presets:` を deep_merge で上書きする方式。書いた項目だけ差し替わる
- キーワード強調は「数字+単位の自動検出／keywords一覧／SRTに書く `*囲み*`」の3系統。数字と英単語は改行で割らない（`_protected_spans`）
- talk presetの話者色分けはSRTの「名前: 発言」形式。`speakers:` に登録した名前のみ話者扱い（「結論:」等の誤爆防止）

## 作業方針

- 変更は小さくコミット。コミットメッセージは日本語可
- ドキュメントと実装がズレたら必ず両方更新
- ユーザーへの説明は結論から。専門用語は一言添える
