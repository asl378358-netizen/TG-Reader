@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\TelegramDailyReader\app\schedule.ps1" -Mode enable
pause
