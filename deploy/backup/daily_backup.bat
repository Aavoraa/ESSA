@echo off
REM ========================= ESSA DAILY BACKUP =========================
REM  Automatic backup every night at 10:30 PM.
REM
REM  Right-click > Run as administrator  ONCE on the server: sets up the
REM                                      10:30 PM backup and takes the first
REM                                      one to prove it works.
REM  Double-click ...................... shows whether it is set up and when
REM                                      it last ran.
REM  The 10:30 PM task runs ............ daily_backup.bat run
REM
REM  Settings (folder, pen drive, time, how many to keep): top of
REM  daily_backup.py. Keep all four backup files in one folder.
REM =====================================================================
title ESSA Daily Backup
setlocal

REM The ESSA folder. These files live in ESSA\deploy\backup; if they are
REM moved, put the full ESSA path here instead.
set "ESSA_HOME=%~dp0..\.."
set "ESSA_DAILY_BAT=%~f0"
set "PY=%ESSA_HOME%\backend\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if not "%~1"=="" goto given
set "ACTION=info"
net session >nul 2>&1
if not errorlevel 1 set "ACTION=setup"
if "%ACTION%"=="setup" echo Setting up the 10:30 PM daily backup and taking the first one. Please wait...
"%PY%" "%~dp0daily_backup.py" %ACTION%
exit /b %ERRORLEVEL%

:given
"%PY%" "%~dp0daily_backup.py" %*
exit /b %ERRORLEVEL%
