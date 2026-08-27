Option Explicit

Dim fso, shell, baseDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run "wscript.exe """ & baseDir & "\start_proxy.vbs""", 0, False
