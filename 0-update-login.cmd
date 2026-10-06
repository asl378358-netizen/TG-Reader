@echo off
setlocal
if /I "%~1"=="--run" goto run
"%ComSpec%" /d /k ""%~f0" --run"
exit /b
:run
title Telegram Daily Reader - login update
set "tdr_python=%LOCALAPPDATA%\TelegramDailyReader\venv\Scripts\python.exe"
if not exist "%tdr_python%" (
 echo Installed reader environment was not found. Run 0-install.cmd first.
 exit /b 1
)
"%tdr_python%" "%~dp0update-network.py" --login-fix
set "tdr_exit=%errorlevel%"
echo.
echo This window stays open. Close it with the X button when finished.
exit /b %tdr_exit%
