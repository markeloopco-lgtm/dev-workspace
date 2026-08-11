<#
    monitor_temps.ps1 のロジック検証。センサーが無い環境（LinuxのCI等）でも動く部分だけを見る。

    実行:
        powershell -ExecutionPolicy Bypass -File tests\test_monitor_temps.ps1
#>
$ErrorActionPreference = 'Stop'

$script:Failures = 0
$script:Checks = 0

function Assert-Equal {
    param($Expected, $Actual, [string]$Name)
    $script:Checks++
    $e = if ($null -eq $Expected) { '<null>' } else { "$Expected" }
    $a = if ($null -eq $Actual) { '<null>' } else { "$Actual" }
    if ($e -eq $a) {
        Write-Host "  ok   $Name" -ForegroundColor Green
    } else {
        Write-Host "  NG   $Name  (期待: $e / 実際: $a)" -ForegroundColor Red
        $script:Failures++
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$target = Join-Path $repoRoot 'scripts/monitor_temps.ps1'

# 構文チェック
Write-Host '構文チェック' -ForegroundColor Cyan
$parseErrors = $null
$tokens = $null
[System.Management.Automation.Language.Parser]::ParseFile($target, [ref]$tokens, [ref]$parseErrors) | Out-Null
Assert-Equal 0 $parseErrors.Count 'monitor_temps.ps1 が構文エラーなくパースできる'
if ($parseErrors.Count -gt 0) {
    $parseErrors | ForEach-Object { Write-Host "       $($_.Extent.StartLineNumber): $($_.Message)" -ForegroundColor Red }
    exit 1
}

# 関数だけ読み込む（末尾のエントリポイントはドットソース時に走らない）
. $target

Write-Host ''
Write-Host 'ConvertTo-Metric（nvidia-smi出力のパース）' -ForegroundColor Cyan
Assert-Equal 68     (ConvertTo-Metric '68')            '整数を数値化'
Assert-Equal 14.62  (ConvertTo-Metric ' 14.62 ')       '小数と前後空白'
Assert-Equal $null  (ConvertTo-Metric '[N/A]')         '[N/A] は null'
Assert-Equal $null  (ConvertTo-Metric 'N/A')           'N/A は null'
Assert-Equal $null  (ConvertTo-Metric '[Not Supported]') 'Not Supported は null'
Assert-Equal $null  (ConvertTo-Metric '')              '空文字は null'
Assert-Equal $null  (ConvertTo-Metric $null)           'null は null'

Write-Host ''
Write-Host 'Get-Status（しきい値判定）' -ForegroundColor Cyan
Assert-Equal 'ok'   (Get-Status -Value 70 -Warn 80 -Crit 87)   '警告未満は ok'
Assert-Equal 'warn' (Get-Status -Value 80 -Warn 80 -Crit 87)   '警告ちょうどは warn'
Assert-Equal 'warn' (Get-Status -Value 86.9 -Warn 80 -Crit 87) '危険直前は warn'
Assert-Equal 'crit' (Get-Status -Value 87 -Warn 80 -Crit 87)   '危険ちょうどは crit'
Assert-Equal 'na'   (Get-Status -Value $null -Warn 80 -Crit 87) '未取得は na'

Write-Host ''
Write-Host 'Get-WorstStatus（CPUとGPUの悪い方を採用）' -ForegroundColor Cyan
Assert-Equal 'crit' (Get-WorstStatus @('ok', 'crit'))   'ok と crit なら crit'
Assert-Equal 'warn' (Get-WorstStatus @('warn', 'ok'))   'warn と ok なら warn'
Assert-Equal 'ok'   (Get-WorstStatus @('ok', 'na'))     'ok と na なら ok（片方未取得でも監視継続）'
Assert-Equal 'na'   (Get-WorstStatus @('na', 'na'))     '両方未取得なら na'
Assert-Equal 'crit' (Get-WorstStatus @('crit', 'warn')) 'crit と warn なら crit'

Write-Host ''
Write-Host 'CSVの書き出しと集計のラウンドトリップ' -ForegroundColor Cyan
$tmpDir = Join-Path ([IO.Path]::GetTempPath()) ("montemp_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
try {
    $csv = Join-Path $tmpDir 'temps_test.csv'
    $base = Get-Date '2026-08-11 20:00:00'
    # 40℃から1℃ずつ上げて、GPUが危険域(87℃)に入るところまで書く
    0..49 | ForEach-Object {
        $gpuC = 40.0 + $_
        $cpuC = 45.0 + $_
        $sample = [pscustomobject]@{
            Time       = $base.AddSeconds(10 * $_)
            CpuC       = $cpuC
            CpuStatus  = (Get-Status -Value $cpuC -Warn 85 -Crit 95)
            GpuC       = $gpuC
            GpuStatus  = (Get-Status -Value $gpuC -Warn 80 -Crit 87)
            GpuName    = 'NVIDIA GeForce RTX 3050 Laptop GPU'
            UtilPct    = 50
            MemUsedMB  = 2048
            MemTotalMB = 4096
            PowerW     = $null      # ノートPCでは取れないことがある
            FanPct     = $null
            Status     = (Get-WorstStatus @(
                            (Get-Status -Value $cpuC -Warn 85 -Crit 95),
                            (Get-Status -Value $gpuC -Warn 80 -Crit 87)))
        }
        Write-CsvRow -Sample $sample -Path $csv
    }

    $rows = @(Import-Csv -Path $csv)
    Assert-Equal 50 $rows.Count '50行書き出せている'
    Assert-Equal 'timestamp' (($rows[0].PSObject.Properties.Name)[0]) '1列目はtimestamp'
    Assert-Equal '40.0' $rows[0].gpu_c        '1行目のGPU温度'
    Assert-Equal '89.0' $rows[-1].gpu_c       '最終行のGPU温度'
    Assert-Equal ''     $rows[0].gpu_power_w  '取得できない値は空欄'
    Assert-Equal 'ok'   $rows[0].status       '1行目は正常'
    Assert-Equal 'crit' $rows[-1].status      '最終行は危険'

    # 40..89℃のうち80℃以上は10件
    $critRows = @($rows | Where-Object { $_.status -eq 'crit' })
    Assert-Equal 3 $critRows.Count 'GPU 87℃以上が3件（87,88,89）'

    $gpuValues = @($rows | ForEach-Object { ConvertTo-Metric $_.gpu_c } | Where-Object { $null -ne $_ })
    $stats = $gpuValues | Measure-Object -Minimum -Maximum -Average
    Assert-Equal 40 $stats.Minimum 'GPU最低温度'
    Assert-Equal 89 $stats.Maximum 'GPU最高温度'

    # ヘッダーが二重に書かれないこと
    $headerCount = @(Get-Content $csv | Where-Object { $_ -like 'timestamp,*' }).Count
    Assert-Equal 1 $headerCount 'ヘッダー行は1回だけ'

    Write-Host ''
    Write-Host 'OBS用テキスト出力' -ForegroundColor Cyan
    $obs = Join-Path $tmpDir 'nested/obs_temps.txt'
    Write-ObsText -Sample ([pscustomobject]@{ CpuC = 72.4; GpuC = 68.9 }) -Path $obs
    Assert-Equal $true (Test-Path $obs) 'サブフォルダごと作成される'
    Assert-Equal 'CPU 72℃ / GPU 69℃' (Get-Content $obs -Raw) '四捨五入して1行で書かれる'
    $bytes = [IO.File]::ReadAllBytes($obs)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    Assert-Equal $false $hasBom 'BOM無し（OBSで先頭に化けた文字が出ない）'

    Write-Host ''
    Write-Host '未取得の値の表示' -ForegroundColor Cyan
    Assert-Equal '  --  ' (Format-Temp $null) 'null は -- 表示'
    Assert-Equal ' 68.0℃' (Format-Temp 68.0)  '数値は小数1桁'

    Write-Host ''
    Write-Host 'nvidia-smiの実出力のパース' -ForegroundColor Cyan
    # RTX 3050 Laptop の典型的な1行（消費電力とファンは取れないことが多い）
    $g = ConvertFrom-NvidiaSmiCsv -Line 'NVIDIA GeForce RTX 3050 Laptop GPU, 58, 30, 1843, 4096, [N/A], [N/A]'
    Assert-Equal 'NVIDIA GeForce RTX 3050 Laptop GPU' $g.Name 'GPU名'
    Assert-Equal 58    $g.TempC      '温度'
    Assert-Equal 30    $g.UtilPct    '使用率'
    Assert-Equal 1843  $g.MemUsedMB  'VRAM使用量'
    Assert-Equal 4096  $g.MemTotalMB 'VRAM総量'
    Assert-Equal $null $g.PowerW     '消費電力が[N/A]ならnull'
    Assert-Equal $null $g.FanPct     'ファンが[N/A]ならnull'

    # 値が全部取れるデスクトップGPUのケース
    $g2 = ConvertFrom-NvidiaSmiCsv -Line 'NVIDIA GeForce RTX 4070, 71, 99, 5120, 12288, 184.52, 62'
    Assert-Equal 184.52 $g2.PowerW '消費電力（小数）'
    Assert-Equal 62     $g2.FanPct 'ファン回転数'

    Assert-Equal $null (ConvertFrom-NvidiaSmiCsv -Line '')            '空行はnull'
    Assert-Equal $null (ConvertFrom-NvidiaSmiCsv -Line 'GPU, 58, 30') '列が足りない行はnull'

    Write-Host ''
    Write-Host '監視1周分の通し動作（センサーを差し替えて実行）' -ForegroundColor Cyan

    # 実機のセンサーが無い環境で Start-Watch を通すためのスタブ
    $script:StubGpuLine = 'NVIDIA GeForce RTX 3050 Laptop GPU, 58, 30, 1843, 4096, [N/A], [N/A]'
    $script:StubCpuC = 62.0
    function Test-IsWindows { return $true }
    function Get-GpuSample { return ConvertFrom-NvidiaSmiCsv -Line $script:StubGpuLine }
    function Get-CpuSample { $script:CpuSource = 'Stub'; return $script:StubCpuC }

    # Start-Watch が読む設定（param既定値と同じ位置づけ）
    $LogDir = $tmpDir
    $NoLog = $false
    $NoSound = $true
    $ObsTextPath = Join-Path $tmpDir 'obs_live.txt'
    $OnCritical = ''
    $CpuWarn = 85; $CpuCrit = 95; $GpuWarn = 80; $GpuCrit = 87

    $rc = Start-Watch -SingleShot $true
    Assert-Equal 0 $rc '正常時の戻り値は0'

    $liveCsv = Join-Path $tmpDir ('temps_{0:yyyy-MM-dd}.csv' -f (Get-Date))
    Assert-Equal $true (Test-Path $liveCsv) '日付入りCSVが作られる'
    $liveRows = @(Import-Csv -Path $liveCsv)
    Assert-Equal 1     $liveRows.Count      '1サンプル記録される'
    Assert-Equal '62.0' $liveRows[0].cpu_c  'CPU温度が記録される'
    Assert-Equal '58.0' $liveRows[0].gpu_c  'GPU温度が記録される'
    Assert-Equal '1843' $liveRows[0].gpu_mem_used_mb 'VRAMが記録される'
    Assert-Equal 'ok'   $liveRows[0].status '58℃/62℃は正常'
    Assert-Equal 'CPU 62℃ / GPU 58℃' (Get-Content $ObsTextPath -Raw) 'OBS用テキストも更新される'

    # 危険域: ビープと警告を出しつつ記録は続く。
    # ここは実際に熱くなったときにしか通らない経路なので、警告の組み立てが
    # 実行時エラーを出していないことまで確認する
    $script:StubGpuLine = 'NVIDIA GeForce RTX 3050 Laptop GPU, 91, 99, 3900, 4096, [N/A], [N/A]'
    $script:StubCpuC = 97.0
    $Error.Clear()
    $rc = Start-Watch -SingleShot $true
    Assert-Equal 0 $rc '危険域でも戻り値は0（監視自体は成功）'
    Assert-Equal 0 $Error.Count '危険域の警告メッセージが実行時エラーを出さない'
    if ($Error.Count -gt 0) { $Error | ForEach-Object { Write-Host "       $_" -ForegroundColor Red } }
    $liveRows = @(Import-Csv -Path $liveCsv)
    Assert-Equal 2      $liveRows.Count     '同じファイルに追記される'
    Assert-Equal 'crit' $liveRows[-1].status '91℃/97℃は危険'

    # GPUだけ取れてCPUが取れない環境でも監視が続くこと
    $script:StubGpuLine = 'NVIDIA GeForce RTX 3050 Laptop GPU, 83, 90, 3000, 4096, [N/A], [N/A]'
    function Get-CpuSample { $script:CpuSource = $null; return $null }
    $rc = Start-Watch -SingleShot $true
    Assert-Equal 0 $rc 'CPU温度が取れなくても0で終わる'
    $liveRows = @(Import-Csv -Path $liveCsv)
    Assert-Equal 3      $liveRows.Count      '3サンプル目も記録される'
    Assert-Equal ''     $liveRows[-1].cpu_c  'CPU温度は空欄'
    Assert-Equal '83.0' $liveRows[-1].gpu_c  'GPU温度は記録される'
    Assert-Equal 'warn' $liveRows[-1].status 'GPUだけで警告判定される'

    # -NoLog でCSVを書かないこと
    $before = (Import-Csv -Path $liveCsv).Count
    $NoLog = $true
    $rc = Start-Watch -SingleShot $true
    Assert-Equal $before (Import-Csv -Path $liveCsv).Count '-NoLog では追記されない'
    $NoLog = $false

    Write-Host ''
    Write-Host '集計（-Summary）' -ForegroundColor Cyan
    $LogFile = $liveCsv
    $rc = Show-Summary
    Assert-Equal 0 $rc '集計は0で終わる'
    $LogFile = $null
    $rc = Show-Summary   # LogDir内の最新を自動で拾う
    Assert-Equal 0 $rc 'ファイル未指定なら最新ログを自動で選ぶ'
} catch {
    # 例外が素通りすると残りの検証が実行されないまま「パス」と表示されてしまうため、
    # ここで必ず失敗として数える
    $script:Checks++
    $script:Failures++
    Write-Host "  NG   予期しない例外で中断: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "       $($_.InvocationInfo.PositionMessage)" -ForegroundColor DarkGray
} finally {
    Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

# 検証が途中で打ち切られていないかの番人（項目を増やしたらこの数も更新する）
$expectedChecks = 65
if ($script:Checks -lt $expectedChecks) {
    Write-Host "  NG   検証が途中で終わっています（$script:Checks / $expectedChecks 件しか実行されていない）" -ForegroundColor Red
    $script:Failures++
}

Write-Host ''
if ($script:Failures -eq 0) {
    Write-Host "全 $script:Checks 件パス" -ForegroundColor Green
    exit 0
} else {
    Write-Host "$script:Failures / $script:Checks 件失敗" -ForegroundColor Red
    exit 1
}
