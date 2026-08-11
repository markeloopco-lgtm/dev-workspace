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
} finally {
    Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
if ($script:Failures -eq 0) {
    Write-Host "全 $script:Checks 件パス" -ForegroundColor Green
    exit 0
} else {
    Write-Host "$script:Failures / $script:Checks 件失敗" -ForegroundColor Red
    exit 1
}
