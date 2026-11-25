@echo off
rem ============================================================================
rem  ESSA - load a Quanta / TAQUALIVE JSON export into the legacy_tqa database
rem
rem  Ways to use it:
rem    - double-click it          : asks for the JSON file / folder / zip
rem    - drag a file onto it      : loads that file
rem    - from a command prompt    : load_json.bat "D:\export" [table ...] [--dry-run]
rem
rem  --dry-run reads and checks the JSON without touching any database.
rem  The database comes from LEGACY_DATABASE_URL if it is set; otherwise it asks.
rem  The password is typed hidden and passed in PGPASSWORD, never on screen.
rem ============================================================================
setlocal
cd /d "%~dp0"
title ESSA - load JSON export

rem --- Python: the ESSA backend's own, else any installed one ---------------
set "PY=%~dp0..\..\backend\.venv\Scripts\python.exe"
if exist "%PY%" goto have_python
set "PY=py -3"
%PY% --version >nul 2>&1 && goto have_python
set "PY=python"
%PY% --version >nul 2>&1 && goto have_python
echo Python was not found. Run setup.bat in the ESSA folder first.
goto finish
:have_python

rem --- What to load ----------------------------------------------------------
set "ARGS=%*"
if not "%~1"=="" goto have_source
echo.
echo Type the path of the JSON file, folder or .zip (or drag it into this window)
set "SRC="
set /p "SRC=Path: "
if not defined SRC goto finish
set "SRC=%SRC:"=%"
set "ARGS="%SRC%""
:have_source

rem --- A dry run needs no database ------------------------------------------
echo %ARGS% | find /i "--dry-run" >nul && goto run

rem --- psycopg (the PostgreSQL driver) --------------------------------------
%PY% -c "import psycopg" >nul 2>&1 && goto have_driver
echo.
echo The PostgreSQL driver for Python (psycopg) is not installed.
set "ANS="
set /p "ANS=Install it now? [Y/N]: "
if /i not "%ANS%"=="Y" goto finish
%PY% -m pip install "psycopg[binary]" || goto finish
:have_driver

rem --- Which database --------------------------------------------------------
if defined LEGACY_DATABASE_URL goto have_url
set "LEGACY_DATABASE_URL=postgresql://postgres@localhost:5432/legacy_tqa"
echo.
echo Database to load into [press Enter for %LEGACY_DATABASE_URL%]
set /p "LEGACY_DATABASE_URL=URL: "
:have_url
if defined PGPASSWORD goto run
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$s = Read-Host 'PostgreSQL password' -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"`) do set "PGPASSWORD=%%p"

:run
echo.
%PY% "%~dp0load_json.py" %ARGS%
if errorlevel 1 (
    echo.
    echo *** Load FAILED or had problems - read the messages above. ***
) else (
    echo.
    echo Load finished OK.
)

:finish
rem keep the window open when double-clicked
echo %cmdcmdline% | find /i "/c" >nul && pause
endlocal
