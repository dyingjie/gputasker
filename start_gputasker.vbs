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

KillExistingProcess pythonExe, scriptDir & "\manage.py", "runserver"
KillExistingProcess pythonExe, scriptDir & "\main.py", ""

cmdWeb = Quote(pythonExe) & " manage.py runserver --insecure 0.0.0.0:8888 --noreload"
shell.Run cmdWeb, 0, False
WScript.Sleep 2000

cmdScheduler = """" & pythonExe & """ main.py"
shell.Run cmdScheduler, 0, False

Function Quote(value)
    Quote = """" & value & """"
End Function

Sub KillExistingProcess(pythonPath, scriptPath, extraToken)
    Dim killCommand, escapedPythonPath, escapedScriptPath, escapedScriptName, escapedExtraToken

    escapedPythonPath = Replace(LCase(pythonPath), "'", "''")
    escapedScriptPath = Replace(LCase(scriptPath), "'", "''")
    escapedScriptName = Replace(LCase(fso.GetFileName(scriptPath)), "'", "''")
    escapedExtraToken = Replace(LCase(extraToken), "'", "''")

    killCommand = "powershell -NoProfile -ExecutionPolicy Bypass -Command " _
        & Quote("$python = '" & escapedPythonPath & "'; " _
        & "$script = '" & escapedScriptPath & "'; " _
        & "$scriptName = '" & escapedScriptName & "'; " _
        & "$token = '" & escapedExtraToken & "'; " _
        & "Get-CimInstance Win32_Process | Where-Object { " _
        & "$_.Name -eq 'python.exe' -and " _
        & "$_.ExecutablePath -and $_.ExecutablePath.ToLower() -eq $python -and " _
        & "$_.CommandLine -and " _
        & "($_.CommandLine.ToLower().Contains($script) -or $_.CommandLine.ToLower().Contains($scriptName)) -and " _
        & "($token -eq '' -or $_.CommandLine.ToLower().Contains($token)) " _
        & "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")

    shell.Run killCommand, 0, True
End Sub
