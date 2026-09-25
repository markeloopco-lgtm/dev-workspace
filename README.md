# Live2Dモデル量産パイプライン

一枚絵（外部発注・外部生成）から Live2D Cubism 用モデルを半自動で量産するための
ツール群とワークフロードキュメント。

```
一枚絵 (外部)                         ← docs/01 の仕様で用意する
  │
  ▼
See-through で自動レイヤー分解        ← docs/02 (SIGGRAPH 2026のOSS・GPU必要)
  │  最大23レイヤー・隠れ部分も補完されたPSD
  ▼
scripts/normalize_psd.py で正規化      ← このリポジトリ (GPU不要)
  │  レイヤー名・フォルダ構成・重ね順を全モデルで統一
  ▼
Cubism Editor でテンプレート適用       ← docs/03
  │  1体目のリグを流用、微調整のみ (1〜2時間/体)
  ▼
.moc3 + model3.json
  │
  ▼
AITuberKitで自動運用配信               ← docs/04
   Geminiチャット応答 + Style-Bert-VITS2発話
   + Live2Dリップシンク + OBS→YouTube
```

## 解説動画の作風分析・制作（videolab）

参考動画（VAIENCE等の科学解説CG動画）を**フレーム単位で計測**して作風を数値化し、
同じテンポ・品質の**オリジナル動画**を台本YAMLから自動で組み立てて、同じ物差しで採点する。

```
参考動画 ──watch(URLのままGemini)／fetch→analyze(全フレーム計測)──▶ report.html・profile.json
   3〜5本を aggregate ──▶ configs/style_profile.yaml（目標スタイル・数値のみ）
台本 episodes/*.yaml ──produce──▶ VOICEVOX合成 → カット割り → 宇宙3DCG/画像 → テロップ → ミックス
   ──▶ renders/*.mp4 ──--check──▶ gap_report.md（一致度スコアと直し方）
```

```powershell
py -3.12 -m venv .venv-video
.venv-video\Scripts\python.exe -m pip install -r requirements-video.txt
.venv-video\Scripts\python.exe scripts\vlab.py doctor
.venv-video\Scripts\python.exe scripts\vlab.py produce episodes\sample_moon_half.yaml --tts dummy --draft
```

手順は [docs/06（分析）](docs/06_video_analysis.md) と [docs/07（制作）](docs/07_video_production.md)。
検証: `python tests/run_video_selftest.py`（正解つき合成動画で解析精度と制作ラウンドトリップを確認）。

## セットアップ（Live2D側）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 動作確認 (GPU不要・合成PSDでラウンドトリップ検証)
.venv/bin/python tests/run_selftest.py
```

See-through本体は別リポジトリ。セットアップは [docs/02](docs/02_see_through_setup.md) 参照。

## 使い方

```bash
# 一括処理: input/ のPNG → 分解 → 正規化 → output/
export SEE_THROUGH_DIR=/path/to/see-through
.venv/bin/python scripts/batch_decompose.py --input input/ --output output/ --vram 12gb

# 分解済みPSDのマッピング確認 (最初に必ずやる)
.venv/bin/python scripts/normalize_psd.py inspect some.psd

# 1枚だけ正規化
.venv/bin/python scripts/normalize_psd.py normalize some.psd -o some_normalized.psd
```

`inspect` で未分類レイヤーが出たら `configs/layer_mapping.yaml` にパターンを追記する。

## リポジトリ構成

| パス | 内容 |
|---|---|
| `docs/01_illustration_spec.md` | 一枚絵の仕様・発注テンプレ・権利の注意 |
| `docs/02_see_through_setup.md` | See-throughの導入とVRAM別設定 |
| `docs/03_cubism_template_workflow.md` | Cubismテンプレート量産手順・チェックリスト |
| `docs/04_aituber_runtime.md` | AITuber運用構成 (AITuberKit + Gemini + SBV2 + OBS) |
| `docs/05_local_claude_code.md` | ローカルPCへの移行手順 (Claude Codeで続きを進める) |
| `CLAUDE.md` | ローカルClaude Code用の引き継ぎ書 (現状・残タスク・技術前提) |
| `scripts/normalize_psd.py` | PSDレイヤー正規化 (inspect / normalize / PNG書き出し) |
| `scripts/batch_decompose.py` | 分解→正規化の一括ドライバ |
| `scripts/setup_aituber.sh` | AITuberKit導入・モデル組み込みヘルパー |
| `configs/layer_mapping.yaml` | レイヤー名マッピング定義 (育てる設定ファイル) |
| `configs/aituberkit.env.example` | AITuberKit環境変数テンプレ (本構成向け・検証済み) |
| `notebooks/see_through_free_gpu.ipynb` | See-throughをKaggle/Colab無料GPU枠で回すノートブック |
| `tests/run_selftest.py` | ラウンドトリップ検証 (GPU不要) |
| `docs/06_video_analysis.md` | 参考動画のフレーム単位分析 → 目標スタイル |
| `docs/07_video_production.md` | 台本から動画を制作・採点 |
| `scripts/vlab.py` | videolabのコマンド一式 |
| `videolab/` | 分析・制作の本体 |
| `configs/style_profile.yaml` | 目標スタイル（数値のみ） |
| `episodes/` | 台本YAML（書式: `episodes/README.md`） |
| `prompts/` | Claude Code向けの台本作成・構成分析の指示書 |
| `tests/run_video_selftest.py` | 解析精度・制作ラウンドトリップ検証 |

## 実装メモ

- マッピング定義はSee-through V3の実タグ体系(ソース調査で確認)に較正済み。
  実タグ28レイヤー相当のフィクスチャでラウンドトリップ検証している
- PSD読み込みは psd-tools、書き出しは pytoshop。往復での重ね順反転
  (pytoshopはリスト先頭=最前面、psd-toolsは最背面から反復) はテストで検証済み
- pytoshopのRLE圧縮はC拡張が必要なため zip 圧縮で書き出す
- pytoshopがunicodeレイヤー名に付ける終端NULは書き出し後に除去している
  (Cubismのテンプレート機能はレイヤー名一致で対応付けるため名前の完全一致が重要)
