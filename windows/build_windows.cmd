@echo off
setlocal
cd /d "%~dp0.."

where pyinstaller >nul 2>nul
if errorlevel 1 (
  echo PyInstaller was not found.
  echo Install it with: python -m pip install pyinstaller
  exit /b 1
)

if not exist build mkdir build
if not exist dist mkdir dist

pyinstaller --noconfirm --onefile --console --name cmdgo-provider --add-data "cmdgo_provider.py;." --hidden-import http --hidden-import http.client --hidden-import http.server --hidden-import urllib --hidden-import urllib.error --hidden-import urllib.parse --hidden-import urllib.request --hidden-import platform --hidden-import secrets run.py
pyinstaller --noconfirm --onefile --windowed --name cmdgo-login login.py

echo.
echo Build complete. Files are in dist\
