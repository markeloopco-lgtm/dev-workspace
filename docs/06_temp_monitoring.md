# 06. GPU/CPU温度の自動監視

配信中のPCの温度を自動で見張り、危ないときに音と赤字で知らせる仕組み。
放置運用でノートPCを長時間まわすので、熱で落ちる・性能が下がるのを事前に防ぐのが目的。

**追加インストールは基本ゼロ**（GPU温度はNVIDIAドライバに最初から入っている
`nvidia-smi` を使う）。CPU温度だけは精度を上げるために無料OSSを自動導入する。

---

## いちばん簡単な使い方

エクスプローラーで `scripts` フォルダを開いて、**ダブルクリックするだけ**。

| ファイル | 何をするか |
|---|---|
| `scripts\check_temps_once.bat` | 今の温度を1回だけ表示する（動作確認用） |
| `scripts\start_temp_monitor.bat` | 10秒おきにずっと監視する（配信中はこれ） |

初回は「このアプリがデバイスに変更を加えることを許可しますか？」と出るので **「はい」**。
CPUの温度センサーを読むのに管理者権限が必要なため。

`start_temp_monitor.bat` はこんな表示が流れ続ける:

```
[監視] しきい値: CPU 警告85℃ / 危険95℃   GPU 警告80℃ / 危険87℃
[監視] 記録先: C:\Users\...\dev-workspace\logs\temps_2026-08-11.csv
[監視] 終了するには Ctrl+C を押してください。

[21:03:11]  CPU  62.0℃  GPU  58.0℃  負荷  30%  VRAM 1.8/4.0GB   正常
[21:03:21]  CPU  71.5℃  GPU  66.0℃  負荷  55%  VRAM 2.1/4.0GB   正常
[21:03:31]  CPU  86.0℃  GPU  82.0℃  負荷  95%  VRAM 2.9/4.0GB   警告
```

止めるときは **Ctrl+C**。止めると平均・最高温度のまとめが出る。

---

## 温度の見方

色と言葉で3段階になっている。

| 表示 | 色 | 意味 | やること |
|---|---|---|---|
| 正常 | 緑 | 問題なし | そのまま |
| 警告 | 黄 | 高いが動作はする。この辺から性能が落ちはじめる | 様子見。続くなら冷却を見直す |
| 危険 | 赤 + ビープ音3回 | 熱で性能が大きく落ちる／落ちる寸前 | 配信を止める、負荷を下げる、吸気口を確認 |

### しきい値の目安（RTX 3050 Laptop の場合）

初期値は**安全側に寄せた保守的な値**にしてある。ノートPCは構造上どうしても
デスクトップより温度が高く出るので、実際に配信してみて「警告」が出っぱなしなら
しきい値の方を上げてよい。

| | 警告 | 危険 | 補足 |
|---|---|---|---|
| GPU | 80℃ | 87℃ | ノートGPUは実力上限が90℃台。80℃台前半なら通常運転の範囲 |
| CPU | 85℃ | 95℃ | ノートCPUは瞬間的に90℃台に触れるのが普通。**続く**かどうかが重要 |

しきい値を変えたいとき（PowerShellで実行）:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\monitor_temps.ps1 -GpuWarn 84 -GpuCrit 90
```

> 一瞬だけ危険域に触れるのは正常。**同じ温度が数分続く**のが本当に注意すべき状態。

---

## 記録（ログ）とあとから確認

監視中は `logs\temps_YYYY-MM-DD.csv` に1行ずつ自動で記録される（日付ごとに1ファイル）。
Excelでそのまま開けるし、次のコマンドで要約も出せる。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\monitor_temps.ps1 -Summary
```

```
[監視] 集計: ...\logs\temps_2026-08-11.csv （5 サンプル）
期間: 2026-08-11 20:00:00  →  2026-08-11 20:00:40

CPU:  最低  62.0℃   平均  77.7℃   最高  91.2℃   警告域 2回 (40.0%)
GPU:  最低  58.0℃   平均  73.7℃   最高  88.5℃   警告域 2回 (40.0%)

危険域の記録はありません。この設定なら放置運用して大丈夫そうです。
```

**配信を1本やったあとに必ず1回これを見る**のがおすすめ。放置運用してよいかの判断材料になる。

CSVの列は `timestamp, cpu_c, gpu_c, gpu_util_pct, gpu_mem_used_mb, gpu_mem_total_mb,
gpu_power_w, gpu_fan_pct, status`。取得できなかった値は空欄になる
（ノートPCでは消費電力やファン回転数が取れないことが多い）。

`logs\` と `tools\` は `.gitignore` 済みなのでコミットされない。

---

## よく使うオプション

```powershell
# 5秒おきに、2時間で自動終了
... \monitor_temps.ps1 -IntervalSeconds 5 -DurationMinutes 120

# 危険域になったらしゃべらせる（別途 nircmd などが必要）
... \monitor_temps.ps1 -OnCritical 'nircmd.exe speak text "GPUが熱いです"'

# ビープ音を鳴らさない（配信に音が乗るのが嫌なとき）
... \monitor_temps.ps1 -NoSound

# CSVを残さない
... \monitor_temps.ps1 -NoLog
```

| オプション | 既定値 | 意味 |
|---|---|---|
| `-IntervalSeconds` | 10 | 測定間隔（秒） |
| `-DurationMinutes` | 0（無制限） | 指定分で自動終了 |
| `-Once` | — | 1回だけ測って終了 |
| `-Setup` | — | LibreHardwareMonitorを導入（導入済みなら何もしない） |
| `-Summary` | — | ログを集計。`-LogFile` で対象CSVを指定可 |
| `-CpuWarn` / `-CpuCrit` | 85 / 95 | CPUのしきい値（℃） |
| `-GpuWarn` / `-GpuCrit` | 80 / 87 | GPUのしきい値（℃） |
| `-LogDir` | `logs\` | CSVの保存先 |
| `-NoLog` | — | CSVを書かない |
| `-ObsTextPath` | — | OBS表示用のテキストを書き出す（下記） |
| `-OnCritical` | — | 危険域で1回だけ実行するコマンド（5分に1回まで） |
| `-NoSound` | — | ビープ音を鳴らさない |

---

## OBSの画面に温度を出す（任意）

配信画面の隅に小さく温度を出しておくと、スマホから配信を眺めるだけで
PCの状態がわかる。身内向け配信やテスト配信では便利。

1. 監視を `-ObsTextPath` 付きで起動する:

   ```powershell
   ... \monitor_temps.ps1 -ObsTextPath "$HOME\Documents\obs_temps.txt"
   ```

2. OBS → ソース → **テキスト (GDI+)** を追加
3. **「ファイルから読み取る」にチェック** → 上のファイルを選択
4. フォントを小さめにして画面の隅へ

`CPU 72℃ / GPU 69℃` という1行が10秒おきに更新される。
（本配信で視聴者に見せる必要はないので、その場合はソースを非表示にしておく）

---

## 仕組み

| 測るもの | 使う仕組み | 追加インストール |
|---|---|---|
| GPU温度・使用率・VRAM | `nvidia-smi`（NVIDIAドライバ同梱） | **不要** |
| CPU温度（本命） | LibreHardwareMonitor（MPL-2.0・無料OSS） | `-Setup` で自動 |
| CPU温度（代替） | WMI `MSAcpi_ThermalZoneTemperature` | 不要（ただしノートPCでは非対応が多い） |

CPU温度は上から順に試して、取れた方法を使う。**どちらも取れなくてもGPU監視は続く**
（GPUの方が熱で先に音を上げるので、最悪GPUだけでも実用になる）。

`-Setup` は GitHub の最新リリースzipを `tools\LibreHardwareMonitor\` に展開するだけ。
`start_temp_monitor.bat` は毎回 `-Setup` を呼ぶが、導入済みなら何もしない。

### なぜ管理者権限が必要か

CPUの温度センサーはOSの通常APIからは読めず、CPUの内部レジスタを直接読むための
カーネルドライバが要る。LibreHardwareMonitorがそれを一時的に読み込むため、
管理者権限が必要になる。`.bat` から起動すれば自動で昇格する。

---

## うまくいかないとき

**CPU温度が `--` のまま**

1. `.bat` から起動しているか確認（管理者権限が要る）
2. `-Setup` が成功しているか確認 → `tools\LibreHardwareMonitor\` にファイルがあるか
3. PowerShell 7 (`pwsh`) で直接起動していないか確認。LibreHardwareMonitorは
   .NET Framework版なので **Windows PowerShell 5.1（`powershell.exe`）が必要**。
   `.bat` は自動で5.1を使う

**GPU温度が `--` のまま**

`nvidia-smi` が見つかっていない。PowerShellで `nvidia-smi` と打って動くか確認。
動かない場合はNVIDIAドライバを入れ直す。

**「このシステムではスクリプトの実行が無効になっている」と出る**

`.bat` から起動する（`-ExecutionPolicy Bypass` が付く）。直接 `.ps1` を実行しない。

**文字化けする**

`.bat` から起動する（文字コードをUTF-8に切り替えている）。

---

## 温度が下がらないときの対処

ソフト側でできることは限られるので、まず物理面から。

1. **吸気口をふさいでいないか** — ノートPCは底面と側面から吸う。布団・膝の上は最悪
2. **ノートPCスタンドで浮かせる** — 数百円のもので5〜10℃下がることがある
3. **配信設定を下げる** — OBSのエンコーダをNVENCにする、解像度を1080p→720pに、
   フレームレートを60→30に。特に**フレームレート半減の効果が大きい**
4. **Style-Bert-VITS2をCPU合成にする** — VRAM 4GBでGPU合成すると
   Live2D描画とVRAMを取り合って発熱も増える（docs/04参照）
5. **Windowsの電源プランを「バランス」に** — 「最適なパフォーマンス」は熱に不利

---

## 開発者向け

ロジックのテスト（センサーが無い環境でも動く部分）:

```powershell
powershell -ExecutionPolicy Bypass -File tests\test_monitor_temps.ps1
```

`monitor_temps.ps1` を変更したら必ず実行すること（65項目）。しきい値判定・`nvidia-smi`
出力のパース・CSVの書式・OBSテキスト出力に加え、センサーを差し替えて**監視1周分を
実際に走らせる**通し確認（正常／危険域／CPUだけ取れない場合／`-NoLog`／`-Summary`）まで行う。

項目を増やしたら末尾の `$expectedChecks` も更新すること。例外で検証が途中で
打ち切られたのに「パス」と表示されるのを防ぐための番人になっている。

`.ps1` は **UTF-8 BOM付き** で保存する。BOMが無いとWindows PowerShell 5.1が
ANSIとして読み、日本語のメッセージが文字化けする。
