@echo off
rem ============================================================================
rem  Builds ESSA.exe (the desktop launcher) and puts it in the ESSA folder.
rem  Needs Python 3 and the internet the first time (to fetch PyInstaller).
rem  The build tools go in deploy\desktop\.buildenv, not in the app's own Python.
rem ============================================================================
setlocal
cd /d "%~dp0"
set "BENV=%~dp0.buildenv"
set "BPY=%BENV%\Scripts\python.exe"

if exist "%BPY%" goto have_env
echo ==^> Creating the build environment
set "BASE=%~dp0..\..\backend\.venv\Scripts\python.exe"
if not exist "%BASE%" set "BASE=py -3"
%BASE% -m venv "%BENV%" || goto failed
"%BPY%" -m pip install --quiet --upgrade pip
:have_env
"%BPY%" -m pip install --quiet pyinstaller pillow || goto failed

echo ==^> Making the icon
"%BPY%" -c "from PIL import Image; Image.open(r'..\..\backend\app\mobile\icon-512.png').convert('RGBA').save('essa.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])" || goto failed

echo ==^> Building ESSA.exe
"%BPY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name ESSA --icon "%~dp0essa.ico" --add-data "%~dp0essa.ico;." --exclude-module PIL ^
    --distpath dist --workpath build --specpath build "%~dp0essa_desktop.py" || goto failed

copy /y dist\ESSA.exe "%~dp0..\..\ESSA.exe" >nul || goto failed
echo.
echo Done: %~dp0..\..\ESSA.exe
echo Double-click it to start ESSA. Right-click ^> Send to ^> Desktop (create shortcut) for a desktop icon.
goto finish

:failed
echo.
echo *** Build FAILED - read the messages above. ***

:finish
echo %cmdcmdline% | find /i "/c" >nul && pause
endlocal
