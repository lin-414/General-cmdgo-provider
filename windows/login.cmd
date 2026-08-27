@echo off
setlocal
set "ROOT=%~dp0..\"
if exist "%ROOT%cmdgo-login.exe" (
  "%ROOT%cmdgo-login.exe" --base http://127.0.0.1:8787
  exit /b %errorlevel%
)
set "PYTHON=python.exe"
"%PYTHON%" "%ROOT%login.py" --base http://127.0.0.1:8787
