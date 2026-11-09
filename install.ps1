# Games installer for Windows (PowerShell):
#   iwr -useb https://raw.githubusercontent.com/RaphaelA4U/games/main/install.ps1 | iex
#
# Installs into %USERPROFILE%\.games and puts a `games` command on PATH
# (via WindowsApps). Use Windows Terminal for the best experience.
$ErrorActionPreference = "Stop"

$repo = "https://github.com/RaphaelA4U/games.git"
$dir = Join-Path $env:USERPROFILE ".games"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git is required. Install it with: winget install Git.Git"
}
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Error "Python 3.9+ is required. Install it with: winget install Python.Python.3.12"
}
& $py.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) {
    $found = & $py.Source -V
    Write-Error "Python 3.9+ is required (found: $found). Install a newer one: winget install Python.Python.3.12"
}

if (Test-Path (Join-Path $dir ".git")) {
    Write-Host "[games] Existing install found; updating..."
    git -C $dir pull --ff-only
} else {
    Write-Host "[games] Cloning into $dir ..."
    git clone --depth 1 $repo $dir
}

$venvPy = Join-Path $dir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "[games] Creating virtualenv..."
    & $py.Source -m venv (Join-Path $dir ".venv")
}
Write-Host "[games] Installing dependencies..."
& (Join-Path $dir ".venv\Scripts\pip.exe") install -q --disable-pip-version-check -e "$dir"

# WindowsApps is on PATH for every user profile by default.
$bin = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
if (-not (Test-Path $bin)) { New-Item -ItemType Directory -Path $bin | Out-Null }
$cmd = "@echo off`r`n`"$venvPy`" -m games_app %*"
Set-Content -Path (Join-Path $bin "games.cmd") -Value $cmd -Encoding ascii

& $venvPy -m games_app --version
if ($env:GAMES_ONLY -ne "1") {
    Write-Host "[games] Installing every game (each with its own installer)..."
    & $venvPy -m games_app --install-all
}
Write-Host "[games] Done! Open the library with: games   (Windows Terminal recommended)"
