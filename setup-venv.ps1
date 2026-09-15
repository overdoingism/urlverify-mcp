#Requires -Version 5.1
<#
.SYNOPSIS
    Rebuild the project virtual environment (.venv) on Windows.

.DESCRIPTION
    Prefers uv when available (uses uv.lock for reproducible installs); otherwise falls back to
    system python >= 3.11 with venv + pip. The existing .venv is moved aside as a backup first,
    unless -Force is given.

.EXAMPLE
    .\setup-venv.ps1
.EXAMPLE
    .\setup-venv.ps1 -Force     # delete the old .venv instead of backing it up
#>
param([switch]$Force)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Say([string]$msg) { [Console]::Error.WriteLine("==> $msg") }

if (Test-Path ".venv") {
    if ($Force) {
        Remove-Item -LiteralPath ".venv" -Recurse -Force
        Say "removed existing .venv (-Force)"
    } else {
        $bak = ".venv.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        Rename-Item -LiteralPath ".venv" -NewName $bak
        Say "moved existing .venv to $bak"
    }
}

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCmd) {
    Say "using uv: $($uvCmd.Source)"
    & $uvCmd.Source sync --extra dev
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
} else {
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pyCmd) { throw "'python' not found in PATH; install Python >= 3.11 (or uv)" }
    $verLine = (& $pyCmd.Source --version 2>&1 | Out-String).Trim()
    Say "using $verLine at $($pyCmd.Source)"
    if ($verLine -notmatch '(\d+)\.(\d+)') { throw "cannot parse python version: $verLine" }
    $maj = [int]$Matches[1]; $min = [int]$Matches[2]
    if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 11)) { throw "$verLine is too old; need Python >= 3.11" }
    Say "creating .venv and installing package + dev extras (pip)"
    & $pyCmd.Source -m venv ".venv"
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed" }
    & ".venv\Scripts\python.exe" -m pip install --quiet -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "expected .venv\Scripts\python.exe not found after setup" }
Say "done: .venv is ready"
