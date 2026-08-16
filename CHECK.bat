@echo off
REM ============================================================
REM   Re-check this machine at any time. Safe to run repeatedly;
REM   it installs nothing.
REM ============================================================
cd /d "%~dp0"
set PY=
if exist "%~dp0tools\interpreter.txt" set /p PY=<"%~dp0tools\interpreter.txt"
if defined PY if exist "%PY%" ( "%PY%" "%~dp0tools\doctor.py" & echo. & pause & exit /b )
where py >nul 2>nul && ( py -3 "%~dp0tools\doctor.py" & echo. & pause & exit /b )
where python >nul 2>nul && ( python "%~dp0tools\doctor.py" & echo. & pause & exit /b )
echo Python not found. Run INSTALL.bat first.
pause
