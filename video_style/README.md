# 動画の編集スタイル解析ツール

お手本の動画を**全フレーム**解析して、編集スタイル（カットのテンポ、切り替え方、ズームなどの演出、
テロップの位置・大きさ・色、色味、音の作り）を数値にし、再現用のプロファイルを作るツール。

再現するのは「編集の型」だけ。お手本動画の映像・台本・音楽・ロゴなどの中身は使わず、
自分の素材にこの型を当てはめて使う。

## 流れ

```
お手本動画 (URL or ファイル)
  │  analyze_video.py … 全フレームを機械で計測（GPU不要）
  ▼
analysis/<動画ID>/
  ├ summary.json / report.md     … 数値のまとめ
  ├ style_profile.yaml           … 再現用プロファイル（数値部分）
  ├ sheets/ transitions/         … 目視レビュー用の画像
  └ frames.csv shots.csv telops.csv events.json timeline.png
  │
  │  目視レビュー（Claudeが画像を見て、フォント・テロップの装飾・効果音の使い方などを記入）
  ▼
video_style/styles/<名前>.yaml + 編集スタイルガイド   … 再現の手順書
```

## 何を測っているか

| 項目 | 方法 | 出力先 |
|---|---|---|
| カット（切り替え）位置 | 前後フレームの色の変化量（前後数フレームと比べて突出したらカット） | `shots.csv`, `events.json` |
| 白フラッシュ・暗転・フェード | 明るさの急変・黒画面の連続と、その前後の明るさの傾き | `events.json` の `specials` |
| クロスフェード等 | 8フレーム離れた画面の差と「2画面の混ぜ合わせで説明できるか」 | `events.json` の `gradual` |
| ジャンプカット・パンチイン | カット前後で同じ背景の特徴点が対応するか、何倍に拡大されたか | `shots.csv` の `jump` |
| ズーム・パン・揺れ | 特徴点の動きから画面全体の拡大率・移動量を毎フレーム推定 | `shots.csv` の `motion`, `events.json` |
| テロップ | 文字領域検出（PP-OCRv3、既定3フレームおき）→ 同じ文字を時間方向に追跡 | `telops.csv` |
| テロップの色 | 文字領域を3色に分け、背景・縁取り・文字色を推定 | `telops.csv` の `fill/outline` |
| 顔・画角 | YuNet顔検出（実写の顔のみ。アニメ顔は対象外）、既定5フレームおき | `shots.csv` の `shot_size` |
| 常時表示の枠・ロゴ | 全ショットの代表フレームを重ねて、場面が変わっても動かない領域 | `static_overlay.png` |
| 色味 | 明るさ・コントラスト・彩度・色温度（Lab）・主要色 | `summary.json` の `color` |
| 音 | ラウドネス(LUFS)、発話区間と「間」の長さ、間のBGM音量、音の立ち上がりとカットの一致 | `summary.json` の `audio` |

数値は目安。特にテロップ色・文字サイズ・ディゾルブ判定は、`sheets/` と `transitions/` の画像で
目視確認してから確定する。

## セットアップ

### Windows（PowerShell）

```powershell
winget install Gyan.FFmpeg          # ffmpeg（入れたらPowerShellを開き直す）
cd <このリポジトリ>
python -m venv .venv
.venv\Scripts\pip install -r video_style\requirements.txt
.venv\Scripts\python video_style\selftest.py     # 「結果: すべてOK」なら準備完了
```

### Linux / Mac

```bash
pip install -r video_style/requirements.txt
python video_style/selftest.py
```

## 使い方

```bash
# YouTubeのURLを直接（動画は analysis/<動画ID>/source.mp4 に保存される）
python video_style/analyze_video.py "https://youtu.be/XXXXXXXXXXX"

# 手元の動画ファイル
python video_style/analyze_video.py お手本.mp4 -o analysis/otehon

# まず冒頭60秒だけ試す
python video_style/analyze_video.py お手本.mp4 --max-seconds 60

# テロップも全フレームで検出する（遅くなる。テロップの出方を1コマ単位で見たいとき）
python video_style/analyze_video.py お手本.mp4 --text-step 1
```

処理時間の目安（CPUのみ・既定設定）: 動画1分あたり約1分。

## 主なオプション

| オプション | 既定 | 意味 |
|---|---|---|
| `--text-step` | 3 | テロップ検出の間隔（1=全フレーム、0=無効） |
| `--face-step` | 5 | 顔検出の間隔（0=無効）。初回にYuNetモデル(約230KB)をGitHubから取得 |
| `--analysis-width` | 640 | 解析に使う横幅px |
| `--keyframe-width` | 960 | 保存する代表フレームの横幅px |
| `--cut-min / --cut-ratio / --cut-hard` | 15 / 3.0 / 55 | カット判定の感度（誤検出が多いときは上げる） |
| `--grad-min` | 30 | ディゾルブ等の判定しきい値 |
| `--max-seconds` | なし | 先頭から指定秒数だけ解析 |

## 注意

- `analysis/` は `.gitignore` 済み（元動画とそのフレーム画像をリポジトリに入れないため）。
  確定したスタイルは `video_style/styles/` に書き出してコミットする。
- 動画のダウンロードは、各サービスの規約と著作権の範囲内（個人的な分析目的）で行う。
- 検証: `python video_style/selftest.py`（正解が分かっている合成動画で、カット・フラッシュ・暗転・
  クロスフェード・ズーム・パン・パンチイン・テロップ位置と色・無音・クリック音の検出を確認する）
