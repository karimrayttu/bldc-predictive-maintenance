@echo off
REM ===== BLDC PHM launcher (portable) =====
REM Finds Python on any machine via the py launcher / PATH; no hardcoded paths.
cd /d "%~dp0"
REM Prefer the interpreter the installer put the packages into. Falling straight to
REM "py -3" would pick the newest Python on the machine, which may not be that one.
set PY=
if exist "%~dp0tools\interpreter.txt" set /p PY=<"%~dp0tools\interpreter.txt"
if defined PY if exist "%PY%" (
  set PYW=%PY:python.exe=pythonw.exe%
  if exist "%PYW%" ( start "" "%PYW%" -m apps.bench.main & exit /b 0 )
  start "" "%PY%" -m apps.bench.main & exit /b 0
)
where pyw     >nul 2>nul && ( start "" pyw -3 -m apps.bench.main & exit /b 0 )
where py      >nul 2>nul && ( start "" py  -3 -m apps.bench.main & exit /b 0 )
where pythonw >nul 2>nul && ( start "" pythonw -m apps.bench.main & exit /b 0 )
if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" (
  start "" "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" -m apps.bench.main & exit /b 0 )
echo Python not found. Please run INSTALL.bat first (installs Python packages and the shortcut).
pause
