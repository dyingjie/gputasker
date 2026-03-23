Option Explicit

Dim shell, fso, scriptDir, pythonExe, cmdMigrate, cmdWeb, cmdScheduler, exitCode

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonExe = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\miniconda3\envs\gputasker\python.exe"

If Not fso.FileExists(pythonExe) Then
    MsgBox "Python not found: " & pythonExe, vbCritical, "GPU Tasker"
    WScript.Quit 1
End If

shell.CurrentDirectory = scriptDir

cmdMigrate = Quote(pythonExe) & " manage.py migrate --noinput"
exitCode = shell.Run(cmdMigrate, 0, True)
If exitCode <> 0 Then
    MsgBox "Database migration failed. Exit code: " & exitCode, vbCritical, "GPU Tasker"
    WScript.Quit exitCode
End If

If Not IsPortListening(8888) Then
    cmdWeb = Quote(pythonExe) & " manage.py runserver --insecure 0.0.0.0:8888 --noreload"
    shell.Run cmdWeb, 0, False
    WScript.Sleep 2000
End If

cmdScheduler = """" & pythonExe & """ main.py"
shell.Run cmdScheduler, 0, False

Function Quote(value)
    Quote = """" & value & """"
End Function

Function IsPortListening(port)
    Dim checkCommand, checkExitCode

    checkCommand = "powershell -NoProfile -Command ""$conn = Get-NetTCPConnection -State Listen -LocalPort " _
        & port _
        & " -ErrorAction SilentlyContinue; if ($null -ne $conn) { exit 0 } else { exit 1 }"""
    checkExitCode = shell.Run(checkCommand, 0, True)
    IsPortListening = (checkExitCode = 0)
End Function
