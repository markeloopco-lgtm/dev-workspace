# 06. 日本語の文字起こし（精度と速度の両立）

配信アーカイブ・録音などを日本語テキストに起こすための補助手順。
Live2D量産パイプライン本体とは独立して使える。

## 結論

**無料のColab（またはKaggle）のGPUで `kotoba-whisper-v2.0-faster` を回す**のが、
精度と速度を両立する現実的な答え。1時間の音声がおおむね2〜5分で終わる。

ノートブックを用意済み: [`notebooks/transcribe_free_gpu.ipynb`](../notebooks/transcribe_free_gpu.ipynb)

## なぜクラウドを使うのか

手元のPC2台は、どちらも文字起こしには向いていない。

| 機材 | 制約 |
|---|---|
| Mac mini 2018 (Intel) | Neural Engineが無く、内蔵GPU(UHD 630)もAI処理に使えない。**CPUのみ**で計算するため実時間の2〜5倍かかる |
| 配信PC (RTX 3050 Laptop) | GPUは使えるが**VRAM 4GB**。large-v3(fp16で約5GB)は載らず、精度を落とすか量子化が必要 |

文字起こしの高速化はここ数年 Apple Silicon の Neural Engine と NVIDIA GPU に最適化する形で
進んできたため、2018年のIntel Macは構造的に不利。CPUの世代の問題ではなく**演算装置が無い**。

## 選択肢の比較（1時間の日本語音声）

| 方法 | 所要時間の目安 | 精度 | 費用 |
|---|---|---|---|
| **Colab無料GPU + kotoba-whisper-v2.0** | **2〜5分** | large-v3相当 | 無料 |
| Kaggle無料GPU + 同上 | 2〜5分 | large-v3相当 | 無料（週30時間） |
| 配信PC + kotoba-whisper-v2.0 | 5〜15分 | large-v3相当 | 無料 |
| 配信PC + whisper medium | 5〜10分 | 実用レベル止まり | 無料 |
| Mac mini + whisper medium (CPU) | 1〜2時間 | 実用レベル止まり | 無料 |
| Mac mini + whisper large-v3 (CPU) | 2〜5時間 | 高い | 無料 |

> 時間はいずれも目安。音声の内容・無音の多さ・その日に割り当てられるGPUで変動する。

### kotoba-whisper-v2.0 とは

OpenAIのWhisper large-v3 を蒸留（distillation）して軽量化し、
日本語音声コーパス ReazonSpeech（約720万クリップ）で学習し直した日本語特化モデル。
**large-v3と同等の日本語精度のまま約6.3倍高速**という位置づけで、
「精度と速度の両立」という要件にそのまま合致する。

`-faster` 付きのリポジトリは CTranslate2 形式への変換版で、`faster-whisper` から読み込める。

派生版もある。必要になったら検討する:

| 版 | 追加機能 | 備考 |
|---|---|---|
| v2.0 | なし（素の文字起こし） | 導入が一番簡単。まずはこれ |
| v2.1 | 句読点の自動付与 | 読み物として整形したいとき |
| v2.2 | 句読点 + 話者分離 | Hugging Face登録と利用規約同意が必要。設定が複雑 |

## 手順A: Colab無料GPU（推奨）

1. Googleアカウントで [Google Colab](https://colab.research.google.com/) を開く
2. `notebooks/transcribe_free_gpu.ipynb` をアップロードして開く
3. メニューの **ランタイム → ランタイムのタイプを変更 → T4 GPU** を選んで保存
4. 上からセルを順に実行する
   - 1) GPU名が表示されればOK。「GPUが見えていません」なら3をやり直す
   - 2) `faster-whisper` の導入（1〜2分）
   - 3) 音声ファイルをアップロード（複数選択可）
   - 4) モデルのダウンロード（初回のみ数分・約1.5GB）
   - 5) 文字起こし実行
   - 6) 結果のzipをダウンロード
5. zipの中に `.txt`（本文）と `.srt`（タイムコード付き字幕）が入っている

### 無料枠の制限は「時間」で来る（容量ではない）

| 種類 | 無料枠の制限 | 文字起こしへの影響 |
|---|---|---|
| **GPU累積使用量** | 上限は非公開の動的な値。超えると**約24時間**GPUに接続できない | ← 実質これだけ気にする |
| 連続実行時間 | 最大12時間（無料はもっと早く切れることもある） | なし |
| アイドル切断 | 90分放置で切断 | なし |
| ディスク | GPUランタイムで約100GB前後 | なし |
| メモリ | 約13GB | なし |

文字起こしはGPU実働が短い（1時間の音声で2〜5分）ため、通常の使い方で上限に届くことはまずない。
消費を無駄にしないためのポイント:

- 終わったら **ランタイム → セッションの管理 → 終了** で必ず切る（繋ぎっぱなしが一番もったいない）
- **複数ファイルをまとめて処理する**（モデルの再ダウンロードを避けられる）
- 動画をそのまま上げるとアップロードで待たされる。音声だけ抜いてから上げると速い

上限に当たった場合は Kaggle に逃げる。Kaggleは **週30時間** という明示枠でカウンターが見えるため、
残量を把握しやすい。ノートブックはどちらでも動くように書いてある。

## 手順A': Kaggle無料GPU

登録・電話番号認証・ファイルの渡し方は [docs/07](07_kaggle_setup.md) にまとめてある。
Kaggleでは音声を右パネルの **「+ Add Input」→「Upload」** から追加する
（ノートブック側が `/kaggle/input/` を自動で探すので、パスの書き換えは不要）。

## 手順B: 配信PC（Windows / RTX 3050）で完結させる

クラウドにファイルを上げたくない場合や、繰り返し使う場合はこちら。

```powershell
python -m venv .venv-asr
.venv-asr\Scripts\pip install faster-whisper
```

```python
from faster_whisper import WhisperModel

model = WhisperModel('kotoba-tech/kotoba-whisper-v2.0-faster',
                     device='cuda', compute_type='float16')
segments, info = model.transcribe('audio.mp3', language='ja', beam_size=5,
                                  vad_filter=True, chunk_length=15,
                                  condition_on_previous_text=False)
for seg in segments:
    print(seg.text.strip())
```

VRAM 4GBでも、蒸留済みで軽い kotoba-whisper なら余裕を持って載る。
`float16` で足りない場合は `compute_type='int8_float16'` に落とす。

## 手順C: Mac mini 2018 で動かす

短い音声（目安10分以内）なら実用範囲。同じコードで device と compute_type だけ変える。

```python
model = WhisperModel('kotoba-tech/kotoba-whisper-v2.0-faster',
                     device='cpu', compute_type='int8')
```

`int8` 量子化でCPUでも2〜3倍速くなるため、素のWhisperをCPUで回すより体感はかなり良い。
それでも長時間音声は現実的でないので、**長いものはColabに投げる**という使い分けにする。

## 精度を上げるコツ

- **入力音質が最優先**。マイク音とBGMが混ざっていると精度が大きく落ちる。
  配信アーカイブなら、可能ならBGM抜きのマイク単独トラックを残しておく
- **固有名詞は `initial_prompt` で与える**。キャラ名・番組名を書いておくと表記ゆれが減る
  （ノートブックの5)セルの `HINT` がこれ）
- **同じ文の無限繰り返し**が出たら、無音・音楽が長い区間が原因のことが多い。
  `vad_filter=True`（無音スキップ）と `condition_on_previous_text=False` で大半は防げる
- **完全自動で完璧にはならない**。固有名詞と同音異義語は最後に目視で直す前提で運用する

## ライセンス

- `faster-whisper` / CTranslate2: MIT
- Whisper（OpenAI）: MIT
- kotoba-whisper: Apache-2.0（配布ページの表記。商用利用の可否は使う直前にモデルカードで再確認すること）

このプロジェクトのLive2D部分は現状 **商用不可**（docs/04 参照）なので、
文字起こし結果の用途もその範囲に合わせる。
