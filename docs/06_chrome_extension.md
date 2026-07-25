# 06. Chrome拡張: YouTube AIアップロードアシスタント

YouTube Studioの動画アップロード画面（詳細タブ）で、**タイトルと概要欄をAI（Gemini）でワンタッチ生成・入力**するChrome拡張機能。

- チャンネルごとに「方向性・視聴者層・口調」などの基本情報を記憶し、生成に自動反映
- LINE誘導・チャンネル説明などの**定型文を毎回チェックボックスで選んで**概要欄の末尾に追加
- AIはGemini API（無料枠）を使用。AITuberKitで使うAPIキーと同じものでOK
- キーや設定はすべて自分のブラウザ内（`chrome.storage.local`）に保存。外部サーバーは使わない

## インストール（約3分）

ChromeウェブストアではなくフォルダをそのままChromeに読み込む方式（無料・審査不要）。

1. このリポジトリをPCに置く（すでにクローン済みならそのままでOK）
2. Chromeでアドレスバーに `chrome://extensions` と入力して開く
3. 右上の **「デベロッパーモード」** をONにする
4. **「パッケージ化されていない拡張機能を読み込む」** をクリック
5. リポジトリ内の **`chrome_extension` フォルダ** を選択

一覧に「YouTube AIアップロードアシスタント」が表示されれば完了。

## 初期設定

1. 拡張機能一覧のカードにある **「詳細」→「拡張機能のオプション」** を開く
   （またはYouTube Studio上のパネルの ⚙ ボタン）
2. **Gemini APIキー** を入力
   - [Google AI Studio](https://aistudio.google.com/apikey) で無料発行（Googleアカウントがあれば1分）
   - docs/04 でAITuberKit用に取得するキーと同じものを使い回してよい
   - モデル名は既定の `gemini-2.5-flash` のままでOK（無料枠対象）
3. **チャンネルの基本情報** を登録
   - 「＋チャンネルを追加」でチャンネルID（`UC...`）を入力
     - チャンネルIDはYouTube StudioのURL `studio.youtube.com/channel/UCxxxx.../` の部分
     - アップロード画面のパネルに出る「このチャンネルを登録」ボタンからでも追加できる
   - チャンネル名・基本情報（方向性・視聴者層・口調・NG事項）・タイトルの傾向を自由記述で入力
4. **定型文** を登録（例）
   - 「LINE誘導」: LINEの案内文＋URL
   - 「チャンネル説明」: チャンネルの自己紹介文
   - 必要なだけ何個でも登録できる
5. 最後に **「保存する」** を押す

## 使い方

1. YouTube Studioで動画をアップロードし、詳細入力画面を開く
2. 画面右下に出る **「🤖 AI入力」** ボタンを押す
3. パネルで以下を入力・選択
   - **動画の内容・キーワード**（例:「新衣装お披露目の雑談配信」）
   - **概要欄に追加する定型文** にチェック（前回の選択を記憶している）
4. **「AIで生成する」** → 数秒でタイトルと概要欄が生成される
5. 内容を確認・手直しして **「アップロード画面に反映」** を押す
6. YouTube Studio側の画面で最終確認して公開設定へ

生成結果はパネル内で自由に編集してから反映できます。タイトルは100文字、概要欄は5000文字（YouTube上限）で自動的に切り詰めます。

## 仕組みと注意点

- 拡張はYouTube Studioの入力欄（contenteditable）に直接文字を書き込む方式。
  **YouTube Studio側のUI更新で入力欄が見つからなくなる可能性がある**。
  その場合は「入力欄が見つかりません」と表示されるので、パネルの生成結果を手動コピペで代用しつつ、`chrome_extension/content.js` の `findTitleBox` / `findDescriptionBox` のセレクタを直す
- Gemini無料枠のレート制限（429エラー）が出たら1分ほど待って再実行
- APIキーが漏れると他人に無料枠を消費されるため、リポジトリにキーを書き込まない（設定はブラウザ内保存のみ）

## ファイル構成

| ファイル | 役割 |
| --- | --- |
| `chrome_extension/manifest.json` | 拡張の定義（Manifest V3） |
| `chrome_extension/content.js` | Studio画面へのボタン・パネル注入、入力欄への書き込み |
| `chrome_extension/content.css` | 注入UIのスタイル |
| `chrome_extension/background.js` | Gemini API呼び出し（キーはcontent側に渡さない） |
| `chrome_extension/options.html/js/css` | 設定画面（APIキー・チャンネル・定型文） |
