# エピソード台本の書き方（episodes/*.yaml）

1本の動画 = 1つのYAMLファイル。**台詞を書けば、尺・カット割り・カメラワーク・テロップ・音量は
目標スタイル（`configs/style_profile.yaml`）に合わせて自動で決まる。**

```bash
python scripts/vlab.py new-episode "もしも〇〇したら" -o episodes/my_first.yaml   # 雛形を作る
python scripts/vlab.py produce episodes/my_first.yaml --tts dummy --draft        # 音声無しで試し書き出し
python scripts/vlab.py produce episodes/my_first.yaml --check                    # 本番 + 採点
```

## 全体の項目

| 項目 | 必須 | 内容 |
|---|---|---|
| `title` | | 動画タイトル（出力ファイル名は台本のファイル名から付く） |
| `voices` | ○ | 話者名 → 音声エンジン設定（下表） |
| `scenes` | ○ | シーンの並び（下記） |
| `telop` | | テロップの見た目の上書き（下表） |
| `title_style` | | シーン冒頭の大見出しの見た目（`size` `color` `outline` `y` `duration`） |
| `callout_style` | | 強調文字（台詞の `callout:`）の見た目（`size` `color` `outline` `y`） |
| `bgm` | | BGMのリスト。`{file, from_scene, to_scene, volume_db}` |
| `sources` | | 台本の根拠（URL・書籍）。**公開前に必ず確認する** |
| `credits` | | 概要欄に載せる素材クレジット（`credits.txt` に書き出される） |
| `style` / `output` / `resolution` / `fps` | | 目標スタイル・出力先・解像度・fpsの上書き |

## voices（話者）

```yaml
voices:
  ナレーター: {engine: voicevox, speaker: 11, speed: auto}
  教授: {engine: voicevox, speaker: 42, speed: auto, pitch: -0.02, intonation: 1.1}
  助手: {engine: sbv2, model_id: 0, speaker_id: 0, style: Neutral, speed: 1.05}
```

| キー | 内容 |
|---|---|
| `engine` | `voicevox` / `aivis`（AivisSpeech）/ `sbv2`（Style-Bert-VITS2）/ `dummy`（仮音声） |
| `speaker` | VOICEVOX/AivisSpeechの話者ID。`python scripts/vlab.py voices` で一覧 |
| `speed` | `auto` = 目標スタイルの話速（文字/秒）に合わせて自動調整。数値なら固定倍率 |
| `pitch` `intonation` `volume` | VOICEVOXの音高・抑揚・音量 |
| `url` | エンジンのURLを変える場合（既定: 50021 / 10101 / 5000番ポート） |
| `credit` | クレジット表記を手で指定する場合 |

## scenes（シーン）

```yaml
scenes:
  - id: hook                      # 省略可（scene1, scene2…）
    title: 月が半分の距離に来たら   # 冒頭に大見出しを出す（省略可）
    lines:                        # 台詞。この合計の長さがシーンの尺になる
      - {speaker: ナレーター, text: "もしも、月が今の半分の距離まで近づいたら……？"}
      - {speaker: 教授, text: "潮を起こす力は8倍じゃ。", telop: "潮を起こす力 → 8倍"}
      - {speaker: ナレーター, text: "見た目の大きさは2倍です。",
         visual: {type: image, path: assets/images/moon_big.png}}   # この台詞から画を切り替える
    visuals:                      # シーンの映像素材。尺に応じて自動でカットを割り、順番に使う
      - {type: space, template: planet, params: {preset: moon}, camera: zoom_in}
      - {type: image, path: assets/images/tide.png, camera: {kind: pan_right, amount: 0.1}}
    se:                           # 効果音（シーン先頭からの秒数）
      - {file: assets/se/don.wav, at: 0.0, volume_db: -2}
    pause_after: 0.8              # シーン後の間（秒）
```

- 台詞の `telop:` は画面に出す文字を別にしたい時（`false` でテロップ無し）
- 台詞の `callout:` は画面中央に「ポン」と出す強調文字（例 `callout: "潮を起こす力 8倍"`）。
  見た目は全体の `callout_style:`（`size` `color` `outline` `y`）で変えられる
- 台詞ごとの `gap:` で次の台詞までの間（秒）を個別に指定できる
- 台詞の無いシーンは `duration: 3.0` のように秒数を書く（アイキャッチ等）

## visuals（映像素材）の種類

| type | 書き方 | 内容 |
|---|---|---|
| `space` | `{type: space, template: planet, params: {...}}` | 宇宙シーンをプログラムで描画（素材不要）。Blenderがあれば3DCG、無ければ2D版 |
| `image` | `{type: image, path: assets/images/x.png}` | 静止画にゆっくりズーム/パン（ケン・バーンズ） |
| `video` | `{type: video, path: assets/video/x.mp4, start: 2.0}` | 動画素材（短ければループ） |
| `color` | `{type: color, color: "#0b1d3a", color2: "#1e5aa8"}` | 単色/グラデーションの下地（図解・文字だけの場面） |

### overlays（背景の上に重ねる画像）

体験役のキャラクター・矢印・図などの**透過PNG**を、どの映像素材の上にも重ねられる。

```yaml
visuals:
  - type: space
    template: planet
    params: {preset: jupiter}
    overlays:
      - {path: assets/chars/explorer.png, x: 0.72, y: 0.58, height: 0.55, anim: float, enter: slide_left}
      - {path: assets/images/arrow.png, x: 0.4, y: 0.4, height: 0.2, anim: none, enter: pop}
```

| キー | 内容 |
|---|---|
| `x` `y` | 画像の中心の位置（画面比。0.5, 0.5 が中央） |
| `height` | 画像の高さ（画面の高さ比） |
| `anim` | `float`（ふわふわ）/ `shake`（小刻みに揺れる）/ `breathe`（ゆっくり伸縮）/ `none` |
| `enter` | 出方: `fade` / `slide_left` / `slide_right` / `slide_up` / `pop` / `none` |
| `opacity` `flip` | 不透明度（0〜1）、左右反転 |

キャラクターは**オリジナルのデザイン**にする（参考チャンネルのキャラクターに似せない）。

### camera（カメラワーク）

`camera` は `static` / `zoom_in` / `zoom_out` / `pan_left` / `pan_right` / `tilt_up` / `tilt_down` /
`orbit`（宇宙シーンのみ）。省略すると目標スタイルのカメラワーク比率に合わせて自動で選ぶ。
`{kind: zoom_in, amount: 0.1, easing: ease_in_out}` で量（10%）とイージングも指定できる。

### space のテンプレート

`python -c "from videolab.produce.space import describe_templates; print(describe_templates())"`
で全パラメータの説明を表示できる。

| template | 主なparams |
|---|---|
| `planet` | `preset`（earth / mars / jupiter / saturn / moon / venus / neptune / ice / lava）, `size`, `position`, `rotation_speed`, `sun_angle`, `texture`（正距円筒図法の画像） |
| `planet_compare` | `presets: [a, b]`, `size_ratio`（bの直径÷aの直径） |
| `sun` | `color`, `activity`, `size` |
| `black_hole` | `disk_color`, `tilt`, `size` |
| `starfield` | `speed`（0=静止、>0で前進）, `density` |

## telop（テロップの見た目）

| キー | 既定 | 内容 |
|---|---|---|
| `font` | `auto` | フォントファイル。`assets/fonts/` に置けば自動で使う |
| `size` | 0.062 | 文字の大きさ（画面の高さ比） |
| `color` / `outline` | 白 / 黒 | 文字色 / 縁取り色 |
| `outline_width` | 0.14 | 縁の太さ（文字サイズ比） |
| `y` | 目標スタイルの実測値 | 縦位置（0=上端、1=下端） |
| `max_chars` | 22 | 1枚に入れる最大文字数（超えたら句読点・文節で分割） |
| `band` | 無し | 帯を敷く場合の色（例 `"#00000099"`） |
| `speaker_colors` | 無し | 話者ごとの文字色 `{教授: "#b8f5c8"}` |

## 守ること

- **登場人物・台詞・設定はオリジナル**にする。参考チャンネルのキャラクター名・見た目・決め台詞・
  固有の世界観・タイトルの型をそのまま使わない（作風＝テンポや品質を学ぶのはOK）
- 数字や主張には `sources:` に根拠を書き、公開前に自分で確認する
- 素材は自作・フリー素材・生成素材だけ（`assets/README.md`）。`refs/` と `analysis/` のファイルは
  制作に使えない（使おうとするとエラーで止まる）
