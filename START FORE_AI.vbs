' Launch the Fore_Ai Tkinter dashboard without console
Dim fso, curDir, cmd, sh
Set fso = CreateObject("Scripting.FileSystemObject")
curDir = fso.GetParentFolderName(WScript.ScriptFullName)

cmd = "cmd /c cd /d """ & curDir & """ && (pythonw -X utf8 ""fore_ai_dashboard.py"" || (py -3 ""fore_ai_dashboard.py"" || python ""fore_ai_dashboard.py""))"
Set sh = CreateObject("WScript.Shell")
sh.Run cmd, 0, False
