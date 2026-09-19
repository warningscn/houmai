@echo off
setlocal
set "TASK=houmai"
set "WRAPPER=%~dp0houmai.bat"
set "VBS=%~dp0run-hidden.vbs"
set "PANELVBS=%~dp0panel-hidden.vbs"

echo ============================================
echo  houmai - register scheduled task
echo ============================================
echo.

if not exist "%WRAPPER%" (
  echo [FAIL] launcher not found: %WRAPPER%
  echo        scripts\houmai.bat ships with the project, so this copy looks
  echo        incomplete - something was moved out of the project folder.
  echo        Copy the whole project folder again and re-run this.
  set "RESULT=1"
  goto :end
)
if not exist "%VBS%" (
  echo [FAIL] hidden launcher not found: %VBS%
  set "RESULT=1"
  goto :end
)

echo Registering task "%TASK%" - every 10 minutes, hidden, also on battery...
echo (action, trigger, settings and power policy are set in one call)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register-task.ps1" -TaskName "%TASK%" -Vbs "%VBS%" -IntervalMinutes 10
if errorlevel 1 (
  echo.
  echo [FAIL] the task was not registered cleanly - see the reason above.
  echo        If the message above says the task still carries an AC-only
  echo        restriction, the task does exist but would stop on battery.
  echo        The houmai:// protocol was NOT registered either, so the panel
  echo        "start" button will not work until this is fixed.
  echo        Fallback: create it by hand in Task Scheduler (taskschd.msc):
  echo          action  = wscript.exe "%VBS%"
  echo          trigger = repeat every 10 minutes
  echo          and under Conditions clear BOTH AC-power checkboxes,
  echo          otherwise the watch silently stops when running on battery.
  set "RESULT=1"
  goto :end
)

echo.
echo Registering houmai:// protocol (lets the panel page start itself)...
rem reg add creates a missing key, so an absent protocol heals itself. What it
rem cannot do is tell us that it worked: all three calls send their output to
rem nul and their exit codes are deliberately ignored. The read-back below is
rem the authority, not reg add's status.
reg add "HKCU\Software\Classes\houmai" /ve /d "URL:houmai panel launcher" /f >nul 2>&1
reg add "HKCU\Software\Classes\houmai" /v "URL Protocol" /d "" /f >nul 2>&1
reg add "HKCU\Software\Classes\houmai\shell\open\command" /ve /d "wscript.exe \"%PANELVBS%\"" /f >nul 2>&1
rem Only what is really in the registry counts. This bit has failed silently
rem before: the key ended up missing and the panel "start" button then did
rem nothing at all when clicked - the hardest kind of failure to notice,
rem because nothing anywhere says anything.
set "PROTOBAD="
rem Never let a missing launcher pass: the path match below would then be
rem searching for an empty string, and findstr matches anything with that.
if not exist "%PANELVBS%" set "PROTOBAD=1"
rem 1) the marker Windows needs to treat houmai:// as a protocol
reg query "HKCU\Software\Classes\houmai" /v "URL Protocol" >nul 2>&1
if errorlevel 1 set "PROTOBAD=1"
rem 2) the display name Windows shows in the "open this?" prompt
reg query "HKCU\Software\Classes\houmai" /ve 2>nul | findstr /i /c:"URL:houmai panel launcher" >nul
if errorlevel 1 set "PROTOBAD=1"
rem 3) the handler must point at THIS copy of the project, so match the whole
rem    path - matching only the file name would accept a stale path left over
rem    from a moved project and report it as healthy.
reg query "HKCU\Software\Classes\houmai\shell\open\command" /ve 2>nul | findstr /i /c:"%PANELVBS%" >nul
if errorlevel 1 set "PROTOBAD=1"
if defined PROTOBAD (
  echo [WARN] the houmai:// protocol was not registered correctly.
  echo        The panel "start" button will do nothing when clicked.
  echo        Expected handler: wscript.exe "%PANELVBS%"
  echo        Workaround: use this menu, option 1, to start the panel.
) else (
  echo [OK] protocol registered and verified.
)

echo.
echo Run it once now to confirm it works:
echo     schtasks /Run /TN "%TASK%"
echo Check the result with:
echo     houmai.bat status
echo To remove the task later, run uninstall-task.cmd

:end
rem Exit non-zero when anything needs a human eye: the caller then stops and
rem lets the operator read the messages instead of closing on a timer.
rem Four places set RESULT=1: wrapper missing, launcher missing, register-task.ps1
rem failing, and PROTOBAD below.
if defined PROTOBAD set "RESULT=1"
if not defined RESULT set "RESULT=0"
rem Called from the menu with "quiet": the menu does the closing countdown.
if /i "%~1"=="quiet" exit /b %RESULT%
echo.
if not "%RESULT%"=="0" (
  echo   Something above needs your attention.
  echo   Press any key to close this window...
  pause >nul
  exit /b %RESULT%
)
echo   Closing in 5 seconds (any key closes it now)...
timeout /t 5 >nul
exit /b 0
