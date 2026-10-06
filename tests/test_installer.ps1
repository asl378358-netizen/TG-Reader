# Run: pwsh -NoProfile -File tests/test_installer.ps1 -TestPython <existing python path>
param([Parameter(Mandatory=$true)][string]$TestPython)
$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'install.ps1') -FunctionsOnly
$passed = 0
function Assert-True {
    param([bool]$Value, [string]$Name)
    if (-not $Value) { throw ('FAIL: ' + $Name) }
    $script:passed++
    Write-Host ('PASS: ' + $Name)
}
$temp = Join-Path ([IO.Path]::GetTempPath()) ('reader-installer-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $result = Invoke-NativeProbe -Executable $TestPython -Arguments @('-c', 'import sys; print("No suitable Python runtime found",file=sys.stderr); sys.exit(103)')
    Assert-True ($result.ExitCode -eq 103 -and $result.Stderr.Contains('No suitable Python runtime found')) 'Missing runtime is a normal probe result under ErrorAction Stop'
    $result = Invoke-NativeProbe -Executable (Join-Path $temp 'missing-python.exe') -Arguments @('-c','pass')
    Assert-True ($result.ExitCode -eq -1) 'Missing executable does not terminate discovery'
    $arguments = @('C:\Users\Fullservis TG\Desktop\reader', 'quote " inside', 'C:\ending\', '')
    $result = Invoke-NativeProbe -Executable $TestPython -Arguments (@('-c','import sys,json; print(json.dumps(sys.argv[1:]))') + $arguments)
    $actual = @(ConvertFrom-Json $result.Stdout)
    Assert-True ($result.ExitCode -eq 0 -and $actual.Count -eq 4 -and $actual[0] -ceq $arguments[0] -and $actual[1] -ceq $arguments[1] -and $actual[2] -ceq $arguments[2] -and $actual[3] -ceq '') 'Arguments with spaces, quotes, trailing slash and empty value survive'
    $result = Invoke-NativeProbe -Executable $TestPython -Arguments @('-c','import time; time.sleep(2)') -TimeoutMilliseconds 50
    Assert-True ($result.ExitCode -eq 124) 'Hung discovery process times out'
    $info = Get-PythonInfo -Executable $TestPython
    Assert-True ($info.Compatible -and $info.Executable -and $info.Major -eq 3) 'Existing compatible Python is detected'
    $inventory = @(Get-PythonInventory -Candidates @($TestPython, $TestPython))
    Assert-True ($inventory.Count -eq 1) 'Duplicate interpreter paths are removed'
    $selected = Select-ExistingPython -Inventory $inventory
    Assert-True ($selected.Executable -ceq $info.Executable) 'Existing interpreter is selected'
    $otherVersion = [pscustomobject]@{Executable='C:\Python313\python.exe';Version='3.13.7';Major=3;Minor=13;Compatible=$true;Reason=''}
    $bad = [pscustomobject]@{Executable='C:\old\python.exe';Version='3.9';Major=3;Minor=9;Compatible=$false;Reason='old'}
    $selected = Select-ExistingPython -Inventory @($bad,$otherVersion)
    Assert-True ($selected.Executable -eq $otherVersion.Executable) 'Selection is not restricted to Python 3.12'
    $caught = $false
    try { Select-ExistingPython -Inventory @($bad) | Out-Null } catch { $caught = $_.Exception.Message.Contains('Nothing was downloaded') }
    Assert-True $caught 'No compatible interpreter stops with diagnostics instead of installing another Python'
    $caught = $false
    try { Select-ExistingPython -Inventory @($otherVersion) -ExplicitPath 'C:\missing\python.exe' | Out-Null } catch { $caught = $true }
    Assert-True $caught 'Invalid explicit Python path does not fall back silently'
    $launcherText = " -V:3.13 * C:\Users\Fullservis TG\Python313\python.exe`n -V:3.11 C:\Python311\python.exe`n"
    $paths = @(ConvertFrom-LauncherPaths $launcherText)
    Assert-True ($paths.Count -eq 2 -and $paths[0] -eq 'C:\Users\Fullservis TG\Python313\python.exe') 'Launcher listing preserves paths with spaces'
    $script:InstallLog = Join-Path $temp 'install.log'
    Invoke-LoggedNative -Executable $TestPython -Arguments @('-c','import sys; print("stdout marker"); print("stderr marker",file=sys.stderr)')
    $logged = Get-Content -LiteralPath $script:InstallLog -Raw
    Assert-True ($logged.Contains('stdout marker') -and $logged.Contains('stderr marker')) 'Both native output streams are saved to the log'
    $caught = $false
    try { Invoke-LoggedNative -Executable $TestPython -Arguments @('-c','import sys; print("dependency failure",file=sys.stderr); sys.exit(7)') } catch { $caught = $_.Exception.Message.Contains('exit code 7') }
    Assert-True ($caught -and $ErrorActionPreference -eq 'Stop') 'Real dependency failure is reported and preference restored'
    $installer = Get-Content -LiteralPath (Join-Path (Split-Path $PSScriptRoot -Parent) 'install.ps1') -Raw
    Assert-True (-not $installer.Contains('Invoke-WebRequest') -and -not $installer.Contains('Install-PrivatePython')) 'Installer contains no automatic Python download path'
    Write-Host ("Installer regression tests: $passed passed")
} finally {
    $script:InstallLog = $null
    Remove-Item -LiteralPath $temp -Recurse -Force
}
