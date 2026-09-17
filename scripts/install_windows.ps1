# Kodgar/YODAW Windows installer (PowerShell 5.1+).
#
# Windows equivalent of scripts/bootstrap.py's unix flow:
#   1. verifies Python 3.12 (py launcher or python on PATH)
#   2. copies the source tree to the install directory
#   3. creates an isolated venv and installs dependencies
#   4. writes a yodaw.cmd wrapper onto the user PATH
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
#   powershell ... -InstallDir D:\yodaw -Skip9Router

param(
    [string]$InstallDir = "$env:USERPROFILE\.yodaw",
    [switch]$Skip9Router,
    [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"

Write-Host "== Kodgar/YODAW Windows installer =="

# 1. Python 3.12 detection ---------------------------------------------
$Py = $null
foreach ($candidate in @("py", "python")) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) {
        if ($candidate -eq "py") {
            $versionOutput = & py -3.12 -c "import sys; print(sys.version)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $versionOutput) {
                $Py = "py -3.12"
                break
            }
        }
        else {
            $versionOutput = & python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $Py = "python"
                break
            }
        }
    }
}
if (-not $Py) {
    Write-Error @"
Python 3.12 is required but was not found.
Install it from https://www.python.org/downloads/ (check 'Add to PATH')
or: winget install Python.Python.3.12
"@
    exit 1
}
Write-Host "python: $Py"

# 2. Locate source ------------------------------------------------------
$SourceDir = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $SourceDir "app"))) {
    Write-Error "source tree not found at $SourceDir (run this script from the repo's scripts directory)"
    exit 1
}

# 3. Copy source --------------------------------------------------------
$AppDir = Join-Path $InstallDir "lib\yodaw"
Write-Host "installing source into $AppDir ..."
if (Test-Path $AppDir) {
    Remove-Item -Recurse -Force $AppDir
}
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$excludeDirs = @(".git", ".github", "workspace", "data", "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build", "htmlcov", "node_modules", "logs", "output")
Get-ChildItem -Path $SourceDir | Where-Object {
    $excludeDirs -notcontains $_.Name
} | ForEach-Object {
    Copy-Item -Recurse -Force $_.FullName -Destination $AppDir
}

# 4. Virtualenv + dependencies ------------------------------------------
$VenvDir = Join-Path $InstallDir "lib\.venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "creating venv at $VenvDir ..."
    if ($Py -eq "py -3.12") {
        & py -3.12 -m venv $VenvDir
    }
    else {
        & python -m venv $VenvDir
    }
}
if (-not (Test-Path $VenvPython)) {
    Write-Error "venv creation failed: $VenvPython missing"
    exit 1
}
if (-not $SkipDeps) {
    Write-Host "installing dependencies ..."
    & $VenvPython -m pip install --quiet --upgrade pip
    & $VenvPython -m pip install --quiet -r (Join-Path $AppDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Error "dependency installation failed"
        exit 1
    }
}

# 5. Zero-touch 9Router stage -------------------------------------------
if (-not $Skip9Router) {
    Write-Host "provisioning 9Router (zero-touch) ..."
    $VarDir = Join-Path $InstallDir "var\yodaw"
    New-Item -ItemType Directory -Force -Path $VarDir | Out-Null
    Push-Location $AppDir
    try {
        & $VenvPython -m app.cli.main setup-9router `
            --path (Join-Path $VarDir "config.toml") `
            --data-dir (Join-Path $InstallDir "var\9router") `
            --no-verify
        if ($LASTEXITCODE -ne 0) {
            Write-Host "warning: 9Router provisioning stage did not complete; run 'yodaw setup-9router' later" -ForegroundColor Yellow
        }
    }
    finally {
        Pop-Location
    }
}

# 6. Wrapper + PATH -----------------------------------------------------
$BinDir = Join-Path $InstallDir "bin"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Wrapper = Join-Path $BinDir "yodaw.cmd"
@"
@echo off
rem Kodgar/YODAW Windows wrapper
set "SCRIPT_DIR=%~dp0"
set "LIB_DIR=%SCRIPT_DIR%..\lib"
set "VENV_PYTHON=%LIB_DIR%\.venv\Scripts\python.exe"
set "APP_DIR=%LIB_DIR%\yodaw"

if not exist "%VENV_PYTHON%" (
    echo Error: YODAW virtual environment not found. Please reinstall. 1>&2
    exit /b 1
)

set "BOOTSTRAP_CONFIG=%SCRIPT_DIR%..\var\yodaw\config.toml"
if "%YODAW_CONFIG%"=="" if exist "%BOOTSTRAP_CONFIG%" set "YODAW_CONFIG=%BOOTSTRAP_CONFIG%"
set "BOOTSTRAP_9ROUTER_DATA=%SCRIPT_DIR%..\var\9router"
if "%DATA_DIR%"=="" if exist "%BOOTSTRAP_9ROUTER_DATA%" set "DATA_DIR=%BOOTSTRAP_9ROUTER_DATA%"

cd /d "%APP_DIR%"
"%VENV_PYTHON%" -m app.cli.main %*
"@ | Out-File -FilePath $Wrapper -Encoding ascii

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$BinDir", "User")
    Write-Host "added $BinDir to the user PATH (new shells only)"
}

Write-Host "install:   $InstallDir"
Write-Host "launcher:  $Wrapper"
Write-Host "done. try: $Wrapper kodgar-doctor"
