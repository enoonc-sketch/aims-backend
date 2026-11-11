' AIMS - run a PowerShell maintenance script with no console window at all.
'
' Why this exists:
'   A shortcut that targets powershell.exe flashes a console window for a
'   moment even with -WindowStyle Hidden, because the console is created
'   before the style is applied. WScript.Shell.Run with window style 0
'   starts the process already hidden, so the user sees nothing.
'
' Usage (called by the installer shortcuts):
'   wscript.exe run-hidden.vbs backup.ps1 -Notify
'
' The exit code of the PowerShell script is passed through to the caller,
' so failure is never silently swallowed.
Option Explicit

Dim shell, fso, baseDir, scriptName, extraArgs, i, cmd, rc

If WScript.Arguments.Count = 0 Then
    WScript.Quit 2
End If

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptName = WScript.Arguments(0)

extraArgs = ""
For i = 1 To WScript.Arguments.Count - 1
    extraArgs = extraArgs & " " & WScript.Arguments(i)
Next

cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ _
      & baseDir & "\" & scriptName & """" & extraArgs

' 0 = hidden window, True = wait for completion so we can return the real code
rc = shell.Run(cmd, 0, True)
WScript.Quit rc
