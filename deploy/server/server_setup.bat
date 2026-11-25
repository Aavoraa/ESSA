@echo off
rem ============================================================================
rem  ESSA server setup - no exe needed anywhere.
rem    right-click > Run as administrator   set this PC up as the ESSA server
rem    server_setup.bat status              is it running, and at which URL
rem    server_setup.bat remove              undo (as administrator)
rem    server_setup.bat restart             restart ESSA (as administrator)
rem  Counter PCs then simply open  http://<this PC's IP>:8000  in Chrome / Edge.
rem ============================================================================
setlocal
set "PY=%~dp0..\..\backend\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo The ESSA Python environment is missing. Run setup.bat in the ESSA folder first.
    goto finish
)
"%PY%" "%~dp0server_setup.py" %*

:finish
echo %cmdcmdline% | find /i "/c" >nul && pause
endlocal
