' run-hidden.vbs - run one watch cycle with no window. Scheduled task entry.
' Paths are derived from this file's own location, so the project can be
' cloned or copied anywhere, under any user name.
' Still calls scripts\houmai.bat: the interpreter-independence checks live
' only there, and one place is enough to keep correct.
' Args: 0 = hide the window, True = wait until this cycle finishes.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = root
sh.Run """" & root & "\scripts\houmai.bat"" run", 0, True
