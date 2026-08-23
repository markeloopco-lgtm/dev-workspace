# 翻訳切り抜き制作(Windows用ラッパー)
# 使い方:  .\tools\make_clip.ps1 001
#          .\tools\make_clip.ps1 001 -SkipFetch
param(
    [Parameter(Mandatory = $true)][string]$Clip,
    [switch]$SkipFetch
)
$ErrorActionPreference = "Stop"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Write-Error "Python が見つかりません。https://www.python.org/downloads/ からインストールしてください。"; exit 1 }

$script = Join-Path $PSScriptRoot "make_clip.py"
$argsList = @($script, $Clip)
if ($SkipFetch) { $argsList += "--skip-fetch" }
& $py.Source @argsList
exit $LASTEXITCODE
