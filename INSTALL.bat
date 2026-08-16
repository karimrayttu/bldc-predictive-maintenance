@echo off
REM ============================================================
REM   BLDC Motor Bench, ONE-CLICK installer
REM   Double-click this. It installs Python packages, verifies
REM   that this machine can RUN and FLASH, and makes a shortcut.
REM
REM   NOTE: this deliberately does NOT ask for administrator.
REM   Running elevated would install the packages and the desktop
REM   shortcut into the ADMINISTRATOR's profile, not yours, and
REM   the app would then fail to start from your own account.
REM ============================================================
cd /d "%~dp0"

REM ---- clear the Mark-of-the-Web so PowerShell will run the script ----
REM A zip that arrived by email, Teams or Discord is flagged as "from the
REM internet"; every .ps1 inside it is blocked until unblocked.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -Recurse -Force -LiteralPath '%~dp0' | Unblock-File" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
