# 04. AITuber運用構成（AITuberKit + Gemini + Style-Bert-VITS2 + OBS）

docs/03で書き出した.moc3モデルを [AITuberKit](https://github.com/tegnike/aituber-kit) に載せ、
**Geminiでチャット自動応答 → Style-Bert-VITS2で発話 → Live2Dリップシンク → OBSでYouTube配信**
のほぼ放置運用を組む手順。

本書の設定値・変数名・パスは2026-07-19時点の各リポジトリ一次ソース
（aituber-kit / aituber-kit-docs / Style-Bert-VITS2 のmainブランチ実ファイル）から検証済み。

```
YouTubeライブのコメント
      │ (Data API v3を10秒間隔でポーリング)
      ▼
AITuberKit (localhost:3000)
  ├─ Gemini APIで返信生成 (GOOGLE_API_KEY)
  ├─ Style-Bert-VITS2サーバーへTTS (localhost:5000/voice)
  ├─ Live2Dモデル表示 + 音声駆動リップシンク + 感情→表情/モーション
  └─ 背景グリーン(#00FF00) + 回答字幕表示
      ▼ (ブラウザをウィンドウキャプチャ + クロマキー)
OBS ──RTMP──▶ YouTube Live
```

## 前提

- Node.js **24.x** / npm **11.6.2+**（aituber-kitのenginesで固定）
- Style-Bert-VITS2は**合成のみならCPUでも動く**（GPUがあれば低遅延）
- Google Cloudで **Gemini APIキー** と **YouTube Data API v3有効のAPIキー** を用意
- Live2D Cubism Core（後述。ライセンス上、手動ダウンロードが必要）

## Step 1: AITuberKit導入

```bash
./scripts/setup_aituber.sh init ~/aituber-kit
```

やっていること: `git clone` → `npm install` → `cp .env.example .env` →
`configs/aituberkit.env.example` の本構成向け設定を.envへ反映。
手動でやる場合はclone後に `configs/aituberkit.env.example` の値を.envに写す。

起動は `npm run dev` → http://localhost:3000 。

## Step 2: 自作Live2Dモデルの組み込み

1. **Cubism Core配置**（初回のみ）: ライセンス上リポジトリに同梱されないため、
   [Live2D公式のSDKダウンロード](https://www.live2d.com/sdk/download/web/)で規約同意して取得し、
   `Core/live2dcubismcore.min.js` を `public/scripts/live2dcubismcore.min.js` に置く
2. **モデル配置**: モデル一式（.moc3 / .model3.json / テクスチャ / physics3.json）を
   `public/live2d/<モデル名>/` に置く。**`.model3.json`はフォルダ直下に置く**こと
   （`/api/get-live2d-list` がこの構造をスキャンして設定UIのドロップダウンに出す。再ビルド不要）

   ```bash
   ./scripts/setup_aituber.sh add-model /path/to/cubism-export ~/aituber-kit
   ```
3. **.env設定**: `NEXT_PUBLIC_LIVE2D_ENABLED="true"`、`NEXT_PUBLIC_MODEL_TYPE="live2d"`、
   `NEXT_PUBLIC_SELECTED_LIVE2D_PATH="/live2d/<モデル名>/<名前>.model3.json"`
4. **表情・モーションのマッピング**: AIの感情タグ（neutral/happy/sad/angry/relaxed/surprised）が
   モデルの**表情名**（model3.jsonの`FileReferences.Expressions[].Name`）と
   **モーショングループ名**（`FileReferences.Motions`のキー）に対応付く。
   `.env`の`NEXT_PUBLIC_*_EMOTIONS` / `NEXT_PUBLIC_*_MOTION_GROUP` をモデル側の名前に合わせる。
   アイドルモーションはグループ名を **`Idle`** にしておくと無設定で5秒間隔再生される
5. **リップシンクは自動**: 合成音声の音量からpixi-live2d-display(lipsyncpatch)が口を駆動する。
   モデル側は標準の口パラメータがあればよい

> **Cubism Editorからの書き出し注意**: AITuberKitのランタイムはCubism 3/4系。
> **Cubism 5の新機能を使ったモデルは完全互換が保証されない**ため、
> docs/03のリグ作成時はCubism 4互換の範囲（SDK向け書き出し設定）に収めること。

## Step 3: Style-Bert-VITS2サーバー

```bash
git clone https://github.com/litagin02/Style-Bert-VITS2.git
cd Style-Bert-VITS2
python -m venv venv && source venv/bin/activate   # Windowsは venv\Scripts\activate
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install -r requirements.txt
python initialize.py        # デフォルト音声モデル(JVNV等)をダウンロード
python server_fastapi.py    # APIサーバー起動 (デフォルトポート5000, /docsでAPI確認)
```

Windowsなら公式の `Install-Style-Bert-VITS2.bat`（CPU版あり）でも可。
動作確認: `curl "http://127.0.0.1:5000/voice?text=こんにちは&model_id=0" -o test.wav`

AITuberKit側は `NEXT_PUBLIC_SELECT_VOICE="stylebertvits2"` と
`STYLEBERTVITS2_SERVER_URL="http://127.0.0.1:5000"`（テンプレ反映済み）。
モデル・スタイルは `NEXT_PUBLIC_STYLEBERTVITS2_MODEL_ID` / `_STYLE` で選択。

**声モデルのライセンス**: 同梱のJVNVモデルは CC BY-SA 4.0。
小春音アミ/あみたろモデルは商用可だが**生成音声の公開時にクレジット表記必須**
（「あみたろの声素材工房」）。配信で使うなら概要欄に記載する。
自作キャラ声にしたい場合は、権利クリアな音声でのモデル学習も可能（要GPU）。

## Step 4: Gemini接続

`.env`に `GOOGLE_API_KEY` を記入。`NEXT_PUBLIC_SELECT_AI_SERVICE="google"`、
モデルは `NEXT_PUBLIC_SELECT_AI_MODEL="gemini-2.5-flash"`（テンプレ既定。上位モデルに変更可）。
キャラの人格は設定UIのシステムプロンプトで作り込む。

## Step 5: YouTubeコメント自動応答

1. Google CloudでプロジェクトにYouTube Data API v3を有効化し、APIキーを
   `NEXT_PUBLIC_YOUTUBE_API_KEY` に設定
2. 配信開始後、その配信の動画IDを `NEXT_PUBLIC_YOUTUBE_LIVE_ID`（または設定UI）に設定
3. YouTubeモードON（テンプレで`NEXT_PUBLIC_YOUTUBE_MODE="true"`済み。UIでも切替可）

挙動（コード検証済み）:
- コメントは既定10秒間隔でポーリング（UIで3〜30秒に調整可）。発話中はスキップ
- 1サイクルにつき**ランダムに1コメント**へ返答。先頭が「#」のコメントは無視される
- **会話継続モード**（設定UIでON）にすると、LLMがコメント選択・話題継続を判断し、
  コメントが3サイクル無ければ自発的に新話題、6サイクルで待機状態に入る。
  「放置運用」に必須だが**LLM呼び出しが増えAPIコスト増**な点に注意

## Step 6: OBS配信

1. `.env`は配信向け設定済み: 背景`green`（画面が単色#00FF00になる）、
   操作パネル非表示、回答字幕（縁無し字幕風）ON
2. OBSで **ウィンドウキャプチャ**（またはブラウザソースで http://localhost:3000 ）を追加
3. キャプチャに**クロマキーフィルタ**（色: 緑）を適用し、下のレイヤーに配信用背景を敷く
4. 音声はAITuberKitを開いているブラウザの音を**アプリケーション音声キャプチャ**で拾う
5. OBSからYouTubeへ配信開始（OBSから直接配信するので仮想カメラは不要）

※ OBS取り込み方法自体は公式ドキュメントに記載がないため2〜4は一般的な手順。
背景greenの仕様・字幕・パネル非表示は一次ソース検証済み。

## 起動手順まとめ（毎配信）

1. `python server_fastapi.py`（Style-Bert-VITS2）
2. `npm run dev`（AITuberKit）→ ブラウザで開く
3. OBS起動 → YouTubeで配信作成 → OBSで配信開始
4. 配信の動画IDをAITuberKitに設定 → YouTubeモードON（＋会話継続モードON）
5. 終了時はYouTubeモードOFF → 配信停止

## ライセンス・規約の注意（重要）

| 対象 | 条件 |
|---|---|
| AITuberKit本体 | v2.0.0以降カスタムライセンス。**個人の非商用利用は無料**。商用（収益化配信を含む場合）は別途商用ライセンス（¥100,000〜） |
| AITuberKitのLive2D機能 | **商用利用は現在不可**（Live2D社との契約上の制約。非商用はOK）。収益化するならVRM/PNGTuberモードか、ライセンス状況の最新情報を要確認 |
| Live2D Cubism Core | SDKリリースライセンスへの同意が必要（ダウンロード時） |
| Style-Bert-VITS2 | AGPL-3.0（サーバーとして使う分には通常問題なし） |
| 同梱音声モデル | JVNV: CC BY-SA 4.0 / あみたろ系: クレジット表記必須 |
| Cubism Editor | docs/03のライセンス覚書参照（出版許諾契約） |

**収益化を視野に入れる場合**、Live2D機能の商用制限が現時点の最大の制約。
配信を非商用で始めて、収益化タイミングでAITuberKit作者への商用ライセンス相談
＋Live2D周りの最新条件確認、という順序を推奨。
