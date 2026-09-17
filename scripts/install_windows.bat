@echo off
rem Kodgar/YODAW Windows installer shim.
rem Bootstraps PowerShell (present by default on Windows 7+) and runs
rem the real installer. Usage:
rem   install_windows.bat            (default install)
rem   install_windows.bat -Skip9Router
rem   install_windows.bat -InstallDir D:\yodaw

setlocal
set "SCRIPT_DIR=%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
    echo Error: Windows PowerShell is required but was not found on PATH. 1>&2
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install_windows.ps1" %*
exit /b %errorlevel%
