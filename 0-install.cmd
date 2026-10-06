@echo off
setlocal
if /I "%~1"=="--run" goto run
"%ComSpec%" /d /k ""%~f0" --run"
exit /b
:run
title Telegram Daily Reader - installation
if not exist "%~dp0install.ps1" (
    echo Missing install.ps1. Extract the whole ZIP first.
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "tdr_exit=%errorlevel%"
echo.
if not "%tdr_exit%"=="0" echo Installation failed. The error is above; details are saved in install.log.
echo This window stays open. Close it with the X button when finished.
exit /b %tdr_exit%
