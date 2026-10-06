#requires -Version 5.1
param([switch]$FunctionsOnly, [switch]$DiagnoseOnly, [string]$PythonPath = '', [switch]$EnvironmentOnly)

$ErrorActionPreference = 'Stop'
$script:InstallLog = $null
$script:InstallerDirectory = $PSScriptRoot

function Write-InstallMessage {
    param([string]$Message)
    Write-Host $Message
    if ($script:InstallLog) {
        Add-Content -LiteralPath $script:InstallLog -Value $Message -Encoding UTF8 -ErrorAction Stop
    }
}

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Argument)
    $quoted = [regex]::Replace($Argument, '(\\*)"', '$1$1\"')
    $quoted = [regex]::Replace($quoted, '(\\+)$', '$1$1')
    return '"' + $quoted + '"'
}

function Invoke-NativeProbe {
    param([string]$Executable, [string[]]$Arguments, [int]$TimeoutMilliseconds = 30000)
    $process = New-Object System.Diagnostics.Process
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $info.Arguments = (($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { throw 'Process did not start.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            $process.Kill()
            $process.WaitForExit()
            return [pscustomobject]@{ ExitCode = 124; Stdout = ''; Stderr = 'Probe timed out.' }
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Stdout = $stdout.GetAwaiter().GetResult()
            Stderr = $stderr.GetAwaiter().GetResult()
        }
    } catch {
        return [pscustomobject]@{ ExitCode = -1; Stdout = ''; Stderr = $_.Exception.Message }
    } finally {
        $process.Dispose()
    }
}

function Get-PythonInfo {
    param([string]$Executable)
    $code = @'
import sys, struct, json, sysconfig
value = {'executable': sys.executable, 'version': sys.version.split()[0], 'major': sys.version_info.major, 'minor': sys.version_info.minor, 'bits': struct.calcsize('P') * 8, 'implementation': sys.implementation.name, 'free_threaded': bool(sysconfig.get_config_var('Py_GIL_DISABLED')), 'tk': False, 'venv': False, 'error': ''}
try:
    import tkinter, venv, ensurepip
    value['tk'] = True
    value['venv'] = True
except Exception as error:
    value['error'] = str(error)
print(json.dumps(value))
'@
    $result = Invoke-NativeProbe -Executable $Executable -Arguments @('-c', $code)
    if ($result.ExitCode -ne 0) {
        return [pscustomobject]@{ Executable=$Executable; Version='unknown'; Major=0; Minor=0; Compatible=$false; Reason=('Cannot start: ' + $result.Stderr.Trim()) }
    }
    try {
        $value = ConvertFrom-Json -InputObject $result.Stdout -ErrorAction Stop
        $reason = ''
        if ($value.implementation -ne 'cpython') { $reason = 'CPython is required' }
        elseif ($value.major -ne 3 -or $value.minor -lt 10 -or $value.minor -gt 14) { $reason = 'Supported versions: Python 3.10-3.14' }
        elseif ($value.bits -ne 64) { $reason = '64-bit Python is required' }
        elseif ($value.free_threaded) { $reason = 'Free-threaded Python is not supported by all dependencies' }
        elseif (-not $value.tk -or -not $value.venv) { $reason = 'Missing tkinter/venv/ensurepip: ' + $value.error }
        return [pscustomobject]@{ Executable=[string]$value.executable; Version=[string]$value.version; Major=[int]$value.major; Minor=[int]$value.minor; Compatible=($reason -eq ''); Reason=$reason }
    } catch {
        return [pscustomobject]@{ Executable=$Executable; Version='unknown'; Major=0; Minor=0; Compatible=$false; Reason='Python returned an invalid diagnostic response' }
    }
}

function ConvertFrom-LauncherPaths {
    param([string]$Output)
    foreach ($match in [regex]::Matches($Output, '(?im)(?:[A-Z]:\\|\\\\)[^\r\n]*?python(?:[0-9.]+)?\.exe')) {
        $match.Value.Trim('"')
    }
}

function Get-PythonCandidates {
    param([string]$Root, [string]$ExplicitPath = '')
    $paths = New-Object 'System.Collections.Generic.List[string]'
    if ($ExplicitPath) { $paths.Add($ExplicitPath) }
    $paths.Add((Join-Path $Root 'venv\Scripts\python.exe'))
    foreach ($name in @('python.exe','python3.exe','python')) {
        foreach ($command in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($command.Source) { $paths.Add($command.Source) }
        }
    }
    foreach ($launcher in @(Get-Command py.exe -All -ErrorAction SilentlyContinue)) {
        if (-not $launcher.Source) { continue }
        $listed = Invoke-NativeProbe -Executable $launcher.Source -Arguments @('-0p')
        foreach ($path in @(ConvertFrom-LauncherPaths ($listed.Stdout + "`n" + $listed.Stderr))) { $paths.Add($path) }
    }
    if ($env:OS -eq 'Windows_NT') {
        foreach ($base in @('HKCU:\Software\Python','HKLM:\Software\Python','HKLM:\Software\WOW6432Node\Python')) {
            foreach ($company in @(Get-ChildItem -LiteralPath $base -ErrorAction SilentlyContinue)) {
                foreach ($tag in @(Get-ChildItem -LiteralPath $company.PSPath -ErrorAction SilentlyContinue)) {
                    $key = Get-Item -LiteralPath (Join-Path $tag.PSPath 'InstallPath') -ErrorAction SilentlyContinue
                    if ($key) {
                        $exe = $key.GetValue('ExecutablePath')
                        if ($exe) { $paths.Add([string]$exe) }
                        $directory = $key.GetValue('')
                        if ($directory) { $paths.Add((Join-Path $directory 'python.exe')) }
                    }
                }
            }
        }
        $patterns = @()
        if ($env:LOCALAPPDATA) {
            $patterns += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe'
            $patterns += Join-Path $env:LOCALAPPDATA 'Python\pythoncore-*\python.exe'
            $patterns += Join-Path $env:LOCALAPPDATA 'uv\python\*\python.exe'
            $patterns += Join-Path $env:LOCALAPPDATA 'uv\python\*\install\python.exe'
        }
        if ($env:ProgramFiles) { $patterns += Join-Path $env:ProgramFiles 'Python*\python.exe' }
        if ($env:USERPROFILE) {
            foreach ($pattern in @('miniconda3\python.exe','anaconda3\python.exe','miniconda3\envs\*\python.exe','anaconda3\envs\*\python.exe','.pyenv\pyenv-win\versions\*\python.exe','scoop\apps\python*\current\python.exe')) {
                $patterns += Join-Path $env:USERPROFILE $pattern
            }
        }
        if ($env:CONDA_PREFIX) { $paths.Add((Join-Path $env:CONDA_PREFIX 'python.exe')) }
        if ($env:UV_PYTHON_INSTALL_DIR) { $patterns += Join-Path $env:UV_PYTHON_INSTALL_DIR '*\python.exe' }
        $patterns += 'C:\Python*\python.exe'
        foreach ($pattern in $patterns) {
            foreach ($file in @(Get-Item -Path $pattern -ErrorAction SilentlyContinue)) {
                if (-not $file.PSIsContainer) { $paths.Add($file.FullName) }
            }
        }
    }
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($path in $paths) {
        if ($path -match '(?i)\\Microsoft\\WindowsApps\\(?:python|python3)\.exe$') { continue }
        if ((Test-Path -LiteralPath $path -PathType Leaf) -and $seen.Add($path)) { $path }
    }
}

function Get-PythonInventory {
    param([string[]]$Candidates)
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($candidate in $Candidates) {
        $value = Get-PythonInfo -Executable $candidate
        if ($seen.Add($value.Executable)) { $value }
    }
}

function Show-PythonInventory {
    param([object[]]$Inventory)
    Write-InstallMessage 'Installed Python interpreters (no Python will be downloaded):'
    if (-not $Inventory) { Write-InstallMessage 'No executable was found in PATH, launcher, registry or standard locations.' }
    foreach ($item in $Inventory) {
        $status = if ($item.Compatible) { 'COMPATIBLE' } else { $item.Reason }
        Write-InstallMessage ('Python ' + $item.Version + ' | ' + $status)
        Write-InstallMessage ('  ' + $item.Executable)
    }
}

function Select-ExistingPython {
    param([object[]]$Inventory, [string]$ExplicitPath = '')
    if ($ExplicitPath) {
        $selected = @($Inventory | Where-Object { $_.Executable -ieq $ExplicitPath -and $_.Compatible })
        if (-not $selected) { throw 'The supplied PythonPath is not usable. See the diagnostic list above.' }
        return $selected[0]
    }
    foreach ($item in $Inventory) { if ($item.Compatible) { return $item } }
    throw 'No compatible existing Python was found. Nothing was downloaded. Run 0-check-python.cmd and send python-check.log; a custom path can be supplied with -PythonPath.'
}

function Invoke-LoggedNative {
    param([string]$Executable, [string[]]$Arguments)
    # Use explicit CRT quoting: PowerShell 5.1 rewrites quotes in native arguments.
    $result = Invoke-NativeProbe -Executable $Executable -Arguments $Arguments -TimeoutMilliseconds 1200000
    foreach ($stream in @($result.Stdout, $result.Stderr)) {
        foreach ($line in ($stream -split "`r?`n")) {
            if ($line.Length) { Write-InstallMessage $line }
        }
    }
    if ($result.ExitCode -ne 0) { throw ("Command failed with exit code " + $result.ExitCode + ". See install.log.") }
}

function Install-Reader {
    param([string]$Root, [string]$Source, [switch]$EnvironmentOnly)
    if ($env:OS -ne 'Windows_NT') { throw 'This installer requires Windows 10/11 x64.' }
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    Write-InstallMessage '[1/5] Looking for already installed Python...'
    $candidates = @(Get-PythonCandidates -Root $Root -ExplicitPath $PythonPath)
    $inventory = @(Get-PythonInventory -Candidates $candidates)
    Show-PythonInventory -Inventory $inventory
    $selected = Select-ExistingPython -Inventory $inventory -ExplicitPath $PythonPath
    $python = $selected.Executable
    Write-InstallMessage ('[2/5] Using existing Python ' + $selected.Version + ': ' + $python)

    Write-InstallMessage '[3/5] Copying the application and preparing its environment...'
    $appDir = Join-Path $Root 'app'
    $readerDir = Join-Path $appDir 'tgreader'
    if (-not $EnvironmentOnly) {
        New-Item -ItemType Directory -Force -Path $readerDir | Out-Null
        foreach ($item in Get-ChildItem -LiteralPath (Join-Path $Source 'tgreader')) {
            Copy-Item -LiteralPath $item.FullName -Destination $readerDir -Recurse -Force
        }
        foreach ($name in @('app.py','requirements.txt','schedule.ps1','README.html','AGENT_README.txt')) {
            Copy-Item -LiteralPath (Join-Path $Source $name) -Destination $appDir -Force
        }
    }
    $venv = Join-Path $Root 'venv'
    $runner = Join-Path $venv 'Scripts\python.exe'
    $existing = if (Test-Path -LiteralPath $runner) { Get-PythonInfo -Executable $runner } else { $null }
    if (-not $existing -or -not $existing.Compatible -or $existing.Major -ne $selected.Major -or $existing.Minor -ne $selected.Minor) { Invoke-LoggedNative -Executable $python -Arguments @('-m','venv','--clear',$venv) }
    Invoke-LoggedNative -Executable $runner -Arguments @('-m','ensurepip','--upgrade')

    Write-InstallMessage '[4/5] Installing dependencies. This may take several minutes...'
    Invoke-LoggedNative -Executable $runner -Arguments @('-m','pip','install','--disable-pip-version-check','-r',(Join-Path $Source 'requirements.txt'))
    Write-InstallMessage '[5/5] Checking dependencies and Windows libraries...'
    Invoke-LoggedNative -Executable $runner -Arguments @('-m','pip','check')
    $check = "import os; os.environ['OPENTELE_NO_FETCH']='1'; import tkinter,telethon,opentele2,av,PIL,faster_whisper,python_socks; print('Application libraries: OK')"
    Invoke-LoggedNative -Executable $runner -Arguments @('-c',$check)
    Write-InstallMessage ''
    Write-InstallMessage 'Installation complete. Use the TG Reader desktop shortcut.'
    Write-InstallMessage 'The local speech model downloads when the first voice message is processed.'
}

if ($FunctionsOnly) { return }
$resultCode = 1
try {
    $script:InstallLog = Join-Path $script:InstallerDirectory $(if ($DiagnoseOnly) { 'python-check.log' } else { 'install.log' })
    try {
        Set-Content -LiteralPath $script:InstallLog -Value ('Telegram Daily Reader installer hotfix 2 - ' + (Get-Date).ToString('s')) -Encoding UTF8 -ErrorAction Stop
    } catch {
        $logDir = Join-Path $env:LOCALAPPDATA 'TelegramDailyReader'
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        $script:InstallLog = Join-Path $logDir $(if ($DiagnoseOnly) { 'python-check.log' } else { 'install.log' })
        Set-Content -LiteralPath $script:InstallLog -Value 'Telegram Daily Reader installer hotfix 2' -Encoding UTF8
    }
    if ($PythonPath) { $PythonPath = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).ProviderPath }
    $root = Join-Path $env:LOCALAPPDATA 'TelegramDailyReader'
    Write-InstallMessage ('Installation log: ' + $script:InstallLog)
    if ($DiagnoseOnly) {
        $inventory = @(Get-PythonInventory -Candidates @(Get-PythonCandidates -Root $root -ExplicitPath $PythonPath))
        Show-PythonInventory -Inventory $inventory
        Write-InstallMessage ('Diagnostic log: ' + $script:InstallLog)
    } else { Install-Reader -Root $root -Source $script:InstallerDirectory -EnvironmentOnly:$EnvironmentOnly }
    $resultCode = 0
} catch {
    Write-InstallMessage ''
    Write-InstallMessage ('INSTALLATION FAILED: ' + $_.Exception.Message)
    if ($_.ScriptStackTrace) { Write-InstallMessage $_.ScriptStackTrace }
    Write-InstallMessage ('Log saved: ' + $script:InstallLog)
}
exit $resultCode
