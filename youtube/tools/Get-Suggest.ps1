<#
.SYNOPSIS
    YouTube / Google のサジェスト（検索候補）をまとめて取得してCSVに保存します。
    Pythonのインストール不要。Windows PowerShell だけで動きます。

.EXAMPLE
    # まずは軽く試す（1〜2分）
    .\Get-Suggest.ps1 -Shallow

.EXAMPLE
    # 本番。深掘りして取る（15〜25分かかります）
    .\Get-Suggest.ps1
#>
[CmdletBinding()]
param(
    [string]$SeedFile = "$PSScriptRoot\seeds.txt",
    [string]$Out      = "$PSScriptRoot\..\keywords\suggest.csv",
    [double]$Delay    = 0.4,
    [switch]$Shallow,
    [switch]$IncludeWeb
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not (Test-Path $SeedFile)) {
    throw "シードファイルが見つかりません: $SeedFile"
}

$seeds = Get-Content -Path $SeedFile -Encoding UTF8 |
    Where-Object { $_.Trim() -ne '' -and -not $_.StartsWith('#') } |
    ForEach-Object { $_.Trim() }

if ($seeds.Count -eq 0) { throw "シードキーワードが1つもありません" }

# 後置文字。これを付けて検索することでサジェストを深掘りします。
$expanders = @()
if (-not $Shallow) {
    $expanders += 'あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわ'.ToCharArray()
    $expanders += 'abcdefghijklmnopqrstuvwxyz'.ToCharArray()
    $expanders += '0123456789'.ToCharArray()
}

$sources = @('yt')
if ($IncludeWeb) { $sources += 'web' }

# 問い合わせるクエリを組み立てる
$queries = New-Object System.Collections.Generic.List[object]
foreach ($seed in $seeds) {
    $queries.Add([pscustomobject]@{ Seed = $seed; Query = $seed })
    foreach ($ex in $expanders) {
        $queries.Add([pscustomobject]@{ Seed = $seed; Query = "$seed $ex" })
    }
}

$total = $queries.Count * $sources.Count
Write-Host ("シード {0} 件 / 総リクエスト {1} 件（推定 {2:N1} 分）" -f $seeds.Count, $total, ($total * $Delay / 60))

$rows = New-Object System.Collections.Generic.List[object]
$seen = New-Object System.Collections.Generic.HashSet[string]
$done = 0
$errors = 0

foreach ($source in $sources) {
    foreach ($q in $queries) {
        $done++

        $url = "https://suggestqueries.google.com/complete/search?client=firefox&hl=ja&q=" +
               [uri]::EscapeDataString($q.Query)
        if ($source -eq 'yt') { $url += "&ds=yt" }

        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 10 `
                        -UserAgent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
            # 文字化けを防ぐため、生バイトからUTF-8で読み直す
            $text = [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
            $data = $text | ConvertFrom-Json
        }
        catch {
            $errors++
            if ($errors -le 5) { Write-Warning ("取得失敗 ({0}): {1}" -f $q.Query, $_.Exception.Message) }
            Start-Sleep -Seconds $Delay
            continue
        }

        $rank = 0
        foreach ($s in $data[1]) {
            $rank++
            $suggestion = [string]$s
            if ([string]::IsNullOrWhiteSpace($suggestion)) { continue }
            if (-not $seen.Add("$source|$suggestion")) { continue }
            $rows.Add([pscustomobject]@{
                source     = $source
                suggestion = $suggestion
                seed       = $q.Seed
                rank       = $rank
            })
        }

        if ($done % 25 -eq 0) {
            Write-Host ("  {0}/{1} 件完了 / 収集 {2} 語" -f $done, $total, $rows.Count)
        }
        Start-Sleep -Seconds $Delay
    }
}

$outDir = Split-Path -Parent $Out
if ($outDir -and -not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

$rows | Export-Csv -Path $Out -NoTypeInformation -Encoding UTF8

Write-Host ("完了: {0} 語を {1} に保存しました（失敗 {2} 件）" -f $rows.Count, $Out, $errors)

if ($rows.Count -eq 0) {
    Write-Warning "1語も取れていません。ネットワーク制限やプロキシ設定を確認してください。"
    exit 1
}
