@echo off
setlocal
cd /d "%~dp0.."

where pyinstaller >nul 2>nul
if errorlevel 1 (
  echo PyInstaller was not found.
  echo Install it with: python -m pip install pyinstaller pywebview
  exit /b 1
)

if not exist build mkdir build
if not exist dist mkdir dist

pyinstaller --noconfirm --onefile --console --name cmdgo-provider ^
  --hidden-import=webview ^
  --hidden-import=webview.platforms ^
  --hidden-import=webview.platforms.edgechromium ^
  --hidden-import=clr_loader ^
  --hidden-import=pythonnet ^
  cmdgo_provider.py

echo.
echo Build complete. File is in dist\cmdgo-provider.exe
echo   GUI mode:    dist\cmdgo-provider.exe
echo   Console:     dist\cmdgo-provider.exe --no-gui
echo   Login:       dist\cmdgo-provider.exe --login --keep-alive
