param([ValidateSet('enable','disable')][string]$Mode = 'enable')
$ErrorActionPreference = 'Stop'
$taskName = 'TelegramDailyReader'
$root = Join-Path $env:LOCALAPPDATA 'TelegramDailyReader'
if ($Mode -eq 'disable') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Write-Host 'Automatic collection disabled. Saved messages and files remain available.'
    exit 0
}
if (-not (Test-Path (Join-Path $root 'config.json'))) {
    throw 'Run 1-desktop-setup.cmd first.'
}
$config = Get-Content (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($config.auth_mode -eq 'desktop') {
    if ($config.auth_file -notmatch '^desktop_[0-9a-f]{32}\.auth\.enc$') { throw 'Invalid local auth filename.' }
    $authPath = Join-Path $root $config.auth_file
} else {
    $authPath = Join-Path $root 'account.session.enc'
}
if (-not (Test-Path $authPath)) { throw 'Run 1-desktop-setup.cmd first.' }
$statusPath = Join-Path $config.output_dir 'latest_status.json'
if (-not (Test-Path $statusPath)) { throw 'Run 2-collect-now.cmd and verify the first collection before enabling automatic collection.' }
$status = Get-Content $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (@($status.chats | Where-Object { $_.collection_error -or -not $_.collection_ok_utc }).Count -gt 0) {
    throw 'The first collection contains an error. Open 5-status.cmd, fix it, and retry 2-collect-now.cmd.'
}
$python = Join-Path $root 'venv\Scripts\pythonw.exe'
$app = Join-Path $root 'start.py'
if (-not (Test-Path $app)) { throw 'Install TG Reader using TG-Reader-Setup.cmd first.' }
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + $app + '" --collect') -WorkingDirectory $root
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30)
$logon = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($repeat,$logon) -Principal $principal -Settings $settings -Description 'Read selected Telegram groups, process media locally, publish daily packets to a selected Google Drive folder.' -Force | Out-Null
Write-Host 'Automatic collection enabled: every 30 minutes and at Windows sign-in.'
Write-Host 'It runs while you are signed in to Windows and the computer is on. No administrator account is requested.'
