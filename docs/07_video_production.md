# 07. 目標スタイルどおりの解説動画を作る（videolab produce）

docs/06 で作った目標スタイル（`configs/style_profile.yaml`）に合わせて、
**台本YAMLから完成mp4までを自動で組み立て、出来上がりを同じ物差しで採点する**手順。

```
台本 episodes/xxx.yaml（Claude Codeが下書き → あなたが確認）
  │
  ├─ ① ナレーション合成   VOICEVOX（無料・CPU可）。話速は目標スタイルに自動で合わせる
  ├─ ② カット割り         ナレーションの尺 × 目標のショット長・冒頭テンポ・遷移の比率
  ├─ ③ 映像              宇宙シーン(Blender 3DCG / 2D版) ・ 画像のズーム/パン ・ 動画素材
  ├─ ④ テロップ          目標の位置・縁取り。長い台詞は句読点・文節で分割
  ├─ ⑤ 音               BGMは目標の音量差に自動調整＋台詞中は自動で下げる、-14 LUFSに正規化
  ▼
renders/xxx.mp4 ＋ 字幕(.ja.srt) ＋ クレジット(.credits.txt)
  │
  └─ --check: 完成品を docs/06 と同じ方法で解析 → gap_report.md（一致度スコアと直し方）
```

VAIENCEの求人票にある編集工程（ナレーションに合わせたカット編集 → テロップ → グラフィック →
色と音の調整）を、無料ツールで自動化した形。

## Step 1: まず音声無しで動作確認（5分）

docs/06 の Step 1（準備）が済んでいれば、そのまま動く。

```powershell
.venv-video\Scripts\python.exe scripts\vlab.py produce episodes\sample_moon_half.yaml --tts dummy --draft
```

`renders\sample_moon_half.mp4` ができれば成功（仮音声・半分の解像度）。
宇宙シーンはBlenderが無ければ2D版で描かれる。

## Step 2: VOICEVOXで本物のナレーション（15分）

1. https://voicevox.hiroshiba.jp/ からWindows版をダウンロードしてインストール
   （GPU版でもCPU版でもよい。VRAM 4GBならCPU版で十分）
2. VOICEVOXを起動しておく（起動中は裏で音声エンジンが動く）
3. 話者IDを確認:

   ```powershell
   .venv-video\Scripts\python.exe scripts\vlab.py voices
   ```

4. 本番の書き出し＋採点:

   ```powershell
   .venv-video\Scripts\python.exe scripts\vlab.py produce episodes\sample_moon_half.yaml --check
   ```

### 声の選び方と規約（2026-09時点。使う前に各キャラの利用規約を必ず確認）

| 役 | 候補 | 条件の要点 |
|---|---|---|
| ナレーター（落ち着いた男性） | 玄野武宏 / 麒ヶ島宗麟 | 商用・非商用OK、「VOICEVOX:キャラ名」のクレジット |
| 専門家役（年配・渋い声） | ちび式じい | 商用・非商用OK、「VOICEVOX:ちび式じい」のクレジット |
| （注意） | 青山龍星 | 事業としての利用は事前確認が必要 |
| （注意） | No.7 | 商用は事前確認が必要 |

- クレジットは `renders\xxx.credits.txt` に自動で書き出されるので、概要欄に貼る
- AivisSpeech（`engine: aivis`）・Style-Bert-VITS2（`engine: sbv2`、docs/04）も使える。
  **モデルごとに規約が違う**（商用可・クレジット要否）。実在の声優の声を真似たモデルは使わない
- 参考チャンネルの声優さんに似せた声を作る・使うことは絶対にしない（パブリシティ権の問題）

## Step 3: Blenderで3DCGの宇宙シーン（任意・30分）

VRAM 4GBでも、軽い宇宙シーン（惑星・太陽・星空）ならEEVEEで描ける。

1. https://www.blender.org/download/ から **Blender 5.2 LTS** をインストール（既定の場所でよい）
2. `doctor` で `Blender` が `[OK]` になることを確認
3. 以降 `produce` は自動でBlenderを使う（`--engine 2d` で2D版に固定、`--engine blender` で必須化）

- 描画結果は `renders\cache\` に保存され、同じ場面は2回目から再利用される
- 最初は `--draft`（半分の解像度）で1本通して、所要時間を確かめる
- 重い場合: `episodes` の宇宙シーンを減らす／`--engine 2d` に戻す

## Step 4: 自分の台本を書く

```powershell
.venv-video\Scripts\python.exe scripts\vlab.py new-episode "もしも〇〇したら" -o episodes\my_first.yaml
```

Claude Codeに下書きを頼む:

```
prompts/episode_writer.md を読んで、テーマ「もしも〇〇したら」で約8分の台本を
episodes/my_first.yaml に書いてください。
```

書式は `episodes/README.md`。**あなたがやること**:

- 台本の数字・主張を `sources:` の出典で確認する（解説動画の信頼はここで決まる）
- 「素材TODO」の画像を用意して `assets\images\` に置く（入手先は `assets/README.md`）
- 好きなBGM・効果音を `assets\bgm\` `assets\se\` に置いて台本に書く（規約とクレジットを確認）

## Step 5: 書き出しと採点、直す

```powershell
.venv-video\Scripts\python.exe scripts\vlab.py produce episodes\my_first.yaml --check
```

`analysis\_mine_my_first\gap_report.md` に一致度スコアと項目ごとの助言が出る。例:

| 判定 | 項目 | 目標 | 実測 | 助言 |
|---|---|---|---|---|
| NG | ショット長(中央値, 秒) | 2.8 | 4.6 | カットが長い。ナレーション1文ごとに映像を切り替えるか、素材を増やす |
| △ | 暗い画面の割合 | 0.55 | 0.32 | 暗い(宇宙系)カットが少ない |

- テンポ・カメラワーク・音量・話速は自動で目標に寄るので、主に効くのは**素材の数と種類**
- スコアは「測れる作風」の一致度。**台本の面白さ・CGの作り込み・演技は測れない**ので、
  最後は自分の目と耳で確かめる
- `renders\xxx.plan.json` に、どのショットをどう動かしたかの設計図が残る

## Step 6: 投稿前チェックリスト

- [ ] チャンネル名・ロゴ・キャラクター・サムネイルは**オリジナル**（参考チャンネルに似せない）
- [ ] 台本の根拠を確認した。推定の数字は「試算では」等と言っている
- [ ] 専門家役が健康・法律・お金の助言をしていない（AIペルソナの収益化ルール）
- [ ] 概要欄にクレジット（`credits.txt`：音声・BGM・画像）
- [ ] 字幕 `renders\xxx.ja.srt` をYouTube Studioでアップロード（任意）
- [ ] 実在の場所・出来事の写実的な合成映像がある → YouTube Studioで「改変・合成コンテンツ」を「はい」
- [ ] 毎回テーマ固有の考察があり、BGM・導入・映像が使い回しになっていない
      （YouTubeの「汎用的・繰り返しの多いコンテンツ」は収益化対象外）

## 困ったとき

| 症状 | 対処 |
|---|---|
| `音声エンジンに接続できません` | VOICEVOXを起動する。試すだけなら `--tts dummy` |
| `日本語フォントが見つかりません` | `assets\fonts\` に .ttf/.otf を置く（例: Noto Sans JP） |
| `refs/ 内のファイルが指定されています` | 参考動画の素材は使えない仕様。自分の素材を `assets\` に置く |
| 書き出しが遅い | `--draft` で確認 → 本番。NVIDIAのGPUなら `--encoder nvenc` でエンコードが速くなる |
| Blenderでエラー | `--engine 2d` で回避し、エラー文をClaude Codeに見せる |
