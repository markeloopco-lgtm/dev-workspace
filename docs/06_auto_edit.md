# 06. ニュース風自動編集 (ジェットカット + テロップ + BGM)

録画した動画(配信アーカイブ・解説動画など)を**コマンド1回**で
「無音カット + ニュース番組風テロップ + BGM」に仕上げるツール。
全部無料・ローカル処理(アップロード不要)。

```
入力動画 (mp4など)
  │
  ▼ ① ffmpeg silencedetect で無音検出 ──→ ジェットカット計画 (plan.json)
  │
  ▼ ② faster-whisper で文字起こし ──→ 字幕ファイル (telop.srt) ←誤認識はここを手直し
  │
  ▼ ③ ニュース風テロップ生成 (下部の帯 + 白抜き太字 + 番組名バー)
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
# 全自動 (無音カット + テロップ) → 録画_edited.mp4 ができる
python scripts/auto_edit.py run 録画.mp4

# BGM付き + 左上に番組名バー
python scripts/auto_edit.py run 録画.mp4 --bgm bgm.mp3 --title "AITuberニュース"

# テロップなしでジェットカットだけ
python scripts/auto_edit.py run 録画.mp4 --no-telop
```

初回だけ文字起こしモデル(約500MB)のダウンロードで数分かかる。
2回目からはすぐ始まる。

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

見た目(フォント・色・帯・番組名バー)も同じファイルの `style:` / `title:` で変更できる。
数値を変えたら `render` だけやり直せば反映される(文字起こし不要)。

## BGMについて

- `--bgm ファイル` または `configs/auto_edit.yaml` の `bgm.file` で指定(指定しなければ付かない)
- 動画の長さに合わせて自動ループ+フェードイン/アウト
- `ducking: true`(既定)で**声が鳴っている間はBGMが自動で小さくなる**(ニュース番組と同じ処理)
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
| テロップが豆腐(□)になる | `style.font` をPCにあるフォント名に (例: `Yu Gothic UI`)。フォント一覧は 設定→個人用設定→フォント |
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
| BGM音源 | 各配布サイトの規約による | 要確認 |

編集対象の動画・音声そのものの権利(ゲーム画面・楽曲など)は別途各規約に従うこと。
