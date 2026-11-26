@echo off
rem ============================================================================
rem  Restart ESSA Server  -  what the desktop icon runs.
rem  Double-click it whenever ESSA is not opening on the other PCs. It asks for
rem  administrator permission, then:
rem    - restarts ESSA,
rem    - starts the ESSA start-up task again if it had stopped,
rem    - sets the server up again if the task is missing,
rem  and opens ESSA in the browser when it is back.
rem ============================================================================
setlocal

rem Not yet administrator? Ask Windows for permission and run again elevated.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

title Restart ESSA Server
set "PY=%~dp0..\..\backend\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo The ESSA Python environment is missing. Run setup.bat in the ESSA folder first.
    pause
    exit /b 1
)
"%PY%" "%~dp0server_setup.py" repair
echo.
if errorlevel 1 (
    echo Something is still wrong - see the message above.
) else (
    echo ESSA is running. You can close this window.
)
pause
endlocal
