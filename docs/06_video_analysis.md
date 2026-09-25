# 06. 参考動画をフレーム単位で分析する（videolab analyze）

VAIENCE（バイエンス）のような科学解説動画を**1フレームずつ計測**して、
「何秒ごとにカットが変わるか」「カメラはどれくらいの速さでズームするか」
「テロップはどこに何割の時間出ているか」「声の速さ・間・BGMの大きさ」などを
**数値の作風仕様（スタイルプロファイル）**にまとめる手順。制作側（docs/07）はこの数値を目標に動画を組み立て、
出来上がりを同じ物差しで採点する。

```
参考動画(URL)
  ├─ watch  : URLのままGeminiが構成・映像の出どころ・テロップの癖を言語化 (ダウンロード不要)
  └─ fetch → analyze : 全フレームを計測 → report.html / profile.json
                         │
            3〜5本分を aggregate
                         ▼
          configs/style_profile.yaml (目標スタイル・数値のみ)  ──▶ docs/07 制作
```

## 分かっていること・分からないこと（2026-09時点の調査）

| 項目 | 公開情報から分かったこと |
|---|---|
| 編集 | 求人票より: Premiere Proで「ナレーション音声に合わせたカット編集・テロップ挿入」、After Effectsでアニメーション・グラフィック、最後に色と音を調整 |
| 映像 | 3DCG＋実写・資料映像の組み合わせ（紹介記事）。**3DCGのソフトは非公開**、AI生成の使用は公表されていない |
| 声 | 人間の声優2名の掛け合い（落ち着いた中低音のナレーター＋濁声の専門家役） |
| 尺・頻度 | 単発回は約10分。通常動画は週3本前後 |
| 不明 | カットの平均秒数、テロップの書体・位置、BGM・効果音の出どころ、話速 → **このツールで測る** |

## ⚠ 先に読む: 法律・規約（法的助言ではありません）

| 論点 | 整理 |
|---|---|
| 著作権 | 統計（カットの長さ・色・音量など）を取るための複製は、著作権法30条の4「情報解析」の範囲と考えられる。ただし**見て楽しむ目的が混ざると外れる**。作風（テンポ・雰囲気）が似ること自体は侵害ではない（文化庁「AIと著作権に関する考え方」2024） |
| YouTube規約 | **許可の無いダウンロードは利用規約で禁止**（契約違反。アカウント停止のリスク）。著作権とは別の問題 |
| 安全な順番 | ① `watch`（URLのままGemini分析・ダウンロード無し）→ ② 必要なら本人の判断で `fetch`（720p以下・数本）→ 分析後 `purge` で削除 |
| やってはいけない | 参考動画の映像・音声・台詞を制作に使う／参考動画で画像AIや声のAIを学習させる（LoRA等）／声優の声を真似たAI音声を作る／キャラクター・ロゴ・チャンネル名に似せる |

このツールは安全側に作ってある: 取得は720p止まり・同意確認つき、`refs/`・`analysis/`はGitに入らない、
制作側は `refs/`・`analysis/` 内のファイルを素材に指定するとエラーで止まる。

---

## Step 1: 準備（初回のみ・20分）

このリポジトリをまだPCに持ってきていなければ、先に docs/05 の Step 1〜3 でクローンする。
videolab は `claude/vaience-frame-analysis-setup-ipb3hy` ブランチにあるので、クローン済みなら:

```powershell
cd $HOME\Documents\dev-workspace
git fetch origin
git checkout claude/vaience-frame-analysis-setup-ipb3hy
```

PowerShellを開いて、1行ずつ実行する。

```powershell
# 1. Python 3.12 と Git（入っていれば不要）
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git

# 2. ffmpeg（動画を読むための部品）
winget install -e --id Gyan.FFmpeg
```

**ここでPowerShellを一度閉じて開き直す**（インストールしたものを認識させるため）。

```powershell
# 3. 作業フォルダ（日本語を含まない場所がおすすめ）
cd $HOME\Documents\dev-workspace

# 4. このツール専用のPython環境を作って部品を入れる
py -3.12 -m venv .venv-video
.venv-video\Scripts\python.exe -m pip install -r requirements-video.txt

# 5. 文字化け予防（一度だけ）
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "User")
$env:PYTHONUTF8 = "1"   # 今開いているPowerShellにもすぐ反映

# 6. 環境チェック
.venv-video\Scripts\python.exe scripts\vlab.py doctor
```

`doctor` で `[NG]` が0件ならOK（`[--]` は後で使う任意機能）。

> 以降、`python scripts\vlab.py ...` や `python -m pip ...` と書いてあるところは、`python` を
> `.venv-video\Scripts\python.exe` に読み替える（例: `.venv-video\Scripts\python.exe scripts\vlab.py doctor`）。
> この書き方ならPowerShellの「スクリプト実行禁止」設定に引っかからず、部品も正しい環境に入る。
> **URLは必ず `"..."` で囲む**（`&` を含むURLを囲まないとPowerShellがエラーにする）。

## Step 2: 参考にする動画を選ぶ

3〜5本。**単発の通常回（10分前後・1テーマ）**を選ぶ。総集編・睡眠用・トーク回・ショートは
作風が違うので混ぜない。

## Step 3: ダウンロード不要の分析（推奨・無料）

[Google AI Studio](https://aistudio.google.com/) で無料のAPIキーを作り、作業フォルダで次の1行を実行して
`.env` というファイルに保存する（メモ帳だと `.env.txt` になったり文字コードの問題が出るので、このコマンドで作る）:

```powershell
Set-Content -Path .env -Value 'GEMINI_API_KEY=ここにキー' -Encoding ascii
```

`.env` はGitに入らない設定になっている（キーを公開しないため）。

```powershell
python scripts\vlab.py watch "https://www.youtube.com/watch?v=動画ID"
```

→ `analysis\watch_動画ID\gemini_watch.md` に、構成（区間ごとの役割）・映像の出どころ（3DCG/実写/図解…）・
テロップの癖・声とBGMの傾向・再現できる工夫がまとまる。
回数制限はプロジェクトごとにAI Studioに表示される（無料枠は1日あたりの動画時間にも上限がある）。
長い動画は `--start 0 --end 300` のように区切ってもよい。

※無料枠に送った内容はGoogleの製品改善に使われうる（`watch` はURLを渡すだけ）。

## Step 4: フレーム単位の計測

### 4-1. 動画の取得（規約を理解した上で、本人の判断で）

```powershell
python scripts\vlab.py fetch "https://www.youtube.com/watch?v=動画ID"
```

注意文が出て `y` を押すと、`refs\動画ID.mp4`（720p）と字幕 `refs\動画ID.ja.vtt` が保存される。
**1本の動画のURLだけ**受け付ける（チャンネルや再生リストのURLは一括取得になるので拒否する）。
メインのGoogleアカウントのCookieは絶対に使わない。

### 4-2. 解析

```powershell
python scripts\vlab.py analyze refs\動画ID.mp4
```

10分の動画でノートPCなら5〜15分程度。お試しは `--max-seconds 60`（先頭1分だけ）。
字幕が無い動画は `--whisper small` で文字起こしできる（先に
`.venv-video\Scripts\python.exe -m pip install faster-whisper`）。

できるもの（`analysis\動画ID\`）:

| ファイル | 中身 |
|---|---|
| `report.html` | **ブラウザで開く**。タイムライン・分布グラフ・全ショット一覧（台詞の文字を含む＝複製。共有しない） |
| `frames.csv` | 全フレームの計測値（Excelで開ける） |
| `shots.csv` | ショットごとの尺・カメラワーク・イージング・色・テロップ率 |
| `profile.json` | 数値の要約（スタイルプロファイル） |
| `audio.json` | ラウドネス・話速・間・BGMの音量差・掛け合いの推定 |
| `keyframes\`, `filmstrips\`, `contact_sheet.jpg`, `transcript.json` | 代表画・コマ送り画像・台詞（**参考動画の複製。共有・コミット禁止**） |

### 4-3. レポートの読み方（主な指標）

| 指標 | 意味 | 使い道 |
|---|---|---|
| ショット長（中央値） | 何秒ごとに画が切り替わるか | 台本の「映像1枚あたりの文字数」を決める |
| 冒頭30秒 / 本編のカット数/分 | つかみのテンポ | 冒頭だけ細かく割る |
| 遷移の内訳 | カット・ディゾルブ・暗転の割合 | 切り替え方の比率 |
| カメラワーク内訳・速度・イージング | ズーム/パンの割合と速さ、等速か加減速か | 静止画・3DCGの動かし方 |
| 明るさ・彩度・暗い画面の割合・配色 | 画の「色味」 | 色の調整・素材選び |
| テロップ表示率・縦位置・色 | 字幕の出し方 | テロップ設定 |
| ラウドネス(LUFS)・話速・間・BGM音量差 | 音の作り | 音声ミックスの目標値 |
| 話者交代/分・主話者の割合（推定） | 掛け合いのテンポ | 台本の話者配分 |

計測の仕組み: 全フレームで色・変化量・動き量を測り、1秒に5回、特徴点追跡でカメラの
拡大・移動・回転を推定（テロップは除外して背景の動きを見る）。カット・ディゾルブ・暗転・フラッシュを
区別して検出する。精度は `tests/run_video_selftest.py` の正解つき合成動画で検証している。

### 4-4. コマ送りで見る（フィルムストリップ）

気になる場面を連続フレームの一覧画像にする。テロップが何フレームで出るか、
カメラがどう加減速するかを数えるのに使う。

```powershell
python scripts\vlab.py frames refs\動画ID.mp4 1:23 1:26 --diff
```

`--diff` で前のコマから動いた場所が赤く表示される。画像は `analysis\動画ID\filmstrips\` に保存される。

### 4-5. 映像の「意味」を足す（任意）

どのショットが3DCG・実写・図解・AI生成っぽいか、を分類する方法は2つ:

- **Claude Codeに見せる（追加料金なし）**: 「analysis/動画ID/contact_sheet.jpg を見て、各ショットを
  3DCG/実写/図解などに分類して」と頼む（画像はAnthropicに送信される。学習への利用はClaudeのプライバシー設定次第）
- **Gemini（`annotate`）**: `python scripts\vlab.py annotate analysis\動画ID`
  （代表フレーム画像をGoogleに送信する。確認が出る）→ レポートとプロファイルに「映像の出どころの構成比」が入る

## Step 5: 目標スタイルを作る

3〜5本を解析したら、まとめて1つの目標スタイルにする（数値は中央値）。

```powershell
python scripts\vlab.py aggregate analysis\動画ID1 analysis\動画ID2 analysis\動画ID3 -o configs\style_profile.yaml
```

`configs/style_profile.yaml` は数値だけなのでコミットしてよい（最初に入っている値は「仮の初期値」）。

## Step 6: 構成の型を抜き出す（Claude Code）

```
prompts/structure_analysis.md を読んで、analysis/動画ID の構成を分析してください。
```

複数本やったら「まとめの手順で configs/structure_template.md を作って」と頼む。
台詞は引用せず、区間構成・つかみのパターン・驚きの間隔などの**抽象化した型**だけを残す。
（文字起こし等はAnthropicに送信される。台本づくりの下敷きにはせず、型の抽出だけに使う）

## Step 7: 後片付け

研究が済んだら、参考動画の複製を消して数値だけ残す:

```powershell
python scripts\vlab.py purge analysis\動画ID --video refs\動画ID.mp4
```

（最初から残さないなら `analyze ... --purge`）

purge の後に `python scripts\vlab.py report analysis\動画ID` を実行すると、画像も台詞も含まない
**数値だけのレポート**に作り直せる。人に見せてよいのはこちらだけ。

## 困ったとき

| 症状 | 対処 |
|---|---|
| `ffmpegが見つかりません` | `winget install -e --id Gyan.FFmpeg` → PowerShellを開き直す |
| `yt-dlp が失敗しました` | `.venv-video\Scripts\python.exe -m pip install -U "yt-dlp[default,deno]"` で更新。YouTube側の変更で一時的に失敗することもある |
| `Activate.ps1 を読み込めません` | 有効化せず `.venv-video\Scripts\python.exe` を直接使う（本書の書き方） |
| 文字化け | Step 1-5の `PYTHONUTF8` を設定してPowerShellを開き直す |
| Gemini 429エラー | 無料枠の回数制限。時間をおくか `.env` に `GEMINI_MODEL=gemini-flash-lite-latest` |
| Gemini 404エラー | モデル名が無効。AI Studioに表示されている名前を `GEMINI_MODEL` に書く |
| 解析が遅い | `--step 2`（2フレームに1回）でおよそ半分の時間 |
| `アンパサンド (&) 文字は許可されていません` | URLを `"..."` で囲む |
| `Gemini APIキーがありません`（.envに書いたのに） | Step 3 の `Set-Content` コマンドで作り直す（メモ帳で作ると `.env.txt` やUTF-16になることがある） |
