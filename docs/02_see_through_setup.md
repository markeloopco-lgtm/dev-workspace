# 02. See-through セットアップと実行

[See-through](https://github.com/shitagaki-lab/see-through)（SIGGRAPH 2026）は
一枚絵を最大23の意味的レイヤー（前髪・後髪・顔・目・服…）に自動分解し、
隠れた部分をインペインティングで補完した深度順PSDを出力するOSS。

> 本家は無償の研究プロジェクト。有料サービスを名乗るサイトは非公式なので注意。

## 導入方法は2通り

### A. スタンドアロン（バッチ量産向け・本パイプラインの前提）

```bash
git clone https://github.com/shitagaki-lab/see-through
cd see-through
conda create -n see_through python=3.12 -y
conda activate see_through
pip install torch==2.8.0+cu128 torchvision==0.23.0+cu128 torchaudio==2.8.0+cu128 \
  --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt
ln -sf common/assets assets
```

モデル重み（HuggingFaceから初回実行時に自動DL）:
- LayerDiff 3D: `layerdifforg/seethroughv0.0.2_layerdiff3d`
- Marigold Depth: `24yearsold/seethroughv0.0.1_marigold`
- SAM Body Parsing: `24yearsold/l2d_sam_iter2`

単発実行:

```bash
python inference/scripts/inference_psd.py --srcp path/to/image.png --save_to_psd
# フォルダ一括: --srcp path/to/folder/
# 出力: workspace/layerdiff_output/*.psd
```

### B. ComfyUIプラグイン（1枚ずつ視覚的に確認したいとき）

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/jtydhr88/ComfyUI-See-through.git
cd ComfyUI-See-through && pip install -r requirements.txt
```

同梱の `seethrough-basic.json` ワークフロー（1280px・30steps・左右分割ON）を読み込み、
`SeeThrough Decompose` → `SeeThrough Save PSD` でブラウザからPSDをダウンロードできる。

## VRAM別の設定

| GPU VRAM | 設定 |
|---|---|
| 16GB以上 | デフォルト（bf16・1280px） |
| 12GB | `--group_offload` を付ける（約10GBまで低減） |
| 8GB | `inference_psd_quantized.py`（NF4量子化）を使う |

手元にGPUがない場合、お試しは [HuggingFaceデモ](https://huggingface.co/spaces/24yearsold/see-through-demo)
（1日1〜2枚）で可能。量産は不可なのでローカルGPUかクラウドGPU（下記）を用意する。

## ローカルGPUが8GB未満の場合（クラウドGPU運用）

VRAM 8GB未満（例: RTX 3050 Laptop 4GB）ではSee-throughはローカル実行できない。
ただしSee-throughは量産時だけ動かすバッチ処理なので、**分解だけクラウドで回して
PSDを回収し、正規化以降はローカルで行う**分業が現実的。正規化(`normalize_psd.py`)は
GPU不要でどのPCでも動く。

### 選択肢

| サービス | 費用 | 備考 |
|---|---|---|
| RunPod / Vast.ai | RTX 4090 24GBで$0.5前後/時 | **確実に動く。量産の本命**。使った時間だけ課金 |
| Kaggle Notebooks | 無料（週30時間のGPU枠） | T4/P100 16GB。GMOグループの実証例あり(※)。世代が古くbf16非対応のため設定調整が要る可能性 |
| Google Colab | 無料枠T4 / Pro課金でL4等 | Kaggleと同様の注意 |

※ [GMOの実証記事](https://recruit.group.gmo/engineer/jisedai/blog/see-through-x-kaggle-x-claude-code/)
「See-Through × Kaggle × Claude Codeで1枚絵からLive2Dモデルを（ほぼ）自動生成する」

### RunPodでの手順（例）

1. PyTorchテンプレートでPod作成（GPU: RTX 4090、ディスク50GB以上。モデル重みのDLがあるため）
2. Pod内でセットアップ（本ページ上部のスタンドアロン手順と同じ）:
   ```bash
   git clone https://github.com/shitagaki-lab/see-through && cd see-through
   pip install torch==2.8.0+cu128 torchvision==0.23.0+cu128 torchaudio==2.8.0+cu128 \
     --index-url https://download.pytorch.org/whl/cu128
   pip install -r requirements.txt && ln -sf common/assets assets
   ```
3. 一枚絵をアップロード（RunPodのWeb端末へドラッグ&ドロップ、または `runpodctl send`）
4. フォルダごと一括分解:
   ```bash
   python inference/scripts/inference_psd.py --srcp /workspace/inputs/ --save_to_psd
   ```
5. `workspace/layerdiff_output/*.psd` をダウンロードしてPodを停止（課金停止）
6. ローカルで正規化だけ実行:
   ```bash
   python scripts/batch_decompose.py --normalize-only path/to/downloaded_psds/ --output output/
   ```

初回はモデル重みのダウンロードで時間がかかるため、量産時は一枚ずつでなく
**まとめて処理してからPodを落とす**のがコスト効率が良い。

## 本パイプラインからの一括実行

```bash
export SEE_THROUGH_DIR=/path/to/see-through
python scripts/batch_decompose.py --input input/ --output output/ --vram 12gb
```

`input/` のPNG全件 → See-through分解 → レイヤー正規化 → `output/*_normalized.psd`
まで一気に行う。分解済みPSDがすでにある場合は
`--normalize-only <PSDフォルダ>` で正規化だけ実行できる。

## 出力レイヤーのタグ体系(較正済み)

See-through V3の実タグ体系はソース調査(ComfyUI-See-through nodes.py)で確認済みで、
`configs/layer_mapping.yaml` は以下を前提に較正してある:

- **body系**: front hair / back hair / head / neck / neckwear / topwear / handwear
  / bottomwear / legwear / footwear / tail / wings / objects
- **head系**: headwear / face / irides / eyebrow / eyewhite / eyelash / eyewear
  / ears / earwear / nose / mouth
- **左右分割**(接尾辞直付き): eyer / eyel / earr / earl / browr / browl / handwear-r / handwear-l
- **前後分割**: hairf(前髪) / hairb(後髪)

それでも本体のバージョンアップで命名が変わる可能性はあるため、**1枚目の出力PSDに対して**

```bash
python scripts/normalize_psd.py inspect workspace/layerdiff_output/xxx.psd
```

を実行して未分類ゼロを確認してから量産に入ること。未分類が出たら
`configs/layer_mapping.yaml` にパターンを追記すれば以降の全モデルに効く。
