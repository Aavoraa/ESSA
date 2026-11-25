@echo off
rem ============================================================================
rem  Move ESSA (and the shop / POS) from SQLite to PostgreSQL on this PC.
rem    postgres_setup.bat               create login + database, copy all data
rem    postgres_setup.bat --port 5433   a PostgreSQL on another port
rem    postgres_setup.bat verify        compare SQLite and PostgreSQL row counts
rem  Stop ESSA first. It asks for the PostgreSQL admin password (hidden, not saved).
rem ============================================================================
setlocal
set "PY=%~dp0..\..\backend\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo The ESSA Python environment is missing. Run setup.bat in the ESSA folder first.
    goto finish
)
"%PY%" "%~dp0postgres_setup.py" %*

:finish
echo %cmdcmdline% | find /i "/c" >nul && pause
endlocal
