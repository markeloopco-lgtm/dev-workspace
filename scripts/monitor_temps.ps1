#Requires -Version 5.1
<#
.SYNOPSIS
    GPU/CPUの温度を自動で監視・記録する。配信の放置運用向け。

.DESCRIPTION
    GPU温度はNVIDIAドライバ同梱の nvidia-smi から取得する（追加インストール不要）。
    CPU温度は以下の順に試し、取れた方法を使う:
      1. LibreHardwareMonitor (最も正確・要管理者権限)  ... -Setup で導入
      2. WMI MSAcpi_ThermalZoneTemperature (追加導入不要だがノートPCでは非対応が多い)
    どちらも取れない場合はGPUのみ監視を続ける。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\monitor_temps.ps1 -Once
    いま何度か1回だけ表示する。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\monitor_temps.ps1
    10秒おきに監視し続ける（Ctrl+Cで終了）。logs\ にCSVが残る。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\monitor_temps.ps1 -Summary
    直近のログを集計して最高/平均温度を出す。
#>
[CmdletBinding(DefaultParameterSetName = 'Watch')]
param(
    # 監視間隔（秒）
    [Parameter(ParameterSetName = 'Watch')]
    [ValidateRange(1, 3600)]
    [int]$IntervalSeconds = 10,

    # 指定分だけ監視して自動終了する（0 = Ctrl+Cするまでずっと）
    [Parameter(ParameterSetName = 'Watch')]
    [ValidateRange(0, 10080)]
    [int]$DurationMinutes = 0,

    # 1回だけ測って終了
    [Parameter(ParameterSetName = 'Once', Mandatory = $true)]
    [switch]$Once,

    # LibreHardwareMonitorをtools\に導入する（CPU温度の精度が上がる）
    [Parameter(ParameterSetName = 'Setup', Mandatory = $true)]
    [switch]$Setup,

    # 記録済みログを集計する
    [Parameter(ParameterSetName = 'Summary', Mandatory = $true)]
    [switch]$Summary,

    # 集計するCSV（省略時はLogDir内の最新ファイル）
    [Parameter(ParameterSetName = 'Summary')]
    [string]$LogFile,

    # しきい値（℃）。ノートPCは総じて高めなので実測に合わせて調整してよい
    [int]$CpuWarn = 85,
    [int]$CpuCrit = 95,
    [int]$GpuWarn = 80,
    [int]$GpuCrit = 87,

    # CSVの保存先（既定: <リポジトリ>\logs）
    [string]$LogDir,

    # CSVを書かない
    [switch]$NoLog,

    # OBSのテキストソース用に現在値を書き出すファイル
    [string]$ObsTextPath,

    # 危険域になったとき1回だけ実行するコマンド（例: 'nircmd.exe speak text "GPUが熱いです"'）
    [string]$OnCritical,

    # 危険域でもビープ音を鳴らさない
    [switch]$NoSound
)

$ErrorActionPreference = 'Continue'

# ---------------------------------------------------------------- パス・共通出力

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:ToolsDir = Join-Path $script:RepoRoot 'tools'
if (-not $LogDir) { $LogDir = Join-Path $script:RepoRoot 'logs' }

function Write-Info { param([string]$Message) Write-Host "[監視] $Message" -ForegroundColor Cyan }
function Write-Warn { param([string]$Message) Write-Host "[注意] $Message" -ForegroundColor Yellow }
function Write-Err  { param([string]$Message) Write-Host "[エラー] $Message" -ForegroundColor Red }

function Test-IsWindows {
    # PS5.1には$IsWindowsが無い（= Windows確定）、PS7では変数がある
    if ($null -eq (Get-Variable -Name 'IsWindows' -ErrorAction SilentlyContinue)) { return $true }
    return $IsWindows
}

function Test-IsAdmin {
    if (-not (Test-IsWindows)) { return $false }
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($id)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

# nvidia-smiの "[N/A]" や空文字を$nullに寄せる
function ConvertTo-Metric {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $t = $Text.Trim()
    if ($t -match '^\[?(N/A|Not Supported|Unknown)\]?$') { return $null }
    $value = 0.0
    if ([double]::TryParse($t, [ref]$value)) { return $value }
    return $null
}

# ---------------------------------------------------------------- GPU (nvidia-smi)

function Resolve-NvidiaSmi {
    $cmd = Get-Command 'nvidia-smi' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'),
        (Join-Path ${env:ProgramFiles} 'NVIDIA Corporation\NVSMI\nvidia-smi.exe')
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

$script:NvidiaSmi = $null
$script:NvidiaSmiChecked = $false

function Get-GpuSample {
    if (-not $script:NvidiaSmiChecked) {
        $script:NvidiaSmi = Resolve-NvidiaSmi
        $script:NvidiaSmiChecked = $true
        if (-not $script:NvidiaSmi) {
            Write-Warn 'nvidia-smi が見つかりません。GPU温度は取得できません（NVIDIAドライバが必要）。'
        }
    }
    if (-not $script:NvidiaSmi) { return $null }

    $query = 'name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed'
    try {
        $raw = & $script:NvidiaSmi "--query-gpu=$query" '--format=csv,noheader,nounits' 2>$null
    } catch {
        Write-Warn "nvidia-smi の実行に失敗しました: $($_.Exception.Message)"
        return $null
    }
    if (-not $raw) { return $null }

    # 複数GPUなら1台目（このPCはRTX 3050の1台構成）
    return ConvertFrom-NvidiaSmiCsv -Line (@($raw)[0])
}

# nvidia-smi --format=csv,noheader,nounits の1行をパースする
# 例: 'NVIDIA GeForce RTX 3050 Laptop GPU, 58, 30, 1843, 4096, [N/A], [N/A]'
function ConvertFrom-NvidiaSmiCsv {
    param([string]$Line)
    if ([string]::IsNullOrWhiteSpace($Line)) { return $null }
    $cols = @($Line -split ',' | ForEach-Object { $_.Trim() })
    if ($cols.Count -lt 7) { return $null }

    return [pscustomobject]@{
        Name       = $cols[0]
        TempC      = ConvertTo-Metric $cols[1]
        UtilPct    = ConvertTo-Metric $cols[2]
        MemUsedMB  = ConvertTo-Metric $cols[3]
        MemTotalMB = ConvertTo-Metric $cols[4]
        PowerW     = ConvertTo-Metric $cols[5]
        FanPct     = ConvertTo-Metric $cols[6]
    }
}

# ---------------------------------------------------------------- CPU (LibreHardwareMonitor)

$script:Lhm = $null
$script:LhmTried = $false

function Get-LhmDll {
    $dir = Join-Path $script:ToolsDir 'LibreHardwareMonitor'
    if (-not (Test-Path $dir)) { return $null }
    return Get-ChildItem -Path $dir -Filter 'LibreHardwareMonitorLib.dll' -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Initialize-Lhm {
    if ($script:LhmTried) { return $script:Lhm }
    $script:LhmTried = $true
    if (-not (Test-IsWindows)) { return $null }

    $dll = Get-LhmDll
    if (-not $dll) { return $null }

    if ($PSVersionTable.PSEdition -eq 'Core') {
        Write-Warn 'PowerShell 7で実行中です。LibreHardwareMonitorは .NET Framework 版のため読み込めない可能性があります。'
        Write-Warn 'CPU温度が出ない場合は start_temp_monitor.bat から（Windows PowerShell 5.1で）起動してください。'
    }
    if (-not (Test-IsAdmin)) {
        Write-Warn 'CPU温度センサーの読み取りには管理者権限が必要です（start_temp_monitor.bat が自動で昇格します）。'
    }

    try {
        # 依存DLL（HidSharp等）を先に読み込んでから本体を読む
        $dlls = Get-ChildItem -Path $dll.Directory.FullName -Filter '*.dll' -ErrorAction SilentlyContinue |
            Sort-Object { if ($_.Name -eq 'LibreHardwareMonitorLib.dll') { 1 } else { 0 } }
        foreach ($f in $dlls) {
            try { Add-Type -Path $f.FullName -ErrorAction Stop } catch { }
        }
        $computer = New-Object LibreHardwareMonitor.Hardware.Computer
        $computer.IsCpuEnabled = $true
        $computer.IsGpuEnabled = $true
        $computer.IsMotherboardEnabled = $true
        $computer.Open()
        $script:Lhm = $computer
        Write-Info 'LibreHardwareMonitor からCPU温度を取得します。'
    } catch {
        Write-Warn "LibreHardwareMonitor を初期化できませんでした: $($_.Exception.Message)"
        $script:Lhm = $null
    }
    return $script:Lhm
}

function Get-LhmTemps {
    param($Computer)
    $package = $null
    $coreMax = $null
    $gpu = $null
    try {
        foreach ($hw in $Computer.Hardware) {
            $hw.Update()
            foreach ($sub in $hw.SubHardware) { $sub.Update() }
            $type = $hw.HardwareType.ToString()
            foreach ($s in $hw.Sensors) {
                if ($s.SensorType.ToString() -ne 'Temperature') { continue }
                if ($null -eq $s.Value) { continue }
                $v = [double]$s.Value
                if ($v -le 0 -or $v -gt 150) { continue }
                if ($type -eq 'Cpu') {
                    if ($s.Name -match 'Package|Tctl|Tdie') {
                        if ($null -eq $package -or $v -gt $package) { $package = $v }
                    } elseif ($s.Name -match 'Core') {
                        if ($null -eq $coreMax -or $v -gt $coreMax) { $coreMax = $v }
                    }
                } elseif ($type -like 'Gpu*') {
                    if ($s.Name -match 'Core|Hot ?Spot|GPU') {
                        if ($null -eq $gpu -or $v -gt $gpu) { $gpu = $v }
                    }
                }
            }
        }
    } catch {
        return $null
    }
    $cpu = if ($null -ne $package) { $package } else { $coreMax }
    return [pscustomobject]@{ CpuC = $cpu; GpuC = $gpu }
}

# ---------------------------------------------------------------- CPU (WMI フォールバック)

function Get-WmiCpuTemp {
    if (-not (Test-IsWindows)) { return $null }
    try {
        $zones = Get-CimInstance -Namespace 'root/wmi' -ClassName 'MSAcpi_ThermalZoneTemperature' -ErrorAction Stop
    } catch {
        return $null
    }
    if (-not $zones) { return $null }
    $max = $null
    foreach ($z in $zones) {
        if ($null -eq $z.CurrentTemperature) { continue }
        # 0.1ケルビン単位で返る
        $c = ($z.CurrentTemperature / 10.0) - 273.15
        if ($c -le 0 -or $c -gt 150) { continue }
        if ($null -eq $max -or $c -gt $max) { $max = $c }
    }
    return $max
}

$script:CpuSource = $null

function Get-CpuSample {
    $lhm = Initialize-Lhm
    if ($lhm) {
        $r = Get-LhmTemps -Computer $lhm
        if ($r -and $null -ne $r.CpuC) {
            $script:CpuSource = 'LibreHardwareMonitor'
            return $r.CpuC
        }
    }
    $wmi = Get-WmiCpuTemp
    if ($null -ne $wmi) {
        if ($script:CpuSource -ne 'WMI') { $script:CpuSource = 'WMI' }
        return $wmi
    }
    $script:CpuSource = $null
    return $null
}

# ---------------------------------------------------------------- 判定・表示

function Get-Status {
    param([Nullable[double]]$Value, [int]$Warn, [int]$Crit)
    if ($null -eq $Value) { return 'na' }
    if ($Value -ge $Crit) { return 'crit' }
    if ($Value -ge $Warn) { return 'warn' }
    return 'ok'
}

function Get-WorstStatus {
    param([string[]]$Statuses)
    $rank = @{ 'na' = 0; 'ok' = 1; 'warn' = 2; 'crit' = 3 }
    $worst = 'na'
    foreach ($s in $Statuses) {
        if ($rank[$s] -gt $rank[$worst]) { $worst = $s }
    }
    return $worst
}

function Get-StatusColor {
    param([string]$Status)
    switch ($Status) {
        'crit' { return 'Red' }
        'warn' { return 'Yellow' }
        'ok'   { return 'Green' }
        default { return 'DarkGray' }
    }
}

function Get-StatusLabel {
    param([string]$Status)
    switch ($Status) {
        'crit' { return '危険' }
        'warn' { return '警告' }
        'ok'   { return '正常' }
        default { return '不明' }
    }
}

function Format-Temp {
    param([Nullable[double]]$Value)
    if ($null -eq $Value) { return '  --  ' }
    return ('{0,5:F1}℃' -f $Value)
}

function New-Sample {
    $gpu = Get-GpuSample
    $cpuC = Get-CpuSample
    # LHM経由ならGPU温度も取れるが、nvidia-smiの方が素直なので優先する
    $gpuC = if ($gpu) { $gpu.TempC } else { $null }

    $cpuStatus = Get-Status -Value $cpuC -Warn $CpuWarn -Crit $CpuCrit
    $gpuStatus = Get-Status -Value $gpuC -Warn $GpuWarn -Crit $GpuCrit

    $sample = [pscustomobject]@{
        Time       = Get-Date
        CpuC       = $cpuC
        CpuStatus  = $cpuStatus
        GpuC       = $gpuC
        GpuStatus  = $gpuStatus
        GpuName    = $null
        UtilPct    = $null
        MemUsedMB  = $null
        MemTotalMB = $null
        PowerW     = $null
        FanPct     = $null
        Status     = Get-WorstStatus @($cpuStatus, $gpuStatus)
    }
    if ($gpu) {
        $sample.GpuName    = $gpu.Name
        $sample.UtilPct    = $gpu.UtilPct
        $sample.MemUsedMB  = $gpu.MemUsedMB
        $sample.MemTotalMB = $gpu.MemTotalMB
        $sample.PowerW     = $gpu.PowerW
        $sample.FanPct     = $gpu.FanPct
    }
    return $sample
}

function Write-SampleLine {
    param($Sample)
    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add(('[{0:HH:mm:ss}]' -f $Sample.Time))
    $parts.Add(('CPU {0}' -f (Format-Temp $Sample.CpuC)))
    $parts.Add(('GPU {0}' -f (Format-Temp $Sample.GpuC)))
    if ($null -ne $Sample.UtilPct) { $parts.Add(('負荷 {0,3:F0}%' -f $Sample.UtilPct)) }
    if ($null -ne $Sample.MemUsedMB -and $null -ne $Sample.MemTotalMB -and $Sample.MemTotalMB -gt 0) {
        $parts.Add(('VRAM {0:F1}/{1:F1}GB' -f ($Sample.MemUsedMB / 1024), ($Sample.MemTotalMB / 1024)))
    }
    if ($null -ne $Sample.PowerW) { $parts.Add(('{0,3:F0}W' -f $Sample.PowerW)) }
    $line = ($parts -join '  ') + '   ' + (Get-StatusLabel $Sample.Status)
    Write-Host $line -ForegroundColor (Get-StatusColor $Sample.Status)
}

# ---------------------------------------------------------------- 記録・通知

function Get-LogPath {
    $name = 'temps_{0:yyyy-MM-dd}.csv' -f (Get-Date)
    return Join-Path $LogDir $name
}

function Write-CsvRow {
    param($Sample, [string]$Path)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    if (-not (Test-Path $Path)) {
        $header = 'timestamp,cpu_c,gpu_c,gpu_util_pct,gpu_mem_used_mb,gpu_mem_total_mb,gpu_power_w,gpu_fan_pct,status'
        [System.IO.File]::WriteAllText($Path, $header + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
    }
    $fields = @(
        ('{0:yyyy-MM-dd HH:mm:ss}' -f $Sample.Time)
        if ($null -ne $Sample.CpuC)       { '{0:F1}' -f $Sample.CpuC }       else { '' }
        if ($null -ne $Sample.GpuC)       { '{0:F1}' -f $Sample.GpuC }       else { '' }
        if ($null -ne $Sample.UtilPct)    { '{0:F0}' -f $Sample.UtilPct }    else { '' }
        if ($null -ne $Sample.MemUsedMB)  { '{0:F0}' -f $Sample.MemUsedMB }  else { '' }
        if ($null -ne $Sample.MemTotalMB) { '{0:F0}' -f $Sample.MemTotalMB } else { '' }
        if ($null -ne $Sample.PowerW)     { '{0:F1}' -f $Sample.PowerW }     else { '' }
        if ($null -ne $Sample.FanPct)     { '{0:F0}' -f $Sample.FanPct }     else { '' }
        $Sample.Status
    )
    [System.IO.File]::AppendAllText($Path, ($fields -join ',') + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
}

function Write-ObsText {
    param($Sample, [string]$Path)
    $cpu = if ($null -ne $Sample.CpuC) { '{0:F0}℃' -f $Sample.CpuC } else { '--' }
    $gpu = if ($null -ne $Sample.GpuC) { '{0:F0}℃' -f $Sample.GpuC } else { '--' }
    $text = "CPU $cpu / GPU $gpu"
    try {
        $dir = Split-Path -Parent $Path
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        [System.IO.File]::WriteAllText($Path, $text, (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        Write-Warn "OBS用テキストを書けませんでした: $($_.Exception.Message)"
    }
}

$script:LastAlert = [datetime]::MinValue
$script:AlertCooldown = [timespan]::FromMinutes(5)

function Invoke-CriticalAlert {
    param($Sample)
    $now = Get-Date
    if (($now - $script:LastAlert) -lt $script:AlertCooldown) { return }
    $script:LastAlert = $now

    $detail = New-Object System.Collections.Generic.List[string]
    # -f 全体を括弧で囲むこと。囲まないとカンマがメソッドの引数区切りとして解釈され、
    # 書式に渡る引数が1つだけになって実行時エラーになる
    if ($Sample.CpuStatus -eq 'crit') { $detail.Add(('CPU {0:F1}℃ (しきい値 {1}℃)' -f $Sample.CpuC, $CpuCrit)) }
    if ($Sample.GpuStatus -eq 'crit') { $detail.Add(('GPU {0:F1}℃ (しきい値 {1}℃)' -f $Sample.GpuC, $GpuCrit)) }
    Write-Err ('温度が危険域です: ' + ($detail -join ' / '))
    Write-Err '配信を止める・負荷を下げる・吸気口をふさいでいないか確認してください。'

    if (-not $NoSound) {
        try { 1..3 | ForEach-Object { [Console]::Beep(1200, 250) } } catch { }
    }
    if ($OnCritical) {
        try { Invoke-Expression $OnCritical } catch { Write-Warn "OnCriticalの実行に失敗: $($_.Exception.Message)" }
    }
}

# ---------------------------------------------------------------- -Setup

function Invoke-Setup {
    if (-not (Test-IsWindows)) { Write-Err 'このセットアップはWindows専用です。'; return 1 }

    $dest = Join-Path $script:ToolsDir 'LibreHardwareMonitor'
    if (Get-LhmDll) {
        Write-Info "導入済みです: $dest"
        return 0
    }

    Write-Info 'LibreHardwareMonitor（無料・OSS）の最新版をダウンロードします...'
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    } catch { }

    $api = 'https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest'
    try {
        $release = Invoke-RestMethod -Uri $api -Headers @{ 'User-Agent' = 'monitor-temps' } -ErrorAction Stop
    } catch {
        Write-Err "リリース情報を取得できませんでした: $($_.Exception.Message)"
        Write-Err 'ネットワークを確認するか、https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases から'
        Write-Err "zipを手動でダウンロードし、$dest に展開してください。"
        return 1
    }

    $asset = $release.assets | Where-Object { $_.name -like '*net472*.zip' } | Select-Object -First 1
    if (-not $asset) {
        $asset = $release.assets | Where-Object { $_.name -like '*.zip' } | Select-Object -First 1
    }
    if (-not $asset) { Write-Err 'zipアセットが見つかりませんでした。'; return 1 }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) $asset.name
    try {
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp -UseBasicParsing -ErrorAction Stop
        if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest -Force | Out-Null }
        Expand-Archive -Path $tmp -DestinationPath $dest -Force -ErrorAction Stop
    } catch {
        Write-Err "ダウンロード/展開に失敗しました: $($_.Exception.Message)"
        return 1
    } finally {
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    }

    if (Get-LhmDll) {
        Write-Info "導入しました ($($release.tag_name)): $dest"
        Write-Info 'これでCPU温度が正確に取れます。監視は管理者権限で起動してください。'
        return 0
    }
    Write-Err "展開しましたが LibreHardwareMonitorLib.dll が見つかりません: $dest"
    return 1
}

# ---------------------------------------------------------------- -Summary

function Show-Summary {
    $path = $LogFile
    if (-not $path) {
        if (-not (Test-Path $LogDir)) { Write-Err "ログがありません: $LogDir"; return 1 }
        $latest = Get-ChildItem -Path $LogDir -Filter 'temps_*.csv' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $latest) { Write-Err "ログがありません: $LogDir"; return 1 }
        $path = $latest.FullName
    }
    if (-not (Test-Path $path)) { Write-Err "ファイルが見つかりません: $path"; return 1 }

    $rows = @(Import-Csv -Path $path)
    if ($rows.Count -eq 0) { Write-Err "データが空です: $path"; return 1 }

    Write-Host ''
    Write-Info "集計: $path （$($rows.Count) サンプル）"
    Write-Host ("期間: {0}  →  {1}" -f $rows[0].timestamp, $rows[-1].timestamp)
    Write-Host ''

    foreach ($spec in @(
        @{ Label = 'CPU'; Column = 'cpu_c'; Warn = $CpuWarn; Crit = $CpuCrit },
        @{ Label = 'GPU'; Column = 'gpu_c'; Warn = $GpuWarn; Crit = $GpuCrit }
    )) {
        $values = @($rows | ForEach-Object { ConvertTo-Metric $_.($spec.Column) } | Where-Object { $null -ne $_ })
        if ($values.Count -eq 0) {
            Write-Host ("{0}: データなし" -f $spec.Label) -ForegroundColor DarkGray
            continue
        }
        $stats = $values | Measure-Object -Minimum -Maximum -Average
        $overWarn = @($values | Where-Object { $_ -ge $spec.Warn }).Count
        $pct = 100.0 * $overWarn / $values.Count
        $color = if ($stats.Maximum -ge $spec.Crit) { 'Red' } elseif ($stats.Maximum -ge $spec.Warn) { 'Yellow' } else { 'Green' }
        Write-Host ("{0}:  最低 {1,5:F1}℃   平均 {2,5:F1}℃   最高 {3,5:F1}℃   警告域 {4}回 ({5:F1}%)" -f `
            $spec.Label, $stats.Minimum, $stats.Average, $stats.Maximum, $overWarn, $pct) -ForegroundColor $color
    }

    $crit = @($rows | Where-Object { $_.status -eq 'crit' })
    Write-Host ''
    if ($crit.Count -gt 0) {
        Write-Err "危険域 $($crit.Count) 回。最初の発生: $($crit[0].timestamp)"
        Write-Err '冷却の見直し（ノートPCスタンド・吸気口の確保・GPU負荷を下げる設定）を検討してください。'
    } else {
        Write-Host '危険域の記録はありません。この設定なら放置運用して大丈夫そうです。' -ForegroundColor Green
    }
    return 0
}

# ---------------------------------------------------------------- 監視ループ

function Start-Watch {
    param([bool]$SingleShot)

    if (-not (Test-IsWindows)) {
        Write-Err 'このスクリプトはWindows専用です（nvidia-smi / WMI を使います）。'
        return 1
    }

    $logPath = $null
    if (-not $NoLog) { $logPath = Get-LogPath }

    if (-not $SingleShot) {
        Write-Host ''
        Write-Info ("しきい値: CPU 警告{0}℃ / 危険{1}℃   GPU 警告{2}℃ / 危険{3}℃" -f $CpuWarn, $CpuCrit, $GpuWarn, $GpuCrit)
        if ($logPath) { Write-Info "記録先: $logPath" }
        if ($ObsTextPath) { Write-Info "OBS用テキスト: $ObsTextPath" }
        Write-Info '終了するには Ctrl+C を押してください。'
        Write-Host ''
    }

    $deadline = if ($DurationMinutes -gt 0) { (Get-Date).AddMinutes($DurationMinutes) } else { [datetime]::MaxValue }
    $cpuValues = New-Object System.Collections.Generic.List[double]
    $gpuValues = New-Object System.Collections.Generic.List[double]
    $count = 0

    try {
        while ($true) {
            $sample = New-Sample
            $count++
            if ($null -ne $sample.CpuC) { $cpuValues.Add([double]$sample.CpuC) }
            if ($null -ne $sample.GpuC) { $gpuValues.Add([double]$sample.GpuC) }

            Write-SampleLine -Sample $sample
            if (-not $NoLog) {
                # 日付をまたぐ配信でもその日のファイルに書くよう毎回引き直す
                $logPath = Get-LogPath
                Write-CsvRow -Sample $sample -Path $logPath
            }
            if ($ObsTextPath) { Write-ObsText -Sample $sample -Path $ObsTextPath }
            if ($sample.Status -eq 'crit') { Invoke-CriticalAlert -Sample $sample }

            if ($count -eq 1) {
                if ($sample.GpuName) { Write-Info "GPU: $($sample.GpuName)" }
                if ($script:CpuSource) {
                    Write-Info "CPU温度の取得元: $($script:CpuSource)"
                } else {
                    Write-Warn 'CPU温度を取得できませんでした。 -Setup で LibreHardwareMonitor を導入し、管理者権限で実行してください。'
                }
            }

            if ($SingleShot) { break }
            if ((Get-Date) -ge $deadline) { break }
            Start-Sleep -Seconds $IntervalSeconds
        }
    } finally {
        if (-not $SingleShot -and $count -gt 0) {
            Write-Host ''
            Write-Info "監視を終了しました（$count サンプル）"
            if ($cpuValues.Count -gt 0) {
                $s = $cpuValues | Measure-Object -Minimum -Maximum -Average
                Write-Host ('CPU:  平均 {0:F1}℃   最高 {1:F1}℃' -f $s.Average, $s.Maximum)
            }
            if ($gpuValues.Count -gt 0) {
                $s = $gpuValues | Measure-Object -Minimum -Maximum -Average
                Write-Host ('GPU:  平均 {0:F1}℃   最高 {1:F1}℃' -f $s.Average, $s.Maximum)
            }
            if ($logPath) { Write-Info "記録: $logPath" }
        }
        if ($script:Lhm) { try { $script:Lhm.Close() } catch { } }
    }
    return 0
}

# ---------------------------------------------------------------- エントリポイント

# ドットソース時（tests/test_monitor_temps.ps1）は関数定義だけ読み込んで実行しない
if ($MyInvocation.InvocationName -ne '.') {
    $result = switch ($PSCmdlet.ParameterSetName) {
        'Setup'   { Invoke-Setup }
        'Summary' { Show-Summary }
        'Once'    { Start-Watch -SingleShot $true }
        default   { Start-Watch -SingleShot $false }
    }
    # 関数が余計な値を返しても最後の1つを終了コードにする
    exit ([int](@($result)[-1]))
}
