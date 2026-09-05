Option Explicit

Dim fso, shell, scriptDir, baseDir, guiExe, exeFile, pyFile, logDir, logFile, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
baseDir = fso.GetParentFolderName(scriptDir)
guiExe = baseDir & "\General-cmdgo-provider.exe"
exeFile = baseDir & "\cmdgo-provider.exe"
pyFile = baseDir & "\cmdgo_provider.py"
logDir = baseDir & "\logs"
logFile = logDir & "\proxy.log"

If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
shell.CurrentDirectory = baseDir

If fso.FileExists(guiExe) Then
  ' GUI exe：--minimized 直接常驻托盘，不弹窗口
  command = """" & guiExe & """ --minimized"
  shell.Run command, 0, False
ElseIf fso.FileExists(exeFile) Then
  command = """" & exeFile & """ --no-gui"
  shell.Run command, 0, False
Else
  command = "cmd.exe /d /c """"" & pythonExe & """ """ & pyFile & """ --no-gui >> """ & logFile & """ 2>&1"""""
  shell.Run command, 0, False
End If
