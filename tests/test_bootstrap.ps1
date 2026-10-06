param([Parameter(Mandatory=$true)][string]$TestPython)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
. (Join-Path $repo 'install.ps1') -FunctionsOnly
$passed = 0
function Assert-True {
    param([bool]$Value, [string]$Name)
    if (-not $Value) { throw ('FAIL: ' + $Name) }
    $script:passed++
    Write-Host ('PASS: ' + $Name)
}
$temp = Join-Path ([IO.Path]::GetTempPath()) ('TG Reader test ' + [guid]::NewGuid().ToString('N'))
$previousLocal = $env:LOCALAPPDATA
$previousLaunch = $env:TGR_SETUP_NO_LAUNCH
$previousShortcut = $env:TGR_SETUP_SHORTCUT_DIR
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $env:LOCALAPPDATA = Join-Path $temp 'Local AppData'
    $env:TGR_SETUP_NO_LAUNCH = '1'
    $env:TGR_SETUP_SHORTCUT_DIR = Join-Path $temp 'Desktop with spaces'
    $root = Join-Path $env:LOCALAPPDATA 'TelegramDailyReader'
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $result = Invoke-NativeProbe -Executable $TestPython -Arguments @('-m', 'venv', '--system-site-packages', (Join-Path $root 'venv')) -TimeoutMilliseconds 120000
    Assert-True ($result.ExitCode -eq 0) 'Existing application environment prepared'
    $marker = Join-Path $root 'venv\existing-environment.marker'
    [IO.File]::WriteAllText($marker, 'keep this environment')
    $preserved = @{'config.json'='{"groups":[]}'; 'reader.sqlite3'='original database'; '.auth.enc'='original encrypted session'}
    foreach ($item in $preserved.GetEnumerator()) { [IO.File]::WriteAllText((Join-Path $root $item.Key), $item.Value) }
    $setup = Join-Path $temp 'Setup with spaces.cmd'
    Copy-Item -LiteralPath (Join-Path $repo 'TG-Reader-Setup.cmd') -Destination $setup
    $text = [IO.File]::ReadAllText($setup, [Text.Encoding]::UTF8)
    $begin = $text.LastIndexOf('# BEGIN POWERSHELL')
    $end = $text.LastIndexOf('# END POWERSHELL / PACKAGE')
    $tokens = $null; $errors = $null
    [System.Management.Automation.Language.Parser]::ParseInput($text.Substring($begin, $end-$begin), [ref]$tokens, [ref]$errors) | Out-Null
    Assert-True ($errors.Count -eq 0) 'Embedded script parses in Windows PowerShell 5.1'
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        # cmd.exe needs its own quoting layer, including executable paths with spaces.
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = $env:COMSPEC
        $info.Arguments = '/d /c ""' + $setup + '""'
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $info.RedirectStandardInput = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $info
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(180000)) { $process.Kill(); throw 'Setup timeout' }
        Write-Host $stdout.Result
        Write-Host $stderr.Result
        Assert-True ($process.ExitCode -eq 0) ('Real CMD setup succeeds, attempt ' + $attempt)
        $record = Get-Content -LiteralPath (Join-Path $root 'current.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $source = Join-Path (Join-Path $root 'versions') $record.sha
        Assert-True ((Test-Path -LiteralPath (Join-Path $source 'app.py')) -and (Test-Path -LiteralPath (Join-Path $root 'start.py'))) 'Installed version and permanent launcher exist'
        Assert-True ((Get-Content -LiteralPath $marker -Raw) -ceq 'keep this environment') 'Existing Python environment reused'
        foreach ($item in $preserved.GetEnumerator()) {
            Assert-True (([IO.File]::ReadAllText((Join-Path $root $item.Key))) -ceq $item.Value) ('Preserved ' + $item.Key)
        }
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut((Join-Path $env:TGR_SETUP_SHORTCUT_DIR 'TG Reader.lnk'))
        Assert-True ($link.TargetPath -eq (Join-Path $root 'venv\Scripts\pythonw.exe') -and $link.Arguments -eq ('"' + (Join-Path $root 'start.py') + '"')) 'Permanent shortcut targets installed Python with correctly quoted launcher'
        Assert-True (Test-Path -LiteralPath (Join-Path $root 'TG-Reader-Setup.cmd')) 'Recovery installer retained locally'
        Assert-True (@(Get-ChildItem -LiteralPath $root -Directory -Filter 'setup_*').Count -eq 0) 'Temporary extraction removed'
    }
    Write-Host ("Bootstrap tests: $passed passed")
} finally {
    $env:LOCALAPPDATA = $previousLocal
    $env:TGR_SETUP_NO_LAUNCH = $previousLaunch
    $env:TGR_SETUP_SHORTCUT_DIR = $previousShortcut
    Remove-Item -LiteralPath $temp -Recurse -Force
}
