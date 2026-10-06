@echo off
setlocal
if /I "%~1"=="--run" goto run
"%ComSpec%" /d /k ""%~f0" --run"
exit /b
:run
title Telegram Daily Reader - Telegram setup
"%LOCALAPPDATA%\TelegramDailyReader\venv\Scripts\python.exe" "%LOCALAPPDATA%\TelegramDailyReader\app\app.py" setup-desktop
set "tdr_exit=%errorlevel%"
echo.
echo This window stays open. Close it with the X button when finished.
exit /b %tdr_exit%
