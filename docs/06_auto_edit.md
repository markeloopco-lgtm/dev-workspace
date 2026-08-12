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

## フォント（テロップの印象を一番左右する）

`configs/auto_edit.yaml` は `font: auto` になっていて、**PCにあるフォントを
上から順に自動で探して使う**。今どれが使われるかはこれで分かる:

```powershell
python scripts/auto_edit.py fonts
```

| 優先順 | フォント | 入手 |
|---|---|---|
| 1 | **Noto Sans JP Black** | 無料DL（下記）。報道番組の定番書体の代替として実務者が第一に挙げるもの |
| 2 | BIZ UDPGothic | Windows 10/11 に最初から入っている（DL不要） |
| 3 | メイリオ / 游ゴシック | Windows 標準 |

DL不要でもそれなりに見えるが、**Noto Sans JP Black を入れると一段ニュースらしくなる**。
入れ方は `assets/fonts/README.md` の3ステップ（ZIPの中の `NotoSansJP-Black.ttf` を
`assets/fonts/` にコピーするだけ。インストール作業は不要）。

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
| テロップが小さい/大きい | `style.font_size_ratio` (画面高さ比。放送の目安は0.06〜0.08) |
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
| `font_size_ratio` | 0.062 | テロップの文字サイズは画面縦の6〜8%が目安 |
| `band.padding_em` | 0.52 | 座布団の余白は文字サイズの50〜70% |
| `style.line_height_em` | 1.45 | 日本語の行間は文字サイズの150%前後が下限 |
| `style.outline_em` | 0.05 | 縁取りは文字サイズの5〜10% |
| `style.tracking_em` | -0.02 | 文字を軽く詰める（"プロっぽさ"の決め手と言われる） |
| `band.opacity` | 0.85 | 座布団の不透明度は70〜85%が読みやすい |
| `max_chars_per_line` / `max_lines` | 15 / 2 | ニューステロップは1行12〜15文字・最大2行 |
| `reading_speed` / `max_duration` | 4.0 / 6.5 | 1秒あたり全角約4文字・1枚は最大6.5秒 |

出典は下記「参考にした放送・字幕の基準」。

### あえてやっていないこと

実務者が「安っぽく見える原因」として挙げているものは既定で避けている:

- **二重縁取り・太すぎる縁**（`double_outline: false`）— 帯があるので細い縁で足りる
- **派手なグラデーション** — 上端がわずかに透ける程度（`band.gradient: 0.18`）に留めている
- **テロップ位置がバラバラ** — 位置は画面下に固定
- **画面端いっぱいの文字** — `safe_margin_ratio: 0.05` で左右に余白を確保

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

## 参考にした放送・字幕の基準

既定値の根拠。数値の出どころを残しておくので、好みで動かすときの目安にする。

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
