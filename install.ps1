# Decision graph installer for Windows.
#
#   irm https://raw.githubusercontent.com/DEMONKINGKAI/neuro-graph/main/install.ps1 | iex
#
# or, from a local copy:   powershell -ExecutionPolicy Bypass -File install.ps1 -Source C:\path\to\neuro-graph
#
# Checks Claude Code, Python and Git for Windows (offers to install what's missing), installs the plugin from its marketplace,
# installs the `dg` command and runs a health check. Safe to run again: it updates an existing install.
param([string]$Source = "DEMONKINGKAI/neuro-graph")

$ErrorActionPreference = "Stop"
function Step($t) { Write-Host "`n$t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  ok  $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  !!  $t" -ForegroundColor Yellow }
function RefreshPath { $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") }
function Ask($q) { $a = Read-Host "$q [Y/n]"; return ($a -eq "" -or $a -match "^(y|yes)$") }

Write-Host "Installing decision-graph" -ForegroundColor White

Step "1. Claude Code"
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
  Warn "Claude Code isn't installed."
  if (Ask "     Install it now?") { irm https://claude.ai/install.ps1 | iex; RefreshPath; $env:Path += ";$env:USERPROFILE\.local\bin" }
  if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Write-Host "Install Claude Code first: https://code.claude.com/docs/en/setup"; exit 1 }
}
Ok ("claude " + (claude --version))

Step "2. Python"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py -or $py.Source -like "*WindowsApps*") {
  Warn "Python is missing (or only the Microsoft Store placeholder is there)."
  if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "     Install Python 3.12 with winget?")) {
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    RefreshPath
    $py = Get-Command python -ErrorAction SilentlyContinue
  }
  if (-not $py -or $py.Source -like "*WindowsApps*") {
    Write-Host "Install Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."; exit 1
  }
}
Ok ("python at " + $py.Source)

Step "3. Git for Windows (the plugin's hooks run through Git Bash)"
$git = Get-Command git -ErrorAction SilentlyContinue
$bash = $null
if ($git) { $bash = Join-Path (Split-Path (Split-Path $git.Source)) "bin\bash.exe" }
if (-not $bash -or -not (Test-Path $bash)) {
  foreach ($r in @("$env:ProgramFiles\Git", "$env:LOCALAPPDATA\Programs\Git")) { if (Test-Path "$r\bin\bash.exe") { $bash = "$r\bin\bash.exe" } }
}
if (-not $bash -or -not (Test-Path $bash)) {
  Warn "Git for Windows isn't installed."
  if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "     Install it with winget?")) {
    winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
    RefreshPath
  } else {
    Write-Host "Install it from https://git-scm.com/download/win, then run this again."; exit 1
  }
} else { Ok ("Git Bash at " + $bash) }

Step "4. Plugin"
$ErrorActionPreference = "Continue"
claude plugin marketplace add "$Source" 2>&1 | Out-Null
claude plugin marketplace update neuro-graph 2>&1 | Out-Null
$installed = (claude plugin list --json | ConvertFrom-Json) | Where-Object { $_.id -like "decision-graph@*" }
if ($installed) { claude plugin update decision-graph@neuro-graph | Out-Host } else { claude plugin install decision-graph@neuro-graph | Out-Host }
$entry = (claude plugin list --json | ConvertFrom-Json) | Where-Object { $_.id -like "decision-graph@*" } | Select-Object -First 1
if (-not $entry) { Write-Host "The plugin didn't install. Try: claude plugin install decision-graph@neuro-graph"; exit 1 }
Ok ("decision-graph " + $entry.version)

Step "5. Setup"
python "$($entry.installPath)\scripts\dg.py" setup --yes
Write-Host "`nDone. Open a new terminal for the dg command, then try:  dg demo" -ForegroundColor White
