@echo off
setlocal
wscript.exe "%~dp0start_proxy.vbs"
echo CommandCode Go proxy start requested.
echo Log: %~dp0logs\proxy.log
