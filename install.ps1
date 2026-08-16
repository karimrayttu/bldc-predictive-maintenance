# ============================================================
#  BLDC Motor Bench: one-click installer engine
#  Launched (elevated) by INSTALL.bat. Robust + self-diagnosing:
#  finds/installs Python, installs packages, verifies everything,
#  and creates the desktop shortcut. Reports issues instead of
#  failing silently.
# ============================================================
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function OK($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [X]  $m" -ForegroundColor Red }

$problems = @()

try {
    Write-Host "============================================================"
    Write-Host "   BLDC Motor Bench  -  one-click installer" -ForegroundColor White
    Write-Host "============================================================"

    # 1. authority / environment preflight
    Step "Checking authority and environment"
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($admin) { OK "Administrator rights granted" } else { Warn "Not elevated - will install per-user (fine; driver installs may ask for admin later)" }
    try {
        $t = Join-Path $PSScriptRoot ".wtest"; "x" | Out-File -FilePath $t -Encoding ascii; Remove-Item $t -Force
        OK "Install folder is writable"
    } catch { Fail "Install folder is NOT writable: $PSScriptRoot"; $problems += "folder not writable" }

    # 2. locate Python 3 (install via winget if missing)
    Step "Locating Python 3"
    function Get-Python {
        if (Get-Command py -ErrorAction SilentlyContinue) { return @("py", "-3") }
        if (Get-Command python -ErrorAction SilentlyContinue) { return @("python") }
        $p = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
        if (Test-Path $p) { return @($p) }
        return $null
    }
    $py = Get-Python
    if (-not $py) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Warn "Python not found - installing via winget (Python.Python.3.12)..."
            winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
            $py = Get-Python
        }
    }
    if (-not $py) {
        Fail "Python 3 is not installed and could not be installed automatically."
        Write-Host "      Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run INSTALL.bat." -ForegroundColor Yellow
        Read-Host "`nPress Enter to close"; exit 1
    }
    $pyExe = $py[0]; $pyPre = @(); if ($py.Length -gt 1) { $pyPre = $py[1..($py.Length - 1)] }
    $ver = & $pyExe @pyPre --version
    OK "Python found: $ver  ($pyExe $pyPre)"

    # 3. install Python packages
    Step "Installing Python packages"
    $req = Join-Path $PSScriptRoot "app\requirements.txt"
    & $pyExe @pyPre -m pip install --upgrade pip
    $lock = Join-Path $PSScriptRoot "app\requirements.lock.txt"
    if (Test-Path $lock) {
        Write-Host "  using pinned versions (app/requirements.lock.txt)" -ForegroundColor DarkGray
        & $pyExe @pyPre -m pip install -r $lock
        if ($LASTEXITCODE -ne 0) { Warn "Pinned install failed - falling back to requirements.txt"; & $pyExe @pyPre -m pip install -r $req }
    } else { & $pyExe @pyPre -m pip install -r $req }
    if ($LASTEXITCODE -ne 0) {
        Warn "Default install failed - retrying per-user (--user)..."
        & $pyExe @pyPre -m pip install --user -r $req
    }
    if ($LASTEXITCODE -ne 0) { Fail "Package install failed (check internet / proxy)."; $problems += "pip install failed" } else { OK "Python packages installed" }

    # 4. verify the whole setup
    # verify EVERYTHING, including the flash toolchain
    # Pin the interpreter that just received the packages. "py -3" picks the HIGHEST
    # installed Python, which is not necessarily the one pip installed into, on a machine
    # with 3.13 and 3.14 side by side that produces a confident, wrong "cannot run".
    Step "Recording which Python to use"
    $interp = & $pyExe @pyPre -c "import sys;print(sys.executable)"
    if ($interp) {
        Set-Content -Path (Join-Path $PSScriptRoot "tools\interpreter.txt") -Value $interp -Encoding utf8
        OK "Pinned: $interp"
    }

    Step "Checking what this machine can do (run / flash)"
    & $pyExe @pyPre (Join-Path $PSScriptRoot "tools\doctor.py")
    $doctorCode = $LASTEXITCODE
    if ($doctorCode -ne 0) { $problems += "app cannot run yet - see the setup check above" }

    Step "Verifying setup"
    & $pyExe @pyPre -m app.core.setup_check
    $verifyExit = $LASTEXITCODE

    # 5. desktop shortcut
    Step "Creating the 'BLDC Motor Bench' desktop shortcut"
    & $pyExe @pyPre (Join-Path $PSScriptRoot "app\tools\make_shortcut.py")

    # summary
    Write-Host "`n============================================================"
    if ($problems.Count -eq 0 -and $verifyExit -eq 0) {
        Write-Host "  INSTALL COMPLETE - core app is READY." -ForegroundColor Green
    } else {
        Write-Host "  INSTALL FINISHED WITH NOTES:" -ForegroundColor Yellow
        foreach ($p in $problems) { Write-Host "    - $p" -ForegroundColor Yellow }
        Write-Host "  (See the verification list above for anything marked '--'.)" -ForegroundColor Yellow
    }
    Write-Host "  Launch via the 'BLDC Motor Bench' desktop shortcut." -ForegroundColor White
    Write-Host "  The setup check above states plainly whether you can RUN and whether" -ForegroundColor White
    Write-Host "  you can FLASH. Re-run it any time with:  py -3 tools\doctor.py" -ForegroundColor White
    Write-Host "  has 'Verify setup' + 'Get STM32 tools' to finish that anytime." -ForegroundColor White
    Write-Host "============================================================"
}
catch {
    Fail $_.Exception.Message
    Write-Host "Installer stopped. Fix the error above and re-run INSTALL.bat." -ForegroundColor Yellow
}
Read-Host "`nPress Enter to close"
