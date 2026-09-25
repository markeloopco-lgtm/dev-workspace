# 08. フリー動画素材で作る（Pexels / Pixabay → 台本 → 完成動画）

フリー動画素材を組み合わせた動画を、videolab の制作機能（docs/07）で作る手順。
素材の検索・ダウンロード・クレジットの記録は `vlab stock` が自動でやる。

```
参考動画 ──docs/06──▶ configs/style_profile.yaml（素材1本の長さ・つなぎ方・話速・音量などの目標値）
台本 episodes/xxx.yaml（台詞 ＋ 映像を {type: stock, query: "検索語"} で書く）
   ──vlab stock──▶ assets/video/stock/（素材 ＋ 撮影者・出典の記録）
   ──vlab produce──▶ renders/xxx.mp4（ナレーション・テロップ・BGM・音量調整）＋ credits.txt
   ──--check──▶ 目標値との差（gap_report.md）
```

## 準備（初回のみ）

1. docs/06 Step 1 の環境（`.venv-video`・ffmpeg）と、docs/07 の VOICEVOX
2. 素材サイトのAPIキー（どちらも無料。片方だけでもよい）
   - Pexels: https://www.pexels.com/api/ でアカウントを作り、APIキーを発行
   - Pixabay: https://pixabay.com/api/docs/ （ログインするとページ内にキーが表示される）
3. 作業フォルダで次を実行して `.env` に追記する（既にGeminiのキーがあっても消えない）:

```powershell
Add-Content -Path .env -Value 'PEXELS_API_KEY=ここにキー' -Encoding ascii
Add-Content -Path .env -Value 'PIXABAY_API_KEY=ここにキー' -Encoding ascii
```

`.env` はGitに入らない設定になっている（キーを公開しないため）。

## Step 1: 目標値を決める

参考動画を docs/06 の手順で分析し、`configs/style_profile.yaml` を実測値にする。
素材動画の組み合わせで特に効くのは、素材1本の長さ（`editing.shot_len_median`）、
つなぎ方の割合（`editing.transition_mix`）、クロスフェードの長さ（`editing.dissolve_len_mean`）、
話速（`audio.chars_per_sec`）、音量（`audio.lufs_integrated`）。
`scripts/analyze_video.py` の簡易レポート（docs/06_reference_video_analysis.md）の `shots.csv` も目安になる。

## Step 2: 台本を書く

```powershell
.venv-video\Scripts\python.exe scripts\vlab.py new-episode "タイトル" -o episodes\xxx.yaml
```

映像を検索語で書く（見本: `episodes/sample_stock.yaml`）:

```yaml
scenes:
  - id: hook
    lines:
      - {speaker: ナレーター, text: "朝の海は、一日でいちばん静かな時間です。"}
    visuals:
      - {type: stock, query: "sunrise over calm sea", min_duration: 6}
```

| キー | 内容 |
|---|---|
| `query` | 検索語。英語の方が見つかる数が多い（日本語も可）。「何が・どこで・どう動く」まで具体的に |
| `min_duration` | この秒数以上の素材を優先（省略時5秒） |
| `provider` | `pexels` / `pixabay` に限定したい時だけ |
| `start` `camera` `overlays` | 動画素材（`type: video`）と同じ。素材の動きを生かすなら `camera: static` |

- 同じ検索語を何回も書くと、使った回数ぶん別の素材を取ってきて順番に使う（表記ゆれ・大文字小文字は同じ扱い）
- Claude Code に書かせる時は `prompts/episode_writer.md` を渡す

## Step 3: 素材を取ってくる

```powershell
.venv-video\Scripts\python.exe scripts\vlab.py stock episodes\xxx.yaml
```

- 横長・指定秒数以上・720p以上の素材を優先し、1080p以上のファイルを選んで `assets\video\stock\` に保存する
- 撮影者・出典は `assets\video\stock\index.json` に記録され、制作時に credits.txt へ入る
- 候補だけ見たい時: `scripts\vlab.py stock "ocean waves" --search`
- 取得済みの素材は再利用する（2回目以降はダウンロードしない）
- **気に入らない素材は `assets\video\stock\` のファイルを消して再実行** → 次の候補になり、消した素材は二度と取らない
- 取得前でも `produce --draft` なら「素材TODO」の仮カードで流れを確認できる

## Step 4: 動画にする

```powershell
.venv-video\Scripts\python.exe scripts\vlab.py produce episodes\xxx.yaml --draft    # 下書き(半分の解像度)
.venv-video\Scripts\python.exe scripts\vlab.py produce episodes\xxx.yaml --check    # 本番 ＋ 目標値との比較
```

- 出力: `renders\xxx.mp4`、字幕 `renders\xxx.ja.srt`、概要欄用のクレジット `renders\xxx.credits.txt`
- RTX 3050 なら `--encoder nvenc` で書き出しが速い
- 動画素材にも目標スタイルのズーム・パンが自動でかかる。かけたくない素材は `camera: static`

## ライセンスと収益化の注意

- Pexels・Pixabay の素材は商用利用可・クレジット表記は任意（credits.txt を概要欄に貼るのがおすすめ）
- 素材をほぼそのまま再配布・販売しない。人物・ロゴ・商標が写った素材は、誤解を招く使い方をしない
- 規約は変わることがあるので最新版を確認: https://www.pexels.com/license/ ／ https://pixabay.com/service/license-summary/
- 素材を並べただけの動画は、YouTubeの収益化の対象外になりうる（再利用されたコンテンツ・繰り返しの多いコンテンツ）。
  台本とナレーションで独自の価値を足す
- 参考動画の映像・音声・台詞は使わない（`refs/` と `analysis/` の素材は制作で拒否される）

## 仕組み（参考）

- `videolab/stock.py`: 検索（Pexels / Pixabay の動画検索API）・素材の選び方・ダウンロード・取得記録（index.json）
- `videolab/produce/episode.py`: 台本の読み込み時に `type: stock` を取得済みの `type: video` に差し替え、
  クレジットを `credits` に足す
- 検証: `tests/run_stock_selftest.py`（APIの応答見本とffmpegの合成動画で、ネット・キー無しで確認）。
  実際のAPIでの動作は、キーを入れたPCで `vlab stock ... --search` を実行して確かめる
