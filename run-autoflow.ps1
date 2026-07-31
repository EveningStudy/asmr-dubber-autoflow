[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArguments
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

$ToolRoot = $PSScriptRoot
$Candidates = @()
$SettingsFile = Join-Path $ToolRoot "settings.txt"
if (Test-Path -LiteralPath $SettingsFile -PathType Leaf) {
    foreach ($Line in Get-Content -LiteralPath $SettingsFile -Encoding UTF8) {
        if ($Line -match '^\s*asmr_dubber_path\s*=\s*(.*?)\s*$') {
            $ConfiguredRoot = $Matches[1].Trim().Trim('"').Trim("'")
            if ($ConfiguredRoot) {
                if (-not [System.IO.Path]::IsPathRooted($ConfiguredRoot)) {
                    $ConfiguredRoot = Join-Path $ToolRoot $ConfiguredRoot
                }
                $Candidates += $ConfiguredRoot
            }
            break
        }
    }
}
if ($env:ASMR_DUBBER_ROOT) {
    $Candidates += $env:ASMR_DUBBER_ROOT
} elseif ($env:ASMR_NEXT_ROOT) {
    $Candidates += $env:ASMR_NEXT_ROOT
}
$Candidates += (Join-Path (Split-Path -Parent $ToolRoot) "ASMR-Dubber")
$Candidates += (Join-Path (Split-Path -Parent $ToolRoot) "asmr-next")
$AsmrRoot = $Candidates |
    Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
    Select-Object -First 1

if (-not $AsmrRoot) {
    Write-Host "找不到 ASMR Dubber。" -ForegroundColor Red
    Write-Host "请在 settings.txt 中填写 asmr_dubber_path。"
    Write-Host "也可以设置环境变量 ASMR_DUBBER_ROOT。"
    if ($RemainingArguments.Count -eq 0) {
        [void](Read-Host "按 Enter 关闭")
    }
    exit 1
}
$AsmrRoot = (Resolve-Path -LiteralPath $AsmrRoot).Path

$Python = Join-Path $AsmrRoot ".asmr-dubber\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = Join-Path $AsmrRoot ".venv\Scripts\python.exe"
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Host "找不到 ASMR Dubber 的便携 Python：" -ForegroundColor Red
    Write-Host "  $Python"
    Write-Host "请先确认 settings.txt 指向的 ASMR Dubber 可以正常启动。"
    if ($RemainingArguments.Count -eq 0) {
        [void](Read-Host "按 Enter 关闭")
    }
    exit 1
}

& $Python (Join-Path $ToolRoot "asmr_dubber_autoflow.py") @RemainingArguments
$ExitCode = $LASTEXITCODE

if ($RemainingArguments.Count -eq 0) {
    [void](Read-Host "按 Enter 关闭")
}
exit $ExitCode
