Option Explicit

Dim shell, fso, scriptDir, pythonExe, cmdWeb, cmdScheduler

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonExe = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\miniconda3\envs\gputasker\python.exe"

If Not fso.FileExists(pythonExe) Then
    MsgBox "Python not found: " & pythonExe, vbCritical, "GPU Tasker"
    WScript.Quit 1
End If

shell.CurrentDirectory = scriptDir

cmdWeb = """" & pythonExe & """ manage.py runserver --insecure 0.0.0.0:8888"
cmdScheduler = """" & pythonExe & """ main.py"

shell.Run cmdWeb, 0, False
WScript.Sleep 2000
shell.Run cmdScheduler, 0, False
