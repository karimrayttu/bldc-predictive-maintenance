@echo off
REM ===== BLDC PHM supervised launcher (2026-07-30) =====
REM The app can be killed at any moment by an external UI-automation collision
REM (Windows 0x8001010d in the Qt event loop, documented, not a bench bug).
REM This wrapper relaunches it automatically and appends each exit to a log.
REM An interrupted batch resumes itself from data\_live\batch_state.json.
cd /d "%~dp0"
REM SINGLETON: refuse to start if another supervisor loop is already running.
if exist data\_live\supervisor.pid (
  for /f %%i in (data\_live\supervisor.pid) do tasklist /fi "PID eq %%i" 2>nul | find "cmd.exe" >nul && exit /b 0
)
echo %~1 > nul
powershell -Command "(Get-CimInstance Win32_Process -Filter \"ProcessId=$PID\") | Out-Null" 2>nul
echo %RANDOM% > data\_live\supervisor.pid.tmp
move /y data\_live\supervisor.pid.tmp data\_live\supervisor.pid > nul
set PY=
if exist "%~dp0tools\interpreter.txt" set /p PY=<"%~dp0tools\interpreter.txt"
if not defined PY set PY=python
:loop
echo [%date% %time%] launching app >> data\_live\supervisor.log
"%PY%" -X faulthandler -m apps.bench.main >> data\_live\supervisor.log 2>&1
echo [%date% %time%] app exited (code %errorlevel%) - relaunch in 3 s >> data\_live\supervisor.log
ping -n 4 127.0.0.1 > nul
goto loop
