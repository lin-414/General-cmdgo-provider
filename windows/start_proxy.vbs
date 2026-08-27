Option Explicit

Dim fso, shell, scriptDir, baseDir, pythonExe, runFile, exeFile, logDir, logFile, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
baseDir = fso.GetParentFolderName(scriptDir)
exeFile = baseDir & "\cmdgo-provider.exe"
runFile = baseDir & "\run.py"
logDir = baseDir & "\logs"
logFile = logDir & "\proxy.log"

If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
shell.CurrentDirectory = baseDir

If fso.FileExists(exeFile) Then
  command = "cmd.exe /d /c """ & exeFile & "" >> """ & logFile & "" 2>&1"
Else
  pythonExe = "python.exe"
  command = "cmd.exe /d /c """ & pythonExe & """ """ & runFile & """ >> """ & logFile & "" 2>&1"
End If

shell.Run command, 0, False
