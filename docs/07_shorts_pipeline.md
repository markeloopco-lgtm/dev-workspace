# 07. ショート動画の量産

台本を書く → 音声を作る → **1コマンドで縦型MP4** という流れでショートを量産する。

docs/04 のライブ配信とは別ルート。**AITuberKitもLive2Dも使わない**ので、
配信システム一式を組み終わっていなくても、立ち絵と音声さえあれば今日から作れる。

```
一枚絵 (docs/01)
  │
  ▼
See-throughで分解 → normalize_psd.py --emit-pngs      ← 立ち絵PNGを取り出す
  │  口閉じ / 口開き の2枚を用意する
  ▼
台本を書く (Geminiに書かせてもよい)
  │
  ▼
Style-Bert-VITS2 で台詞ごとにWAV書き出し (docs/04 Step3)
  │
  ▼
scripts/make_short.py build script.yaml              ← このリポジトリ
  │  字幕焼き込み + 口パク自動生成 + BGM合成
  ▼
1080x1920 のMP4 → YouTubeに投稿
```

---

## 必要なもの

| もの | 入手 |
|---|---|
| Python + Pillow / PyYAML / numpy | `pip install -r requirements.txt` |
| ffmpeg | `pip install imageio-ffmpeg`（自動で使われる）または公式版をPATHに |
| 立ち絵PNG（背景透過） | docs/02 の分解結果、または手持ちの一枚絵 |
| 台詞のWAV | Style-Bert-VITS2（docs/04 Step 3） |
| 日本語フォント | Windowsなら標準のメイリオ等を自動検出 |

**GPUは不要。** 動画の書き出しはCPUだけで動くので、VRAM 4GBのPCでも問題ない。

---

## Step 1: 作業フォルダを作る

```powershell
python scripts/make_short.py init shorts/001
```

こうなる:

```
shorts/001/
  script.yaml     ← 台本（これを編集する）
  voice/          ← 台詞WAVを置く
  assets/         ← 立ち絵・背景・BGMを置く
```

## Step 2: 立ち絵を用意する

分解済みPSDからPNGを取り出す:

```powershell
python scripts/normalize_psd.py normalize model.psd -o out.psd --emit-pngs shorts/001/assets/
```

`configs/layer_mapping.yaml` に「口」のスロットがあるので、口のレイヤーが分離される。
**口開きの絵は分解結果には含まれない**ので、口閉じの立ち絵をコピーして
口の部分だけ描き足す（ペイントソフトで数分）。

口開きを用意しなければ**口パクなしの静止立ち絵**になる。まず1本作ってみるならこれで十分。

## Step 3: 台詞のWAVを作る

Style-Bert-VITS2 のWebUIで台詞を1行ずつ合成し、`voice/001.wav`, `voice/002.wav` …
と連番で保存する。

> **サンプリングレートは全部揃えること。** 混在しているとエラーになる（そう表示される）。

## Step 4: 台本を書く

`script.yaml` を編集する。最低限これだけ動く:

```yaml
character:
  closed: assets/closed.png
  open:   assets/open.png

lines:
  - text: "今日はこれだけ覚えて帰ってください"
    audio: voice/001.wav
  - text: "結論から言うと、いちばん大事なのは順番です"
    audio: voice/002.wav
```

`text` がそのまま字幕になる。長い行は自動で折り返される（既定16文字）。

主な設定項目:

| キー | 意味 | 既定 |
|---|---|---|
| `fps` / `size` | フレームレート / 解像度 | 30 / `[1080, 1920]` |
| `background.color` / `.image` | 背景色 / 背景画像（自動で切り抜き） | 濃紺 |
| `character.height_ratio` | 立ち絵の高さ（画面比） | 0.55 |
| `character.align` | `left` / `center` / `right` | center |
| `subtitle.max_chars_per_line` | 字幕の折り返し文字数（上限） | 16 |
| `subtitle.max_width_ratio` | 字幕の最大幅（画面比）。実測幅でも折り返す | 0.88 |
| `subtitle.bottom_ratio` | 字幕の位置（下からの比率） | 0.20 |
| `subtitle.size_ratio` | 字幕サイズ（画面高比） | 0.055 |
| `subtitle.font` | フォントのパス（未指定なら自動検出） | 自動 |
| `mouth.threshold` | 口が開く音量のしきい値（下げるとよく動く） | 0.12 |
| `gap_sec` | 台詞と台詞の間 | 0.25秒 |
| `bgm.file` / `.gain_db` | BGM（尺に合わせて自動ループ） | なし / -18dB |

## Step 5: 書き出す

```powershell
# まず構成と尺だけ確認（書き出さないので一瞬）
python scripts/make_short.py build shorts/001/script.yaml --probe

# 本番
python scripts/make_short.py build shorts/001/script.yaml -o shorts/001/out.mp4
```

`--probe` は台詞ごとの秒数と合計尺を出すだけ。**尺の調整はこれで詰めてから書き出す**と速い。

---

## 量産のコツ

### 台本はGeminiに書かせる

docs/04 で使うGemini APIキーがあれば、台本の下書きは自動化できる。
プロンプトの型を1つ決めて、テーマだけ差し替えて回す:

```
あなたは「（キャラ設定）」というキャラクターです。
「（テーマ）」について、30〜45秒のショート動画の台詞を書いてください。
条件:
- 1行あたり25文字以内、8〜12行
- 1行目で結論か意外な事実を言い切る（続きを見たくなるように）
- 最後は次の動画につながる一言で終える
- 台詞だけを1行ずつ出力（ト書き・記号は不要）
```

出力をそのまま `lines` の `text` に貼り、同じ文をSBV2に流せばWAVが揃う。

### フォルダを使い回す

`shorts/001` をコピーして `shorts/002` にすれば、立ち絵と設定はそのまま使える。
**変えるのは `voice/` の中身と `text` だけ**になる。

### 冒頭2秒がすべて

ショートは最初の数秒で離脱が決まる。1行目に一番強い一言を置く。
挨拶（「こんにちはー、〇〇です」）から始めると弱くなりやすい。

### 字幕の位置

画面の**下2割ほどはYouTubeのUI（タイトル・ボタン類）が重なる**。
既定の `subtitle.bottom_ratio: 0.20` はそれを避けた位置にしてある。
実機で確認して隠れるようならこの値を大きくする。

折り返しは文字数だけでなく**実際の描画幅**でも判定している。
日本語と半角英数字では1文字の幅が倍ちがうので、`max_chars_per_line` だけだと
英数字混じりの行が横にはみ出すため。はみ出しが気になる場合は
`subtitle.size_ratio` を下げるか `max_width_ratio` を絞る。

---

## ライセンス面の違い（重要）

**このルートは docs/04 の商用制限を受けない。**
make_short.py は立ち絵PNGとWAVを合成するだけで、
**AITuberKitもLive2D Cubismのランタイムも使っていない**ため。

ただし次は引き続き効く:

| 対象 | 条件 |
|---|---|
| 一枚絵そのもの | 発注・生成元の規約に従う（docs/01 の権利の項） |
| Style-Bert-VITS2 | AGPL-3.0 |
| 使用した音声モデル | **クレジット表記が必須のものがある**（あみたろ系など）。概要欄に記載 |
| BGM・効果音 | 素材ごとの規約。フリー素材でも表記が必要な場合がある |

一方で、**Cubismでリグを組んで動かした映像を使う場合**は docs/04 の
Live2D関連の条件に戻るので注意。ショートを収益化前提で作るなら、
立ち絵PNGのまま扱うのが安全。

### YouTube側の注意

- 縦9:16で3分以内なら自動でショート扱いになる（投稿画面の表示で確認する）
- **同じ内容の使い回しは推奨されない。** 量産する場合も1本ごとに内容の差をつける
- 規約は変わるので、投稿画面とYouTubeヘルプの最新表示を優先する

---

## つまずいたら

| 症状 | 対処 |
|---|---|
| `ffmpegが見つかりません` | `pip install imageio-ffmpeg` |
| `日本語フォントが見つかりませんでした` | `subtitle.font` にフォントのパスを書く |
| 字幕が画面からはみ出る | `subtitle.max_chars_per_line` を減らす／`size_ratio` を下げる |
| 口パクしない | `character.open` を指定したか確認／`mouth.threshold` を 0.05 くらいに下げる |
| 口がパカパカしすぎる | `mouth.threshold` を上げる／`mouth.min_hold_frames` を 3〜4 に |
| 立ち絵が大きすぎる・小さすぎる | `character.height_ratio` を調整 |
| 音が途切れる／尺が合わない | `--probe` で台詞ごとの秒数を確認する |
| 書き出しが遅い | `size` を `[720, 1280]` にして試し、確認後に戻す |

## 動作確認

```powershell
python tests/test_make_short.py
```

立ち絵と音声を合成して実際にMP4を書き出し、解像度・尺・音声トラックを検証する。
**make_short.py を変更したら必ず実行する。**
