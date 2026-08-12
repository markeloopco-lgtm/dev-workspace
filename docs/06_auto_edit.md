# 06. 自動編集 (ジェットカット + テロップ + BGM)

録画した動画(配信アーカイブ・解説動画など)を**コマンド1回**で
「無音カット + テロップ + BGM」に仕上げるツール。
全部無料・ローカル処理(アップロード不要)。

## テロップの様式 (preset)

`configs/auto_edit.yaml` の `preset:` で切り替える。既定は `talk`。

| preset | 見た目 | 参考にした型 |
|---|---|---|
| **talk**（既定） | 座布団なし。**話者ごとに縁取りの色を変える**。カットは最も詰める | 年収チャンネルなどの対談 |
| **business** | 座布団なし。白文字＋**紺(#294069)の太い縁取り**＋影。キーワードと数字が黄色 | マコなり社長などビジネス系YouTube |
| **news** | 画面下の全幅の帯＋金のアクセントライン | 報道番組 |

コマンドごとに `--preset` で一時的に上書きもできる。

```powershell
python scripts/auto_edit.py preview --preset talk
python scripts/auto_edit.py preview --preset business
python scripts/auto_edit.py preview --preset news
```

## talk preset（年収チャンネル型）の中身

制作元が公開している編集方針をそのまま設定に落としてある。

| 要素 | 実装 | 根拠 |
|---|---|---|
| **カットの詰め** | 発話の前後に**1.5フレームずつ**残す → つなぎ目の無音が**ちょうど3フレーム** | 「発声と発声の間は常に2〜3f」 |
| **話者の区別** | 話者ごとに**文字色**を変える（`speakers:`）。放送の字幕と同じ白・水色の使い分け | 「イメージカラーから誰の発言か分かるように」 |
| **ツッコミ・ボケ** | SRTの行頭に `!` を付けると**拡大＋別フォントに切替**（`scenes:`） | 「スケール拡大しながらフォントを変えている」 |
| **落ち着いた雰囲気** | 背景やグラデーションを足さず、縁取りと影だけで映像から浮かせる | 「落ち着いた雰囲気を崩さないためエフェクトなし」 |
| **数字の強調** | 金額・年収などを自動で黄色に | 年収を扱うチャンネルの性質＋ビジネス系の定番 |

```
3
00:00:12,700 --> 00:00:16,300
ゲスト: いまは1200万円まで上がりました

4
00:00:16,500 --> 00:00:18,000
株本: !それ盛ってないですか
```

**フォント名・文字サイズ・カラーコードは公開されていない**ので、そこは上の方針と
対談動画の一般則（全発言をテロップ化する／中央揃え／視認性重視の角ゴシック）から
決めている。実際の動画と見比べて `configs/auto_edit.yaml` で微調整してほしい。

座布団を使いたくなったら `band.enabled: true` に変えると、文字幅に合わせて伸縮する
角丸の座布団になる。その場合、座布団の色は**暗めにする**こと（白文字を載せるため）。
明るすぎる色を指定すると警告が出る。

```
入力動画 (mp4など)
  │
  ▼ ① ffmpeg silencedetect で無音検出 ──→ ジェットカット計画 (plan.json)
  │
  ▼ ② faster-whisper で文字起こし ──→ 字幕ファイル (telop.srt) ←誤認識はここを手直し
  │
  ▼ ③ テロップ生成 (preset に応じた書体・縁取り・強調色)
  │
  ▼ ④ カット + テロップ焼き込み + BGMミックス を1回のエンコードで実行
  │
出力: 入力名_edited.mp4
```

## セットアップ (Windows PowerShell・全部無料)

```powershell
# ① ffmpeg (動画処理エンジン)
winget install Gyan.FFmpeg
# → 終わったらPowerShellを開き直す (PATH反映のため)

# ② Python側の依存 (文字起こしAI faster-whisper など)
pip install -r requirements-autoedit.txt
```

確認: `ffmpeg -version` と `python scripts/auto_edit.py --help` が表示されればOK。

## クイックスタート

```powershell
# ① まずテロップの見た目を1枚だけ確認する (数秒で終わる)
python scripts/auto_edit.py preview 録画.mp4 --title "AITuberニュース"
#    → telop_preview.png ができるので開いて確認

# ② よければ全自動 (無音カット + テロップ) → 録画_edited.mp4
python scripts/auto_edit.py run 録画.mp4 --title "AITuberニュース"

# BGM付き
python scripts/auto_edit.py run 録画.mp4 --bgm bgm.mp3 --title "AITuberニュース"

# テロップなしでジェットカットだけ
python scripts/auto_edit.py run 録画.mp4 --no-telop
```

初回だけ文字起こしモデル(約500MB)のダウンロードで数分かかる。
2回目からはすぐ始まる。

## フォントを増やす

Google Fonts から日本語フォントを取ってこられる（全て無料・商用可）。
インストール作業は不要で、`assets/fonts/` に置くだけで使えるようになる。

```powershell
python scripts/fetch_fonts.py list                  # 選べる20書体の一覧
python scripts/fetch_fonts.py get ゴシック太         # 1つだけ取得
python scripts/fetch_fonts.py get --all-recommended # ビジネス系の定番をまとめて
python scripts/fetch_fonts.py get --all             # 一覧の20書体すべて
```

取得したら `configs/auto_edit.yaml` の `font_candidates` の先頭に書くか、
`font: "Noto Sans JP"` のように直接指定する。今どれが使われているかは
`python scripts/auto_edit.py fonts` で確認できる。

## 場面ごとにフォントを切り替える（scenes）

同じ動画の中で、通常の発言・ツッコミ・要点でテロップの見た目を変えられる。
SRTの**行頭に記号を付けるだけ**で切り替わる。

| 記号 | シーン | 既定の動き |
|---|---|---|
| （なし） | 通常の発言 | 標準のフォント・大きさ |
| `!` | ツッコミ・ボケ | 拡大＋手書き風フォント |
| `#` | 要点・結論 | 拡大＋インパクトのあるフォント |
| `?` | 質問 | **文末が「？」なら記号なしでも自動判定** |

```
5
00:00:19,200 --> 00:00:22,800
ゲスト: !それ盛ってないですか

6
00:00:23,000 --> 00:00:26,000
株本: #行動した人だけが変わる
```

各シーンのフォント・拡大率・色は `configs/auto_edit.yaml` の `scenes:` で変える。
シーンを増やしたいときは同じ形式で項目を足し、好きな記号を `mark:` に書く。

## 文字の大きさ（1行と2行で切り替わる）

`style.font_size_ratio` が1行のとき、`font_size_ratio_wrapped` が2行のときの大きさ。
1行なら大きく見せて、2行に折り返したら自動で小さくする、という使い方ができる。

```yaml
style:
  font_size_ratio: 0.080          # 1行のとき (1080pで約86px)
  font_size_ratio_wrapped: 0.064  # 2行のとき (同 約69px)
```

同じ数値でも**フォントによって見た目の大きさが変わる**（UDゴシックは字面が大きい）。
書体を決めてから大きさを詰めるのが早い。

長い1行が画面からはみ出しそうなときは、**収まるところまで自動で縮む**ので
文字が画面外に切れることはない。

## キーワードの色替え（business / talk）

ビジネス系YouTubeの特徴である「大事なところだけ色を変える」を自動でやる。
3つの効き方があり、`configs/auto_edit.yaml` の `emphasis:` で調整する。

| やり方 | 例 | 設定 |
|---|---|---|
| **数字を自動で強調** | 年収**1000万円**、上位**3割** | `auto_numbers: true`（既定でON） |
| **決めた語を常に強調** | **結論**から言うと | `keywords: ["結論", "圧倒的"]` |
| **その場で指定** | SRTに `*ここだけ*` と書く | 文字起こしを直すついでに囲む |

「1000万円」「Live2D」のような**数字や英単語が改行で割れない**ようにしてある。

## 対談動画で話者を色分けする（talk）

**誰の発言かを色で示す**やり方。`configs/auto_edit.yaml` に話者と色を書き、
SRTを「名前: 発言」の形にする。名前は自分の動画に合わせて変える。

```yaml
speaker_color_target: text   # 色をどこに使うか (下の表)
speakers:
  株本: "FFFFFF"     # 進行役: 白 (基本色)
  ゲスト: "7FE3FF"   # ゲスト: 水色
```

放送の字幕も同じ考え方で、白を基本に黄・水色で話者を分けている。
3人目以降を足すときも明るい色を選ぶ。

| `speaker_color_target` | 色の使いどころ | 向いている場面 |
|---|---|---|
| `text`（既定） | **文字色** | 背景なしのテロップ。一番はっきり分かる |
| `outline` | 縁取り | 文字色は白で統一したいとき |
| `band` | 座布団 | `band.enabled: true` と併用。色は暗めにする |
| `both` | 文字色＋縁取り | かなり強く分けたいとき |

色が読みにくい向き（文字色なのに暗い、座布団なのに明るい）だと警告が出る。

```
2
00:00:06,200 --> 00:00:09,800
ゲスト: 前職の年収は600万円くらいでした
```

登録していない名前は普通の発言として扱うので、「結論: 〜」のような
書き出しが誤って話者と判定されることはない。

文字起こしは**誰の発言かまでは判定できない**ので、`telop.srt` を直すときに
名前を頭に付ける（1本ぶんで数分の手作業）。名前を付けなければ既定の色で表示される。

## フォント（テロップの印象を一番左右する）

`configs/auto_edit.yaml` は `font: auto` になっていて、**PCにあるフォントを
上から順に自動で探して使う**。今どれが使われるかはこれで分かる:

```powershell
python scripts/auto_edit.py fonts
```

| 優先順 | フォント | 入手 |
|---|---|---|
| 1 | **BIZ UDPGothic**（UDゴシック） | **Windows 10/11 に最初から入っている**のでDL不要。字面が大きく読みやすい |
| 2 | Noto Sans JP Black | 源ノ角ゴシック Heavy と同設計の無料版。`fetch_fonts.py` で取得 |
| 3 | メイリオ / 游ゴシック | Windows 標準 |

既定は **BIZ UDPGothic**。Windows標準なので、フォントを何も入れなくてもそのまま動く。
別の書体にしたいときは下の「フォントを増やす」を参照。

### できあがるファイル

| ファイル | 内容 |
|---|---|
| `録画_edited.mp4` | 完成動画 |
| `録画_autoedit/plan.json` | カット計画 (どこを残したか) |
| `録画_autoedit/telop.srt` | 文字起こし結果 (**メモ帳で修正できる**) |

### 誤認識を直したいとき

1. `録画_autoedit/telop.srt` をメモ帳で開いて文字を直す
2. 再レンダー(文字起こしをやり直さないので速い):

```powershell
python scripts/auto_edit.py render 録画.mp4
```

タイミング(時刻)は**カット前の動画基準**のまま直せばよい。
カット後のどこに出すかはツールが自動で計算する。

## 調整のしかた

設定は `configs/auto_edit.yaml`。よくある症状と対処:

| 症状 | 直すところ |
|---|---|
| 語尾や語頭が切れる | `silence_threshold_db` を -40 に下げる / `keep_padding` を 0.15 に増やす |
| 無音・間がまだ残る | `silence_threshold_db` を -30 に上げる / `min_silence` を 0.3 に減らす |
| テンポが細切れすぎる | `min_silence` を 0.6〜0.8 に増やす |
| テロップの誤字が多い | `telop.model` を `medium` に (遅くなる) / telop.srt を手直し |
| BGMがうるさい/静かすぎ | `bgm.volume_db` を増減 (-24で静かめ、-12で大きめ) |
| テロップが小さい/大きい | `style.font_size_ratio` (画面高さ比。0.05〜0.08あたりで調整) |
| 帯が濃い/薄い | `band.opacity` (放送の目安は0.70〜0.85) |
| 帯の色を変えたい | `band.color` と `band.accent_color` (紺+金がニュースの定番) |

見た目を変えたら、動画を書き出さずに `preview` で1枚だけ確認するのが速い。
確定したら `render` をやり直せば反映される（文字起こしは不要）。

### テロップの寸法の考え方

`*_em` が付いた項目は「**文字サイズに対する倍率**」で、`*_ratio` は「画面高さ比」。
どちらも比率なので、解像度が変わっても見た目が崩れない。既定値は放送実務で
言われている目安に合わせてある:

| 設定 | 既定値 | 根拠 |
|---|---|---|
| `font_size_ratio` | preset依存 | テロップの文字サイズは画面縦の6〜8%が目安 |
| `band.padding_em` | 0.52 | 座布団の余白は文字サイズの50〜70% |
| `style.line_height_em` | 1.45 | 日本語の行間は文字サイズの150%前後が下限 |
| `style.outline_em` | 0.05 | 縁取りは文字サイズの5〜10% |
| `style.tracking_em` | -0.02 | 文字を軽く詰める（"プロっぽさ"の決め手と言われる） |
| `band.opacity` | 0.85 | 座布団の不透明度は70〜85%が読みやすい |
| `max_chars_per_line` / `max_lines` | preset依存 | ニューステロップは1行12〜15文字・最大2行 |
| `reading_speed` / `max_duration` | 4.0 / 6.5 | 1秒あたり全角約4文字・1枚は最大6.5秒 |

出典は下記「参考にした放送・字幕の基準」。

### あえてやっていないこと

実務者が「安っぽく見える原因」として挙げているものは既定で避けている:

- **二重縁取り**（`double_outline: false`）— 実務者が「ダサく見えやすい」と挙げているため
- **派手なグラデーション** — news の帯で上端がわずかに透ける程度に留めている
- **テロップ位置がバラバラ** — 位置は画面下に固定
- **画面端いっぱいの文字** — `safe_margin_ratio: 0.05` で左右に余白を確保

## BGMについて

- `--bgm ファイル` または `configs/auto_edit.yaml` の `bgm.file` で指定(指定しなければ付かない)
- 動画の長さに合わせて自動ループ+フェードイン/アウト
- `ducking: true`(既定)で**声が鳴っている間はBGMが自動で小さくなる**(放送と同じ処理)
- 無料BGMの入手先(いずれも規約を読んでクレジット表記の要否を確認):
  - YouTube オーディオライブラリ (YouTube Studio内・収益化でも利用可)
  - DOVA-SYNDROME (要・各曲のライセンス確認)
  - 甘茶の音楽工房 (クレジット表記推奨)

## このPC(RTX 3050 / VRAM 4GB)での動かし方

- 既定設定 (`model: small` + `compute_type: int8`) はそのままでOK。
  GPUが使えない環境でも自動でCPUに切り替わる(遅くなるだけで止まらない)
- エンコードはNVENC(GPUの動画エンジン。VRAMをほぼ使わない)を自動で試し、
  失敗したらCPUエンコード(libx264)に自動フォールバックする
- 精度を上げたいときだけ `model: medium`(CPUだと実時間の数倍かかる)

## うまくいかないとき

| エラー/症状 | 対処 |
|---|---|
| `ffmpeg が見つかりません` | winget でインストール後、**PowerShellを開き直す** |
| `faster-whisper が入っていません` | `pip install -r requirements-autoedit.txt` |
| モデルDLで403/接続エラー | ネットワークが huggingface.co をブロックしていないか確認 |
| テロップが豆腐(□)になる | `python scripts/auto_edit.py fonts` で使えるフォントを確認。何も○が付かなければ `assets/fonts/README.md` の手順でNoto Sans JPを入れる |
| 全編カットされてしまう | マイク音量が小さすぎ。`silence_threshold_db: -45` を試す |

## 動作検証

```powershell
python tests/run_autoedit_selftest.py
```

ffmpegがあれば合成動画で「無音検出→カット→テロップ焼き込み→BGM」まで通しで検証する
(ffmpegが無い環境ではロジックのみ検証してスキップ表示)。

## ライセンスと権利

| もの | ライセンス | 商用配信 |
|---|---|---|
| ffmpeg | LGPL/GPL (ツールとして使うだけ) | ○ |
| faster-whisper | MIT | ○ |
| Whisperモデル | MIT (OpenAI公開) | ○ |
| Noto Sans JP | SIL Open Font License | ○ |
| BGM音源 | 各配布サイトの規約による | 要確認 |

編集対象の動画・音声そのものの権利(ゲーム画面・楽曲など)は別途各規約に従うこと。

## 参考にした基準

既定値の根拠。数値の出どころを残しておくので、好みで動かすときの目安にする。
なお参考にしたのは**編集の作法（書体の太さ・縁取り・カットの詰め方）だけ**で、
各チャンネルのロゴや固有のデザインは使っていない。

### ビジネス系YouTube (business / talk preset)

- マコなり社長のテロップは**源ノ角ゴシック Heavy**、縁取りは**紺 #294069**
  — [マコなり社長のフォントは？ビジネス系完コピ動画の作り方](https://chiripoteto.com/2024/04/06/business_youtube/)
- 年収チャンネルは**発声と発声の間を常に2〜3フレーム**まで詰める。通常テロップは
  座布団を使い、**イメージカラーで誰の発言か分かる**ようにしている。SEは
  ツッコミだけでなく**要点や質問のときにも**入れる
  — [年収チャンネル 制作事例（エンジーニアス）](https://sg.wantedly.com/portfolio/projects/67709)
- 源ノ角ゴシックは Extra Light〜Heavy の7ウェイト。Noto Sans JP は同設計の無料版
  — [源ノ角ゴシックとNoto Sansの違い](https://dtptransit.design/fonts/genno-noto-sans-matome.html)
- ビジネス系は**白文字＋黒(濃色)の縁取り**が標準、強調は赤か黄
  — [見やすいテロップの入れ方](https://omniweb.jp/m25/)、
  [テロップデザインの基本完全ガイド](https://bopeblog.com/telop-design-youtube-font-layout-guide/)

### 放送・字幕の基準 (news preset と共通の土台)

- 1画面2行まで／表示は2秒以上6.5秒以下／1秒あたり全角4文字
  — [日本コンベンションサービス](https://www.convention.co.jp/news/detail/contents_type=16&id=1043)、
  [Netflix日本語ガイドラインの解説](https://www.k-intl.co.jp/blog/B_230412A)
- ニューステロップは1行12〜15文字／座布団の不透明度70〜85%／余白は文字サイズの50〜70%
  — [ニューステロップ完全ガイド](https://www.blue-moon.jp/news-telop-complete-guide/)、
  [テロップベース完全ガイド](https://www.blue-moon.jp/telop-base-complete-guide/)
- 文字サイズは画面縦の6〜8%／基本は角ゴシックの白文字+黒エッジ
  — [テロップ入れのテレビ的ルール](https://pencre.com/telop-rules/)、
  [テロップの最適なサイズと表示時間](https://video-knowledge.com/character_size_time_timing/)
- 報道番組の定番書体は太ゴB101・ゴシックMB101系。無料の代替は源ノ角ゴシック/Noto Sans JP
  — [太ゴB101](https://ja.wikipedia.org/wiki/%E5%A4%AA%E3%82%B4B101)、
  [ゴシックMB101に似たフリーフォント](https://watanabedesign511.info/archives/13711)
- 報道番組向けUDフォントの設計思想（視認性に寄与しない表現を排除）
  — [フォントワークス「テレ朝UD」](https://fontworks.co.jp/life-with-font/reports/0006/)
- 「安っぽく見える」原因（文字詰めをしない／縁が太い・二重／装飾過多／位置がバラバラ）
  — [プロのテロップの作り方](https://note.com/meec/n/n4023f548d5f1)、
  [差がつくテロップ大学（エッジ編）](https://videosalon.jp/series/telop_vol5/)
- セーフゾーンは現行95%前後（旧来は80/90%）
  — [テレビを意識したセーフエリアのサイズ](https://note.com/shigezoo/n/nc1366223a765)、
  [セーフティゾーン解説](https://movie-happy.com/column/video_editing/1355/)
