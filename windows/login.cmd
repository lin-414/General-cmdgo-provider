@echo off
setlocal
set "ROOT=%~dp0..\"
if exist "%ROOT%cmdgo-provider.exe" (
  start "" "%ROOT%cmdgo-provider.exe" --login --keep-alive
  exit /b
)
set "PYTHON=python.exe"
start "" "%PYTHON%" "%ROOT%cmdgo_provider.py" --login --keep-alive
