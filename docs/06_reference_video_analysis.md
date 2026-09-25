# 06. 参考動画のフレーム分析（同じクオリティで作るための数値化）

お手本にしたい動画を**全フレーム走査して数値と画像にまとめ**、
「何秒ごとに素材を切り替えているか」「つなぎ方」「画面の配置」「色」「音量」などの
**目標値**を作る手順。自分の動画も同じ物差しで測り、差を詰めていく。GPU不要・OpenCV不要で軽い。

> 台本から動画を作る（docs/07・08）ための目標スタイル `configs/style_profile.yaml` は、videolab の
> `vlab analyze` → `aggregate`（[docs/06_video_analysis.md](06_video_analysis.md)）で作る。
> 本書の `scripts/analyze_video.py` は、読みやすい簡易レポート・素材ごとの一覧（shots.csv）・
> 1コマずつの書き出し・自分の動画との比較に使う。

```
参考動画 ──fetch──▶ refs/<動画ID>/source.mp4
                      │
                      ├─analyze──▶ report.md（数値と図）・shots.csv（素材ごとの一覧）・代表フレーム
                      │              └─ 気になる瞬間は frames で1コマずつ確認
                      ▼
            目標値（制作仕様）を決める ──▶ 自分の動画を作る ──analyze──▶ compare で差を確認 → 調整
```

## 注意（権利・規約）

- 真似るのは**作り方**（テンポ・つなぎ方・画面設計・色・音量）。映像・音声・台本・キャラ・ロゴなどの**中身は使わない**
- 取得した動画・書き出したフレームは**分析用の手元保管のみ**。再配布・転載しない
  （`refs/` は .gitignore 済みでリポジトリに入らず、videolab の制作では素材として使えない仕組み）
- YouTubeの利用規約は、YouTubeが用意した機能以外でのダウンロードを原則認めていない。
  他人の動画の取得は分析目的の範囲で自己責任で。自分の動画なら YouTube Studio から正規にダウンロードできる

## 準備（Windows・初回のみ）

videolab と同じ環境を使う。[docs/06_video_analysis.md](06_video_analysis.md) の Step 1
（ffmpeg の導入と `.venv-video` の作成・`requirements-video.txt` の導入）を済ませてから、リポジトリのフォルダで:

```powershell
.venv-video\Scripts\python.exe tests\run_analyze_video_selftest.py   # [OK] が出れば分析ツールは正常
```

## Step 1: 参考動画を取得

```powershell
.venv-video\Scripts\python.exe scripts\analyze_video.py fetch "https://youtu.be/XXXXXXXXXXX"
```

- 保存先: `refs\<動画ID>\source.mp4`（＋動画情報 `source.info.json`・サムネイル `source.jpg`）
- 長い配信アーカイブは一部だけ取得: `--section 00:10:00-00:20:00`
- 失敗したら yt-dlp を更新（YouTube側の変更に合わせて頻繁に更新される）:
  `.venv-video\Scripts\python.exe -m pip install -U "yt-dlp[default,deno]"`
- クラウド版のClaude Codeは、環境のネットワーク設定によってはYouTubeに接続できない。
  その場合はこのPC（ローカル）で実行する

## Step 2: 分析

```powershell
.venv-video\Scripts\python.exe scripts\analyze_video.py analyze refs\XXXXXXXXXXX\source.mp4
```

- 出力先: `refs\XXXXXXXXXXX\source_report\`
- 時間の目安: 1080p・60fpsの動画1分あたり約20秒（PCの性能で変わる）
- 長い動画は区間を絞る `--start 10:00 --duration 5:00`、または間引く `--sample-fps 10`

| 出力 | 内容 |
|---|---|
| `report.md` | 分析レポート（数値・図・確認すべき区間・目標値） |
| `report.json` | 同じ内容の機械向けデータ（compare で使う） |
| `shots.csv` | ショット（素材1本ぶん）ごとの開始・長さ・入り方・明るさ・彩度・動き。Excelで開ける |
| `timeline.png` | 映像の変化量と音量の時間変化。縦線は切り替え（橙=カット、緑=クロスフェード・暗転） |
| `layout_motion.png` | よく動く場所（青）と「動く領域」の枠 |
| `layout_static.png` | 動画全体でほぼ変わらない部分（背景・枠・ロゴ） |
| `palette.png` | 主要色 |
| `sheet_shots_*.jpg` / `sheet_timeline_*.jpg` | ショットごと / 等間隔20枚の代表フレーム一覧 |

## Step 3: 気になる瞬間をフレーム単位で見る

report.md の7章に、切り替えの瞬間や大きな変化の区間ごとのコマンドが出るので、そのまま実行する:

```powershell
.venv-video\Scripts\python.exe scripts\analyze_video.py frames refs\XXXXXXXXXXX\source.mp4 --start 83.2 --duration 1.5
```

- `refs\XXXXXXXXXXX\frames\t83.20s\` に全フレームのPNGと一覧画像 `filmstrip_01.jpg`
- 一覧の「Δ」は前のコマとの差、「=」は前と同じ絵。
  クロスフェードが何コマか、テロップが何コマで出るか、ズームなどの加工があるかを数えられる

## Step 4: 目標値（制作仕様）にまとめる

Claude Code に頼む例:「refs/XXXXXXXXXXX/source_report を読んで、同じクオリティで作るための制作仕様を作って」

- 数値は report.md の10章「目標値」が土台
- 数値で測れない所（字幕の書体・色・縁取り、素材の種類、加工、BGMの雰囲気）は、
  代表フレーム一覧と frames の書き出しを Claude が見て言葉にする（report.md 9章のチェックリスト）

## Step 5: 自分の動画と比べる

自分の動画（書き出した動画やOBS録画）も analyze してから:

```powershell
.venv-video\Scripts\python.exe scripts\analyze_video.py compare refs\XXXXXXXXXXX\source_report 自分の動画_report
```

差の表と、調整の目安（例: 「音量が参考より6 LU小さい → OBSの音声フィルタ『ゲイン』で約+6 dB」）が出る。

## 指標の読み方

| 指標 | 意味 | 使いどころ |
|---|---|---|
| 切り替え/分 | 素材・場面が切り替わる回数（カット・クロスフェード・暗転）。スライド・ワイプ・ズームのつなぎは含まず7章に出る | 〜0.5 配信型 ／ 〜4 ゆったり ／ 〜12 標準 ／ 12〜 速い |
| ショットの長さ | 素材1本を見せている秒数（中央値と、短い方・長い方10%） | 素材を切り出す長さ |
| つなぎ方の内訳 | カット ／ クロスフェード ／ 暗転（黒をはさむ） | トランジションの種類と比率 |
| 実効fps | 動く場面で実際に絵が更新される頻度（60fpsの動画でも中身は30fpsのことがある） | 動きの滑らかさ |
| ラウドネス (LUFS) | 聴感上の音量。YouTubeは約-14 LUFSを目安に、大きすぎる動画の再生音量を下げる | 仕上げの音量 |
| トゥルーピーク | 瞬間的な最大音量。-1 dBFS 以下なら音割れしにくい | 音割れ防止 |
| 無音の割合・間・BGMの推定 | 声の間の取り方、BGMが流れ続けているか | ナレーションのテンポ・BGMの有無 |
| 固定部分・動く領域 | 背景・枠などの動かない部分と、キャラ・字幕などの動く部分の位置と大きさ | 画面レイアウト |
| 明るさ・彩度・カラフルさ・主要色 | 画面全体の色の傾向 | 素材選び・色調整 |
| 素材ごとの色のばらつき | ショット間の明るさ・彩度の差。小さいほど色味がそろっている | 色の統一感（色調整の有無） |

## 素材を組み合わせた動画の場合（フリー動画素材など）

見る所:

- **素材1本の長さとつなぎ方**（2章・`shots.csv`）: 同じテンポで素材を切り替えるための設計図になる
- **素材の動き**（`shots.csv` の「動き」）: 静止に近い素材が中心か、ドローン撮影のように動く素材が中心か
- **色の統一感**（4章の「素材ごとの色のばらつき」）: 小さければ色調整されている可能性が高い
- **加工**（7章の「大きな変化」→ frames）: ズーム・パンなどの加工や、スライド系のつなぎ
- **字幕・ナレーション・BGM**（5章・9章）: 数値化しにくいので、代表フレームと音で確認

素材の入手先の例: [Pexels](https://www.pexels.com/) / [Pixabay](https://pixabay.com/)（どちらも無料の動画素材あり・検索APIあり）。
**ライセンスは各サイトで必ず確認**（素材単体での再配布の禁止、人物・ロゴが写った素材の扱いなど）。

収益化の注意: 素材を並べただけの動画は、YouTubeパートナープログラムの「再利用されたコンテンツ」などの
ポリシーにより収益化の対象外になりうる。台本・ナレーション・編集で独自の価値を加える。

## このプロジェクトでの合わせ先（AITuber配信の場合）

| 指標 | 合わせる場所 |
|---|---|
| 解像度・fps | OBS 設定 → 映像（基本・出力解像度、FPS） |
| 音量 | OBS 音声ミキサーのフィルタ（ゲイン・リミッター）。録画して analyze → compare で確認 |
| キャラの位置・大きさ | OBS で AITuberKit のキャプチャを配置・拡大縮小（docs/04） |
| キャラの動きの大きさ | Cubism のアイドルモーション・物理演算（docs/03） |
| 背景・配色 | 背景画像、字幕の色 |

## 分析の仕組み（参考）

- 映像を長辺320pxに縮小し、全フレームを1枚ずつ比較する
- **カット**: 直前フレームとの差が、前後のフレームより突出して大きいコマ
  （PySceneDetect の AdaptiveDetector と同じ考え方）。1〜2コマだけ光って戻るフラッシュは除外
- **クロスフェード**: 約0.5秒前・中間・現在の3コマで、中間のコマが「前後の画を一定の割合で混ぜた画」として
  再現できれば混合＝クロスフェード。パン・ズーム・スライドは画素がずれるので再現できず、区別できる
- **暗転**: 黒い画面をはさむ切り替え（黒へのカット・フェードを含む）を1回と数える
- **実効fps**: 動いている場面で「前と同じ絵」のコマが占める割合から計算
- **ラウドネス**: ffmpeg の ebur128（EBU R128 / ITU-R BS.1770 準拠）
- 検証: `tests/run_analyze_video_selftest.py`（答えの分かっている合成動画で、カット・クロスフェード・パン/ズームの区別・
  暗転・フラッシュ・実効fps・無音区間などを確認）
