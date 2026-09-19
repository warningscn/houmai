@echo off
setlocal
cd /d "%~dp0"
set "DETAIL=%~dp0state\install.log"

rem Keep this log from growing forever: rotate before writing a new section.
rem The watcher rotates state\run.log by the same rule (512 KB) but that runs in
rem Python when the watcher writes, and it knows nothing about this file. Keep
rem one older copy. The "if exist" is not decoration: %%~zF expands to nothing
rem for a missing file, and "if  GTR 524288" would then abort the whole script.
if exist "%DETAIL%" for %%F in ("%DETAIL%") do if %%~zF GTR 524288 move /y "%DETAIL%" "%DETAIL%.1" >nul 2>&1

:menu
cls
echo ============================================
echo  houmai
echo ============================================
echo.
echo   [1] Start panel (background, opens browser)
echo   [2] Stop panel
echo   [3] Register watcher task (install / repair) then start panel
echo   [0] Close this window (do nothing)
echo.
choice /c 1230 /n /m "  Select: "
if errorlevel 4 goto :eof
if errorlevel 3 goto reg
if errorlevel 2 goto stop
if errorlevel 1 goto bg

:bg
rem Nothing to capture from "start" (it returns at once, and redirecting it
rem would hand our temp file to the panel process it launches). Just give
rem :flush an empty file so the log gets a timestamped section header.
set "TEEF=%TEMP%\houmai-tee-%RANDOM%.log"
type nul > "%TEEF%"
start "" wscript.exe "scripts\panel-hidden.vbs"
set "RC=%ERRORLEVEL%"
call :flush "option 1 - start panel"
echo.
rem "start" only delivers the command; it returns success even if the launcher
rem is missing. So this line must not promise that the panel came up.
echo   Starting the panel in the background; the browser should open shortly.
echo   Full log: %DETAIL%
echo   Closing in 5 seconds (any key closes it now)...
timeout /t 5 >nul
exit /b 0

:reg
rem "quiet" tells install-task.cmd to skip its own closing wait, so the window
rem closes once, not twice. It still reports success through its exit code -
rem capture that on the very next line: echo and start would overwrite it.
set "TEEF=%TEMP%\houmai-tee-%RANDOM%.log"
call scripts\install-task.cmd quiet > "%TEEF%" 2>&1
set "RC=%ERRORLEVEL%"
set "REGFAIL=%RC%"
call :flush "option 3 - register watcher task"
echo.
rem Report the registration result BEFORE saying anything about the panel: the
rem old order claimed "Panel started" first and only then admitted registration
rem had failed, which reads like good news followed by an afterthought. The
rem panel is still started either way (you need it to see the health card), so
rem the wording is "starting" - not "started".
if not "%REGFAIL%"=="0" echo   Registration reported a problem - see the messages above.
start "" wscript.exe "scripts\panel-hidden.vbs"
echo   Starting the panel in the background; the browser should open shortly.
echo   Full log: %DETAIL%
if not "%REGFAIL%"=="0" (
  echo   Press any key to close this window...
  pause >nul
  exit /b 0
)
echo   Closing in 5 seconds (any key closes it now)...
timeout /t 5 >nul
exit /b 0

:stop
set "TEEF=%TEMP%\houmai-tee-%RANDOM%.log"
rem The launcher lives inside the repo and derives everything from its own
rem location, so this works no matter where the project sits or who runs it.
call "%~dp0scripts\houmai.bat" stop > "%TEEF%" 2>&1
set "RC=%ERRORLEVEL%"
call :flush "option 2 - stop panel"
echo.
rem Three outcomes, and they are not the same thing: stopped / nothing was
rem running (not a failure) / could not be confirmed. Only the first two may
rem auto close - "could not stop it" has to stay on screen (AGENTS: no timed
rem exit on problems; and never report "nothing to do" as a failure).
if "%RC%"=="0" echo   Panel stopped. The watcher is not affected.
if "%RC%"=="1" echo   No panel was running - nothing to stop. The watcher is not affected.
echo   Full log: %DETAIL%
if not "%RC%"=="0" if not "%RC%"=="1" (
  echo   Stop did NOT confirm - the panel may still be running.
  echo   Press any key to close this window...
  pause >nul
  exit /b 0
)
echo   Closing in 5 seconds (any key closes it now)...
timeout /t 5 >nul
exit /b 0

:flush
rem Show the captured output and keep a copy: the screen is a one-shot, the log
rem is not. Called as: call :flush "<label>", with %TEEF% and %RC% already set.
rem %RC% must be read before type/del - both would overwrite %ERRORLEVEL%.
echo.>> "%DETAIL%"
echo ============================================>> "%DETAIL%"
echo %DATE% %TIME%   [menu] %~1>> "%DETAIL%"
echo ============================================>> "%DETAIL%"
type "%TEEF%"
type "%TEEF%">> "%DETAIL%"
if not "%RC%"=="0" echo --- exit code %RC% --->> "%DETAIL%"
del "%TEEF%" >nul 2>&1
exit /b %RC%
