@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$items = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and (($_.CommandLine -like '*cmdgo-provider.exe*') -or ($_.CommandLine -like '*cmdgo-provider*run.py*')) }; if (-not $items) { Write-Host 'No cmdgo-provider process found.'; exit 0 }; $items | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Stopped PID ' + $_.ProcessId) }"
