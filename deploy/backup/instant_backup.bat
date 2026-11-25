@echo off
REM ======================== ESSA INSTANT BACKUP ========================
REM  Double-click at ANY time (e.g. 10 AM): saves all data up to this moment.
REM  ESSA keeps running. A message box says when it is done.
REM  Saved as ESSA-INSTANT-<date>_<time>.zip (also on the pen drive if
REM  plugged in). The newest 20 are kept.
REM  Tip: right-click > Send to > Desktop (create shortcut) for staff.
REM
REM  Settings: top of daily_backup.py. Keep all four backup files together.
REM =====================================================================
title ESSA Instant Backup
setlocal

REM The ESSA folder. These files live in ESSA\deploy\backup; if they are
REM moved, put the full ESSA path here instead.
set "ESSA_HOME=%~dp0..\.."
set "PY=%ESSA_HOME%\backend\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Taking the instant backup now. Please wait...
"%PY%" "%~dp0instant_backup.py" %*
exit /b %ERRORLEVEL%
