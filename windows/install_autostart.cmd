@echo off
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%STARTUP%\CommandCode Go Proxy.vbs"
set "LAUNCHER=%~dp0start_proxy.vbs"

if not exist "%STARTUP%" (
  echo Windows Startup folder was not found.
  exit /b 1
)

> "%TARGET%" echo Option Explicit
>> "%TARGET%" echo Dim shell
>> "%TARGET%" echo Set shell = CreateObject("WScript.Shell")
>> "%TARGET%" echo shell.Run "wscript.exe ""%LAUNCHER%""", 0, False
if errorlevel 1 (
  echo Failed to install the Startup entry.
  exit /b 1
)

wscript.exe "%LAUNCHER%"
echo.
echo Installed Windows Startup entry: %TARGET%
echo The proxy starts automatically when this Windows account logs in.
echo Endpoint: http://127.0.0.1:8787
