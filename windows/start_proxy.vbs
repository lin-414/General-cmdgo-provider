Option Explicit

Dim fso, shell, scriptDir, baseDir, exeFile, pyFile, logDir, logFile, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
baseDir = fso.GetParentFolderName(scriptDir)
exeFile = baseDir & "\cmdgo-provider.exe"
pyFile = baseDir & "\cmdgo_provider.py"
logDir = baseDir & "\logs"
logFile = logDir & "\proxy.log"

If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
shell.CurrentDirectory = baseDir

If fso.FileExists(exeFile) Then
  command = """" & exeFile & """ --no-gui"
  shell.Run command, 0, False
Else
  pythonExe = "python.exe"
  command = "cmd.exe /d /c """"" & pythonExe & """ """ & pyFile & """ --no-gui >> """ & logFile & """ 2>&1"""""
  shell.Run command, 0, False
End If
