@echo off
setlocal
if /I "%~1"=="--run" goto run
"%ComSpec%" /d /k ""%~f0" --run"
exit /b
:run
title Telegram Daily Reader - Python check
if not exist "%~dp0install.ps1" (
    echo Missing install.ps1. Extract the whole ZIP first.
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -DiagnoseOnly
set "tdr_exit=%errorlevel%"
echo.
if not "%tdr_exit%"=="0" echo Python check failed. The error is above; details are saved in python-check.log.
echo This window stays open. Close it with the X button when finished.
exit /b %tdr_exit%
