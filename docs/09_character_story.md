# 09. 立ち絵つきの物語動画を台本から作る（ずんだもん系）

ずんだもん等のキャラクターが画面の下で口パクしながら話し、ナレーションと字幕で物語が進む動画を、
**ふつうの文章で書いた台本（かんたん台本）**から作る手順。

見た目（声・立ち絵・字幕・場面タイトル・背景・BGM）は **look** という1つのファイルにまとめてあるので、
台本には中身だけを書けばよい。同じ look を使えば、どんな台本でも同じ作風の動画になる。

```
かんたん台本 episodes\xxx.txt   「ずんだもん(笑顔): 今日も余裕なのだ！」のように書く
  ＋ look configs\look_zunda_story.yaml（チャンネルの作風。分析結果に合わせて1回直せば全部の台本に効く）
  ──vlab make──▶ 台本YAML → (フリー素材の取得) → VOICEVOXで声 → 立ち絵の口パク・まばたき・表情 → 字幕 → 音量調整
  ──▶ renders\xxx.mp4 ＋ xxx.ja.srt（字幕） ＋ xxx.credits.txt（概要欄に貼るクレジット）
```

## 準備（初回のみ）

1. docs/06 Step 1 の環境（`.venv-video`・ffmpeg）
2. **VOICEVOX** をインストールして起動しておく（docs/07）。話者IDを確かめて、look の `voices` に合わせる:

   ```powershell
   .venv-video\Scripts\python.exe scripts\vlab.py voices
   ```

   look の初期値は ずんだもん（ノーマル）= 3、ナレーター = 玄野武宏（ノーマル）= 11。
   IDはVOICEVOXのバージョンで変わることがあるので、一覧で名前とIDを必ず確認する
3. **立ち絵の素材**を用意する（下の「立ち絵の用意」）。届くまでは仮の立ち絵で流れを確かめられる:

   ```powershell
   .venv-video\Scripts\python.exe scripts\make_placeholder_character.py assets\chars\zundamon
   ```

   仮の立ち絵は単純な図形のオリジナルキャラ（口パク・まばたき・5つの表情つき）。本番では差し替える

## 立ち絵の用意

ずんだもんは「東北ずん子・ずんだもんプロジェクト」のキャラクター。**公式の利用ガイドライン
（キャラクター利用の手引き）に沿って**使う。立ち絵は、公式サイトで配布されているものか、
ガイドラインに沿って配布されている立ち絵素材を入手する（配布者ごとに利用規約がある）。

- このツールはキャラクターの絵を作らない。AIで似せた絵を生成するのもやめる
- 使う前に、手引きと配布者の規約で次を確認する: 収益化する動画で使えるか／クレジットの書き方／
  加工（表情ごとのPNG書き出し・左右反転）してよいか／禁止されている使い方
- 確認した内容は look の `credits` に書く（`credits.txt` に出て、概要欄に貼れる）

### ファイルの置き方

`assets\chars\zundamon\` に、表情ごとに次のPNG（背景が透明なもの）を置く。`assets/` の中はGitに入らない。

| ファイル名 | 内容 | 無いと |
|---|---|---|
| `<表情>.png` | 口を閉じた絵（**必須**） | その表情は使えない |
| `<表情>_open.png` | 口を開けた絵 | 口パクしない |
| `<表情>_half.png` | 口が半開きの絵 | 開き/閉じの2段階で口パク |
| `<表情>_blink.png` | 目を閉じた絵 | まばたきしない |

表情の名前は **normal / happy / sad / surprised / angry** にそろえるのがおすすめ
（台本では 普通 / 笑顔 / 悲しい / 驚き / 怒り の日本語でも書ける）。足りない表情は作らなくてよい
（台本でその表情を使うとエラーで教えてくれる）。全部のPNGは**同じ大きさ・同じ位置**で書き出すこと
（ずれると口パクの時に絵が動いて見える）。

配布素材がPSD（レイヤーで表情や口を切り替える形式）の場合は、画像編集ソフト
（無料ならブラウザで動く Photopea など）でレイヤーの表示を切り替えて、上の組み合わせを1枚ずつPNGで書き出す。

## かんたん台本の書き方

雛形を作る（`.txt` にすると、かんたん台本の雛形になる）:

```powershell
.venv-video\Scripts\python.exe scripts\vlab.py new-episode "締め切りの話" -o episodes\deadline.txt
```

見本は `episodes/sample_story.txt`。

```text
look: configs/look_zunda_story.yaml
# 締め切りをいつも後回しにしたずんだもんの末路

## 月曜日の朝 [タイトル: 月曜日]
ナレーター: これは、締め切りをいつも後回しにしてきた、ずんだもんの物語です。
ずんだもん(笑顔): 今週の仕事は、金曜までに出せばいいのだ。
そう言って、ずんだもんは動画を見始めました。

## 木曜日の夜 [タイトル: 木曜日の夜]
ずんだもん(驚き): ……え、もう木曜日なのだ！？ [強調: 締め切りまで あと1日] [効果音: assets/se/shock.wav]

## まとめ [出演: なし]
先延ばしは、未来の自分に仕事を押しつけることでもあります。
```

| 書き方 | 意味 |
|---|---|
| `look: ファイル` | 見た目の共通設定（最初の場面より前に1回。`vlab make` の `--look` でも指定でき、そちらが優先） |
| `# タイトル` | 動画のタイトル |
| `## 場面の名前 [ … ]` | 場面の区切り。名前はメモ（画面には出ない） |
| `話者(表情): セリフ` | 台詞。表情は省略可（前の表情が続く）。（）や：の全角も可 |
| 話者名の無い行 | ナレーション（look の `default_speaker`） |
| `// …` | メモ（動画に入らない） |

話者として扱われるのは look の `voices` / `characters` にある名前だけ。それ以外の「○○: …」は
ナレーションの文として読まれる（例「注意: …」）。

**場面の [ ]**

| 書き方 | 内容 |
|---|---|
| `[タイトル: 3日後…]` | 場面の頭に大きく出す文字 |
| `[背景: assets/images/room.png]` | 背景の画像（無ければ look の `default_visual`） |
| `[動画: assets/video/rain.mp4]` | 背景の動画 |
| `[素材: rainy street]` | フリー動画素材の検索語（docs/08。APIキーが要る。`--draft` なら仮カード） |
| `[色: #102030]` | 単色の背景 |
| `[出演: ずんだもん, めたん]` / `[出演: なし]` | この場面に出す立ち絵（省略時は look の `cast`） |
| `[BGM: assets/bgm/sad.mp3]` | この場面から流すBGM |
| `[効果音: assets/se/door.wav]` | 場面の頭で鳴らす音 |
| `[間: 1.5]` | 場面の後の間（秒） |

**台詞の後ろの [ ]**

| 書き方 | 内容 |
|---|---|
| `[強調: あと1日]` | 画面中央に「ポン」と出す大きな文字 |
| `[字幕: 短い文]` | 字幕だけ別の文にする（声は台詞のまま） |
| `[効果音: assets/se/shock.wav]` | 台詞の頭で鳴らす音 |
| `[表情: ずんだもん=悲しい]` | 話していないキャラの表情も変える（カンマで複数） |
| `[間: 0.8]` | 次の台詞までの間（秒） |

画像・音のパスは、作業フォルダ（ふつうはリポジトリ直下）か台本のフォルダからの書き方でよい。

## 作る

```powershell
# 1) 半分の解像度で速く試す（未配置の素材は「素材TODO」の仮カード、無い効果音・BGMは飛ばす）
.venv-video\Scripts\python.exe scripts\vlab.py make episodes\deadline.txt --draft
# 2) 本番（フルHD）
.venv-video\Scripts\python.exe scripts\vlab.py make episodes\deadline.txt
# VOICEVOXを起動していなくても流れだけ確かめる（仮の音声）
.venv-video\Scripts\python.exe scripts\vlab.py make episodes\deadline.txt --tts dummy --draft
```

- `make` は、台本YAML（`episodes\deadline.yaml`）への変換 → `[素材: …]` の取得 → 制作 を続けて行う
- 変換だけしたい時は `vlab script episodes\deadline.txt`。台本を直したら変換し直す
  （自動変換したYAMLを手で直した場合は、名前を変えて `vlab produce` で使う。手で書いたYAMLは上書きされない）
- できあがり: `renders\deadline.mp4`、字幕 `deadline.ja.srt`、クレジット `deadline.credits.txt`
  （VOICEVOXの「VOICEVOX:ずんだもん」などは自動で入る。立ち絵・BGM・効果音のクレジットは look か台本の `credits` に書く）
- 参考動画のテンポに近いかを測るなら `--check`（docs/07。目標スタイルは look の `style:`）

## look（見た目の共通設定）を直す

`configs/look_zunda_story.yaml` の中身は台本YAML（episodes/README.md）と同じ項目で、台本の下敷きになる。
台本の側に同じ項目があれば台本が優先（`credits` は両方を足す）。

| 項目 | 内容 |
|---|---|
| `style` | 目標スタイル（素材の長さ・つなぎ方・話速・音量）。分析で作った `configs/style_<名前>.yaml` |
| `default_speaker` | 話者名の無い行を読む声 |
| `voices` | 話者ごとの声（VOICEVOXの話者ID・話速） |
| `characters` | 立ち絵（下表） |
| `cast` | 立ち絵を出すキャラの既定（場面の `[出演: …]` で変えられる） |
| `default_visual` | 背景の指定が無い場面の背景（例 `{type: image, path: assets/images/room.png}`） |
| `telop` | 字幕の大きさ・位置・1枚の文字数・縁取り。`speaker_styles` で話者ごとの色 |
| `title_style` / `callout_style` | 場面タイトル / `[強調: …]` の見た目 |
| `bgm` | 全体に流すBGM（`- {file: assets/bgm/xxx.mp3}`） |
| `credits` | 概要欄に載せるクレジット（立ち絵・BGMなど） |

**characters の項目**（`ずんだもん: {dir: …, x: 0.82, …}`）

| キー | 既定 | 内容 |
|---|---|---|
| `dir` | | 立ち絵のフォルダ（上の「ファイルの置き方」） |
| `default` | normal | 最初の表情 |
| `x` | 0.84 | 立ち絵の中心の横位置（画面比。0=左端、1=右端） |
| `height` | 0.72 | 立ち絵の高さ（画面の高さ比） |
| `bottom` | 1.03 | 立ち絵の下端（1より大きいと少し下にはみ出す） |
| `flip` | false | 左右反転（左側に置くキャラに。素材の規約で反転してよいか確認） |
| `hop` | 0.012 | 話し始めの小さな跳ね（0で無し） |
| `breathe` | 0.003 | 待機中のゆっくりした上下（0で無し） |
| `silent_dim` | 0 | 話していない時に暗くする度合い（0〜1。掛け合いで話者を目立たせる時に） |
| `blink_every` | [2.5, 5.5] | まばたきの間隔（秒の範囲） |

2人を出すなら、もう1人を `x: 0.18, flip: true` のように左に置き、`cast: [ずんだもん, めたん]` にする。

## 分析結果を反映する（PCで参考チャンネルを分析した後）

`prompts/channel_style_analysis.md` の手順で作った `configs/style_spec_<名前>.md`（見せ方の仕様）を見ながら、
look の値を直す。直したら見本の台本で作って、参考動画と同じ場面を見比べる。

| 見せ方の仕様 | look の項目 |
|---|---|
| 立ち絵の位置・大きさ | `characters.<名前>.x` `height` `bottom` |
| 立ち絵の動き（跳ね・揺れ・話していないキャラ） | `hop` `breathe` `silent_dim` `blink_every` |
| 字幕の位置・大きさ・文字数・縁取り | `telop.y` `size` `max_chars` `outline_width`、帯は `band` |
| 話者ごとの字幕の色 | `telop.speaker_styles` |
| 場面タイトル・強調文字 | `title_style` / `callout_style` |
| 背景の種類 | `default_visual`（場面ごとは台本の `[背景: …]`） |
| テンポ・つなぎ方・話速・音量 | `style: configs/style_<名前>.yaml`（`vlab aggregate` で作ったもの） |

```powershell
.venv-video\Scripts\python.exe scripts\vlab.py make episodes\sample_story.txt --draft
.venv-video\Scripts\python.exe scripts\vlab.py frames renders\sample_story.mp4 5 7    # 5〜7秒を1コマずつ並べた画像
```

## 困ったとき

| 症状 | 直し方 |
|---|---|
| 「立ち絵のフォルダがありません」 | 素材を `assets\chars\…` に置く／仮の立ち絵を作る／`--draft` なら立ち絵なしで進む |
| 「'ずんだもん' に表情 'xxx' がありません」 | ファイル名（`<表情>.png`）と台本の表情名をそろえる。エラーに使える表情の一覧が出る |
| 口パクしない／まばたきしない | `<表情>_open.png`／`<表情>_blink.png` が無い |
| 話者の台詞がナレーターの声になる | look の `voices` に話者名が無い（表記ゆれ。全角・半角も別の名前になる） |
| 字幕が立ち絵に重なる | `telop.max_chars` を減らす、`characters.x` を端に寄せる、`telop.y` を変える |
| 「手で書いた台本のようなので上書きしません」 | 同じ名前のYAMLがある。`-o`（make は `--out-episode`）で別の名前にする |
| 「[素材: …] がありますが、APIキーがありません」 | docs/08 のキーを入れる、または `--draft` で仮カードのまま試す |

## 守ること

- **キャラクターの絵を作らない・似せて生成しない**。立ち絵は配布されている素材を、規約の範囲で使う
- 声は VOICEVOX と各キャラクターの音声の利用規約に従い、クレジット（例「VOICEVOX:ずんだもん」）を入れる
- 物語・台詞・タイトルは**毎回オリジナル**にする。参考チャンネルの台詞・シリーズ名・決め台詞・固有の設定は使わない
  （学ぶのはテンポと見せ方の品質）
- YouTubeの収益化では、同じ型の使い回しは「繰り返しの多いコンテンツ」とみなされるおそれがある。
  話の中身・構成・見せ場を毎回変える
