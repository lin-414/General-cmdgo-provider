@echo off
setlocal
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CommandCode Go Proxy.vbs"

if exist "%TARGET%" (
  del /Q "%TARGET%"
  if errorlevel 1 (
    echo Failed to remove the Startup entry.
    exit /b 1
  )
  echo Removed Windows Startup entry: %TARGET%
) else (
  echo Startup entry was not found.
)

echo Existing proxy processes are not stopped by this command.
