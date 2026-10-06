$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'TelegramDailyReader'
New-Item -ItemType Directory -Force -Path $root | Out-Null
$setupLog = Join-Path $root 'setup.log'
Start-Transcript -LiteralPath $setupLog -Force | Out-Null
$temporary = Join-Path $root ('setup_' + [guid]::NewGuid().ToString('N'))
try {
    if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) { throw 'Требуется Windows 10/11 x64.' }
    Write-Host 'TG Reader: установка ярлыка и автоматического обновления.'
    Write-Host 'Новый Python не скачивается. Данные предыдущей установки сохраняются.'
    $text = [IO.File]::ReadAllText($env:TGR_SETUP_FILE, [Text.Encoding]::UTF8)
    $marker = '# END' + ' POWERSHELL / PACKAGE'
    $position = $text.LastIndexOf($marker)
    if ($position -lt 0) { throw 'Установочный файл неполный.' }
    $bytes = [Convert]::FromBase64String($text.Substring($position + $marker.Length).Trim())
    $hash = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    if ($hash -ne '__PACKAGE_SHA256__') { throw 'Проверка установочного файла не прошла. Скачайте его заново.' }
    New-Item -ItemType Directory -Force -Path $temporary | Out-Null
    $zip = Join-Path $temporary 'bundle.zip'
    [IO.File]::WriteAllBytes($zip, $bytes)
    Expand-Archive -LiteralPath $zip -DestinationPath $temporary -Force
    $source = Join-Path $temporary 'TG-Reader'
    $manifest = Get-Content -LiteralPath (Join-Path $source 'update-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 1) { throw 'Неизвестный формат установочного файла.' }
    foreach ($property in $manifest.files.PSObject.Properties) {
        $name = $property.Name
        if ($name -match '(^/|\\|:|(^|/)\.\.(/|$))') { throw 'Недопустимый путь в установочном файле.' }
        $file = Join-Path $source $name
        $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $property.Value.sha256 -or (Get-Item -LiteralPath $file).Length -ne $property.Value.bytes) { throw 'Проверка файлов программы не прошла.' }
    }
    . (Join-Path $source 'install.ps1') -FunctionsOnly
    $script:InstallLog = $null
    $python = Join-Path $root 'venv\Scripts\python.exe'
    $check = "import sys; sys.path.insert(0, sys.argv[1]); from update_client import verify_runtime; verify_runtime(sys.argv[1], sys.executable); print('Existing environment: OK')"
    $result = if (Test-Path -LiteralPath $python) { Invoke-NativeProbe -Executable $python -Arguments @('-c', $check, $source) -TimeoutMilliseconds 180000 } else { $null }
    if (-not $result -or $result.ExitCode -ne 0) {
        Write-Host 'Подготавливаю зависимости в окружении программы...'
        Install-Reader -Root $root -Source $source -EnvironmentOnly
        $result = Invoke-NativeProbe -Executable $python -Arguments @('-c', $check, $source) -TimeoutMilliseconds 180000
        if ($result.ExitCode -ne 0) { throw ('Проверка программы не прошла: ' + $result.Stderr) }
    } else { Write-Host 'Используется ранее установленное окружение Python.' }
    $bundleId = 'bundled-' + $hash.Substring(0,16)
    $versions = Join-Path $root 'versions'
    New-Item -ItemType Directory -Force -Path $versions | Out-Null
    $destination = Join-Path $versions $bundleId
    if (-not (Test-Path -LiteralPath $destination)) { Move-Item -LiteralPath $source -Destination $destination }
    $record = @{sha=$bundleId; version=$manifest.version; python=$python; requirements_sha256=$manifest.files.'requirements.txt'.sha256}
    $pointer = Join-Path $root 'current.json'
    $starter = Join-Path $root 'start.py'
    if (Test-Path -LiteralPath $pointer) { Copy-Item -LiteralPath $pointer -Destination (Join-Path $root 'current.before-setup.json') -Force }
    if (Test-Path -LiteralPath $starter) { Copy-Item -LiteralPath $starter -Destination (Join-Path $root 'start.before-setup.py') -Force }
    Copy-Item -LiteralPath (Join-Path $destination 'launcher.py') -Destination $starter -Force
    [IO.File]::WriteAllText($pointer, ($record | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    $shell = New-Object -ComObject WScript.Shell
    $shortcutFolders = if ($env:TGR_SETUP_SHORTCUT_DIR) { @($env:TGR_SETUP_SHORTCUT_DIR) } else { @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs')) }
    foreach ($folder in $shortcutFolders) {
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        $link = $shell.CreateShortcut((Join-Path $folder 'TG Reader.lnk'))
        $link.TargetPath = Join-Path $root 'venv\Scripts\pythonw.exe'
        $link.Arguments = '"' + $starter + '"'
        $link.WorkingDirectory = $root
        $link.Description = 'Telegram: группы, медиа и автоматические обновления'
        $link.Save()
    }
    $savedSetup = Join-Path $root 'TG-Reader-Setup.cmd'
    if ([IO.Path]::GetFullPath($env:TGR_SETUP_FILE) -ne [IO.Path]::GetFullPath($savedSetup)) { Copy-Item -LiteralPath $env:TGR_SETUP_FILE -Destination $savedSetup -Force }
    Write-Host 'Готово. Запускайте TG Reader по ярлыку на рабочем столе.'
    if ($env:TGR_SETUP_NO_LAUNCH -ne '1') { Start-Process -FilePath (Join-Path $root 'venv\Scripts\pythonw.exe') -ArgumentList ('"' + $starter + '"') -WorkingDirectory $root }
    $resultCode = 0
} catch {
    Write-Host ('Ошибка установки: ' + $_.Exception.Message)
    Write-Host ('Журнал: ' + $setupLog)
    $resultCode = 1
} finally {
    Stop-Transcript | Out-Null
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
}
exit $resultCode
