' panel-hidden.vbs - start the watch panel in the background (no window).
' Paths are derived from this file's own location, so the project can be
' cloned or copied anywhere, under any user name.
' Stop it from the panel page button (top right), or run: houmai stop
' Need to watch it live? Run "houmai web" in any console window.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = root
sh.Run """" & root & "\scripts\houmai.bat"" web", 0, False
