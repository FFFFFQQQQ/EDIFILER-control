@echo off
cd /d "%~dp0"

echo Building... first run takes about a minute.
echo.

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "W830NB-Console" ^
  --icon "ui\icon.ico" ^
  --add-data "ui;ui" ^
  --collect-submodules winrt ^
  --hidden-import pystray._win32 ^
  --exclude-module moderngl ^
  --exclude-module tkinter ^
  edifier_app.py

if errorlevel 1 (
  echo.
  echo BUILD FAILED - see the errors above.
) else (
  echo.
  echo OK: dist\W830NB-Console.exe
)

echo.
pause
