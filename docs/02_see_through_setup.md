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
（1日1〜2枚）で可能。量産は不可なのでローカルGPUかクラウドGPU（RunPod等）を用意する。

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
