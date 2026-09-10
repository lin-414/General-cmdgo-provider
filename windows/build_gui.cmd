@echo off
setlocal
cd /d "%~dp0.."

where pyinstaller >nul 2>nul
if errorlevel 1 (
  echo PyInstaller was not found.
  echo Install it with: python -m pip install pyinstaller customtkinter pystray pillow cryptography
  exit /b 1
)

if not exist build mkdir build
if not exist dist mkdir dist

rem Generate the app icon if missing
if not exist "assets\icon.ico" python windows\make_icon.py

rem Use "python -m PyInstaller" so it is the same interpreter as "python" above
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name General-cmdgo-provider ^
  --icon "assets\icon.ico" ^
  --add-data "assets;assets" ^
  --hidden-import customtkinter ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageDraw ^
  --hidden-import pystray ^
  --hidden-import pystray._win32 ^
  --hidden-import pool ^
  --hidden-import cryptography ^
  cmdgo_gui.py

rem Fail loudly if PyInstaller failed: otherwise we would report success with a stale dist
if errorlevel 1 (
  echo.
  echo BUILD FAILED. If the exe is in use, close the running app first ^(tray -^> exit^).
  exit /b 1
)

echo.
echo Build complete. File is in dist\General-cmdgo-provider.exe
echo   GUI + tray (windowed, no console): dist\General-cmdgo-provider.exe
