# 06. 金融解説チャンネルの動画自動生成（日本語・長尺アニメーション）

海外の解説・ナレーション系チャンネルをコンセプトの参考に、**日本語の金融解説
チャンネル**を完全無料スタックで自動運用するためのパイプライン。

```
configs/finance_channel.yaml のネタ帳（topics）
  │
  ▼
台本生成: Gemini API（無料枠・GOOGLE_API_KEY）      ← script.json
  │  タイトル/概要欄/タグ + セクション/ナレーション文/画面指示(visual)
  ▼
音声合成: Style-Bert-VITS2（docs/04 Step3と同じサーバー）← narration.wav + timing.json
  │  文ごとにWAV化 → 結合。字幕・アニメの切替タイミングも同時に確定
  ▼
描画: Pillow + ffmpeg（GPU不要・モーショングラフィックス）← video.mp4
  │  字幕バー / キーワード強調 / 折れ線・棒グラフ / 箇条書き / 比較パネル
  │  （任意で一枚絵キャラの立ち絵＋口パクも表示可能）
  ▼
サムネイル自動生成（thumbnail.png）
  │
  ▼
YouTube投稿: Data API v3（scripts/upload_youtube.py）
```

すべて `scripts/make_finance_video.py` のサブコマンドで、工程ごとにも一括でも実行できる。

> **重要（法的注意）**: 参考元チャンネルの動画を翻訳して転載したり、台本・映像を
> 丸写しすることは著作権侵害・YouTube規約違反になる。**参考にするのは
> 「ジャンルと構成の考え方」まで**とし、台本は毎回Geminiで独自生成する。

## 前提

- Python 3.10+ と `pip install -r requirements.txt`（追加の必須依存なし）
- **ffmpeg** がPATH上に必要。Windowsは PowerShell で:
  ```powershell
  winget install --id Gyan.FFmpeg
  # 入れた後はPowerShellを開き直す（PATH反映のため）
  ```
- 台本生成に **Gemini APIキー**（AITuberKit用と共用可・無料枠でOK）
- 音声合成に **Style-Bert-VITS2**（docs/04 Step3でセットアップするものと同じ。
  合成のみならCPUで動くのでVRAM 4GBでも問題ない）

## Step 1: まずデモで動作確認（APIキー不要・無音）

```powershell
python scripts/make_finance_video.py demo
```

サンプル台本から低解像度・**無音**の `output/videos/demo/video.mp4` と
`thumbnail.png` が生成される。再生して見た目を確認する。
色・チャンネル名・フォントは `configs/finance_channel.yaml` で変更できる。

## Step 2: 台本生成（Gemini）

```powershell
$env:GOOGLE_API_KEY = "AIza..."   # docs/04で取得するキーと同じでよい
python scripts/make_finance_video.py script --next
```

- `--next` は `configs/finance_channel.yaml` の `topics`（ネタ帳）のうち
  未生成の最初のトピックを使う。`--topic "好きなテーマ"` で直接指定も可
- 出力: `output/videos/<日付>_<テーマ>/script.json`
- **投稿前にscript.jsonの内容（特に数値・断定表現）を一読すること**。
  金融ジャンルはAIの事実誤りがそのまま信頼問題になる

## Step 3: 音声合成（Style-Bert-VITS2）

docs/04 Step3のSBV2サーバーを起動しておく（`python server_fastapi.py`、
デフォルト http://127.0.0.1:5000）。その後:

```powershell
python scripts/make_finance_video.py tts <出力ディレクトリ名>
```

- 使う声モデル・話速は `configs/finance_channel.yaml` の `tts:` で調整
- SBV2なしで流れだけ試すときは `--engine none`（無音・推定タイミング）
- **声モデルのクレジット表記**（docs/04のライセンス表参照）が必要なモデルは
  `upload.description_footer` に追記しておくと全動画の概要欄に自動で入る

## Step 4: レンダリングとサムネイル

```powershell
python scripts/make_finance_video.py render <出力ディレクトリ名>
python scripts/make_finance_video.py thumbnail <出力ディレクトリ名>
```

- フルHD・30fpsで6分の動画のレンダリングはCPUで十数分程度（GPU不要）
- 調整中は `--preview 20 --scale 0.5` で先頭20秒だけ半分の解像度で確認すると速い
- Step 2〜4をまとめてやるなら:
  ```powershell
  python scripts/make_finance_video.py all --next
  ```

### 立ち絵キャラを出す場合（任意）

このプロジェクトの一枚絵キャラを解説役として画面右下に表示できる。
`configs/finance_channel.yaml` の `character.base` に透過PNGのパスを設定。
`mouth_open`（口開き差分）も設定すると音声に合わせて口パクする。
※Live2Dのランタイム再生はこのパイプラインでは使わない（静止画+ゆれ+口パク）。
  Live2D機能の商用不可制約（docs/04）もこのため回避できる。

## Step 5: YouTube自動投稿

初回のみ:

1. Google Cloudコンソール →「APIとサービス」→ YouTube Data API v3 を有効化
2. 「認証情報」→ OAuthクライアントID（種類: **デスクトップアプリ**）を作成し、
   JSONをダウンロードして `configs/client_secret.json` に置く
   （OAuth同意画面はテストモードでよい。自分のアカウントをテストユーザーに追加）
3. `pip install google-api-python-client google-auth-oauthlib`

毎回:

```powershell
python scripts/upload_youtube.py <出力ディレクトリ名>
```

タイトル・概要欄（免責事項つき）・タグ・サムネイル・AI生成開示フラグまで自動設定される。

### 知っておくべきAPIの制約

| 制約 | 内容 | 対処 |
|---|---|---|
| **非公開ロック** | API審査(audit)を通していないプロジェクトからの投稿は**強制的に非公開**になる | 当面は「自動で非公開アップ→スマホのYouTube Studioで公開ボタン」運用。本格運用時に審査申請 |
| アップロードクォータ | 1本=1600ユニット、1日10000ユニット | 1日6本まで。通常運用（1日1〜2本）なら問題なし |
| カスタムサムネイル | 電話番号認証済みアカウントのみ | youtube.com/verify で認証 |

## Step 6: 定期自動実行（ほぼ放置運用）

Windowsタスクスケジューラに「毎日決まった時刻に1本作って投稿」を登録する例:

```powershell
# タスク用スクリプト scripts/daily_video.ps1 を作る場合の中身の例
$env:GOOGLE_API_KEY = "AIza..."
cd C:\path\to\dev-workspace
python scripts/make_finance_video.py all --next
# 最新の出力ディレクトリを投稿
$latest = Get-ChildItem output\videos -Directory | Sort-Object Name | Select-Object -Last 1
python scripts/upload_youtube.py $latest.Name
```

タスクスケジューラ → 「基本タスクの作成」→ 毎日 → プログラム:
`powershell.exe`、引数: `-ExecutionPolicy Bypass -File C:\...\scripts\daily_video.ps1`

※SBV2サーバーが起動している必要がある（スタートアップ登録するか、
daily_video.ps1の冒頭で起動する）。

## 運用上の注意（収益化・ポリシー）

- **AI生成の開示**: アップロード時に `containsSyntheticMedia`（改変・合成
  コンテンツの開示）を自動で立てている。切らないこと
- **収益化(YPP)**: 機械的な量産と見なされる「繰り返しの多いコンテンツ」は
  収益化審査に通りにくい。台本の質チェック・オリジナルの切り口・サムネの
  作り込みなど人の付加価値を残すのが現実的
- **金融ジャンル特有**: 断定的な投資助言・銘柄推奨はしない（プロンプトで抑制済み）。
  免責事項は全動画の概要欄と動画末尾に自動挿入される
- **Gemini無料枠**: 1日1〜2本の台本生成なら無料枠で十分足りる

## トラブルシューティング

- `日本語フォントが見つかりません` → `configs/finance_channel.yaml` の
  `fonts.candidates` に実在するフォント（例: `C:/Windows/Fonts/meiryo.ttc`）を追加
- `Style-Bert-VITS2サーバーに接続できません` → サーバー起動を確認。
  ポートを変えている場合は `tts.endpoint` を修正
- `ffmpeg失敗` / ffmpegが見つからない → Step 0のwingetで導入し、シェルを開き直す
- Geminiが不正なJSONを返す → もう一度実行（temperature由来の揺れ）。
  続くようなら `script_gen.model` を変更
