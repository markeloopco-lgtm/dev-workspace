# 06. AI動画生成パイプライン（クラウド無料GPU × MacBook仕上げ）

リアル系の縦型ショート動画（犬など）を無料で量産するための工程。
Instagram Reels / YouTube Shorts / TikTok に投稿できる形まで持っていく。

## 役割分担

| 場所 | 役割 | 使うもの |
|---|---|---|
| Kaggle（無料GPU・週30h） | **動画の生成** | `notebooks/video_gen_free_gpu.ipynb` |
| MacBook (M5 / 16GB) | **仕上げ・結合・BGM** | `scripts/finish_reel.py` |

### なぜ生成をMacでやらないのか

Apple Siliconでの動画生成は、無料でも技術的には可能だが実用速度に届かない。

- **M1 Max 64GB でも Wan 2.2 で2秒のクリップに82分**かかるという実測報告がある
- Metal が `Float8_e4m3fn` を実装しておらず、**FP8のモデルが動かない**（GGUF等への回避が必要）
- Mac最速の無料アプリ Draw Things でも、**動画生成は24GB以上のメモリを推奨**

M5/16GBはこの推奨を下回るため、生成はクラウドに出し、Macは編集に専念させる。

## Kaggle側で押さえるべき制約

無料GPUのT4は**Turing世代**で、ネット上の作例が前提にしている機能を欠く。

- **bf16 非対応** → 作例をそのままコピペすると落ちる。ノートブックは自動でfp16に切り替える
- **FP8 非対応** → FP8量子化モデルは選べない
- **Wan 2.2 はVAEデコード時にVRAMを使い切るOOM報告**がある（diffusers issue #12097）
  → ノートブックは `vae.enable_tiling()` と `enable_model_cpu_offload()` で回避する

## モデルの選び方

ノートブックの `MODEL` で切り替える。

| 値 | モデル | 特徴 |
|---|---|---|
| `'wan'`（既定） | Wan 2.2 TI2V-5B | 品質が高くリアル寄り。遅い。**本番用** |
| `'ltx'` | LTX-Video 0.9.7 distilled | 数ステップで生成でき速い。品質は落ちる。**構図の試し撮り用** |

進め方の推奨: まず `ltx` でプロンプトの当たりを付け、良さそうなものだけ `wan` で本生成する。
無料枠（週30時間）の消費を大きく抑えられる。

### 解像度とフレーム数の決まりごと

モデルごとに刻みが決まっており、外すとエラーになる。

- **Wan**: 幅・高さは **16の倍数**、フレーム数は **4n+1**（例: 480x832 / 81フレーム）
- **LTX**: 幅・高さは **32の倍数**、フレーム数は **8n+1**（例: 480x832 / 121フレーム）

既定の `480x832` は縦型（9:16）で、両モデルの条件を同時に満たす。
生成時点では小さめに作り、仕上げ側で1080x1920へ引き伸ばす。

## 手順

### 1. 初回のみ: Mac環境のセットアップ

```bash
bash scripts/setup_mac_video.sh
```

Homebrew の確認 → ffmpeg 導入 → `.venv` 作成 → 作業フォルダ作成 → 動作確認まで自動で行う。
Homebrew が未導入の場合は、案内に従って先に入れてから再実行する。

### 2. Kaggleで生成する

1. [kaggle.com](https://www.kaggle.com/) に登録し、`notebooks/video_gen_free_gpu.ipynb` をアップロード
2. 右パネル Session options で **Accelerator: GPU T4 x2** / **Internet: ON**
3. 上からセルを順に実行
4. セル3で `MODEL` と解像度、セル6で `PROMPTS` を編集
5. 最後のセルで zip をダウンロード

初回はモデル重みのダウンロードに10〜20分かかる。
**プロンプトをまとめて1セッションで回す**のが無料枠の節約になる。

### 3. Macで仕上げる

zipを展開して `video_input/` に置いてから:

```bash
source .venv/bin/activate

# 1本を投稿用（1080x1920）に整える
python scripts/finish_reel.py video_input/clip_001.mp4

# 複数本をつないで1本にする
python scripts/finish_reel.py video_input/*.mp4 --concat -o video_out/reel_final.mp4

# BGMを乗せる（ラウドネスは-14 LUFSに自動調整）
python scripts/finish_reel.py video_input/clip_001.mp4 --bgm assets/bgm.mp3

# 全体を収めて黒帯にする（既定は切り抜きの cover）
python scripts/finish_reel.py video_input/clip_001.mp4 --fit contain
```

出力は `video_out/` に入る。

`finish_reel.py` が揃えているもの:

- 1080x1920 / 30fps / H.264 / yuv420p
- **正方形ピクセル（SAR 1:1）** — これを揃えないと9:16として正しく表示されない
- **音声トラックを必ず1本**（元が無音なら無音トラックを付ける）
  — クリップ間で本数が食い違うと結合時に壊れるため
- `+faststart`（再生開始が速くなる）

## プロンプトの書き方

リアル系を狙うときに効くもの:

- **被写体の具体性**: `fluffy shiba inu puppy` のように犬種と毛質まで書く
- **カメラの言葉**: `shallow depth of field`, `handheld shot`, `slow motion`
- **光**: `soft natural window light`, `golden hour` — 質感がリアルに寄る
- **仕上げ**: `photorealistic`, `4k`

ネガティブプロンプトは既定で崩れ・余分な脚・透かしを抑えるよう入れてある。
犬は**脚の本数と関節が破綻しやすい**ので、生成後に必ず目視で確認すること。

## ライセンス上の注意（投稿前に確認）

- **Wan 2.2 / LTX-Video のライセンス**を、商用利用するなら必ず自分で確認すること。
  モデルごとに条件が異なり、改定されることもある
- **BGM**は権利の切れたものかフリー素材を使う。Instagramの音源機能は
  アプリ側で付ける方が安全な場合がある
- AI生成物であることの**表示義務**が各プラットフォームで定められている。
  Instagram/YouTubeともにAI生成コンテンツのラベル付けが求められる

## 検証状況

- `scripts/finish_reel.py`: 規格ちがいのクリップ（縦/横・音声あり/なし）で
  単体・結合・BGM・contain・異常系を実機確認済み
- `notebooks/video_gen_free_gpu.ipynb`: **GPUの無い環境で作成したため実行未検証**。
  エラーが出たらセルの出力を添えて相談すること
