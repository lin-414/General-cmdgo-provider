@echo off
setlocal
cd /d "%~dp0.."

where pyinstaller >nul 2>nul
if errorlevel 1 (
  echo PyInstaller was not found.
  echo Install it with: python -m pip install pyinstaller customtkinter pystray pillow
  exit /b 1
)

if not exist build mkdir build
if not exist dist mkdir dist

rem Generate the app icon if missing
if not exist "assets\icon.ico" python windows\make_icon.py

pyinstaller --noconfirm --onefile --windowed ^
  --name cmdgo-gui ^
  --icon "assets\icon.ico" ^
  --add-data "assets;assets" ^
  --hidden-import customtkinter ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageDraw ^
  --hidden-import pystray ^
  --hidden-import pystray._win32 ^
  cmdgo_gui.py

echo.
echo Build complete. File is in dist\cmdgo-gui.exe
echo   GUI + tray (windowed, no console): dist\cmdgo-gui.exe
