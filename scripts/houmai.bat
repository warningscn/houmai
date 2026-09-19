@echo off
rem ===================================================================
rem  houmai launcher - the single entry point to the Python side.
rem
rem  This is NOT the menu. The menu is houmai.cmd in the project root;
rem  the scheduled task, the two hidden launchers and the menu all come
rem  through here, so interpreter checks live in exactly one place.
rem
rem  houmai is a watchdog, so it must not run on a runtime that ships
rem  with the thing it watches. If it did, uninstalling or moving the
rem  watched program would silently kill the watchdog - worse than no
rem  watchdog at all.
rem
rem  Every path below is derived from this file's own location, so the
rem  project can be cloned or copied anywhere, under any user name.
rem
rem  Force a specific interpreter any time with:
rem      set HOUMAI_PYTHON=C:\path\to\python.exe
rem
rem  Exit codes: whatever houmai.py returns, or 3 when no acceptable
rem  interpreter could be found (the caller must be able to tell the
rem  difference between "houmai said no" and "we never ran houmai").
rem ===================================================================
setlocal
set "HOUMAI=%~dp0.."
set "SCRIPT=%HOUMAI%\src\houmai.py"

if defined HOUMAI_PYTHON goto :explicit

rem Preferred: the official Python launcher. It only knows about real
rem Python installations, so it can never resolve to a runtime that an
rem application ships inside its own folder.
where py >nul 2>nul
if errorlevel 1 goto :fallback
py -3 --version >nul 2>nul
if errorlevel 1 goto :fallback
py -3 "%SCRIPT%" %*
exit /b %ERRORLEVEL%

:explicit
if not exist "%HOUMAI_PYTHON%" goto :badoverride
"%HOUMAI_PYTHON%" "%SCRIPT%" %*
exit /b %ERRORLEVEL%

:badoverride
echo.
echo [FAIL] HOUMAI_PYTHON is set but that file does not exist:
echo        %HOUMAI_PYTHON%
echo.
exit /b 3

:fallback
rem Known install locations, newest first. Add the one you actually use
rem if it is missing here - but do not be tempted to fall back to
rem "python from PATH": PATH frequently resolves to a runtime bundled
rem with some other application, which is exactly what we refuse.
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "%ProgramFiles%\Python313\python.exe" set "PY=%ProgramFiles%\Python313\python.exe"
if not defined PY if exist "%ProgramFiles%\Python312\python.exe" set "PY=%ProgramFiles%\Python312\python.exe"

if not defined PY goto :nopython
"%PY%" "%SCRIPT%" %*
exit /b %ERRORLEVEL%

:nopython
echo.
echo [FAIL] houmai needs an independent Python 3.9 or newer.
echo.
echo        Install Python from python.org (the "py" launcher comes
echo        with it), then run this file again. Or pin one explicitly:
echo            set HOUMAI_PYTHON=C:\path\to\python.exe
echo.
echo        houmai deliberately refuses to run on a runtime that ships
echo        with the program it watches: such a watchdog dies together
echo        with the very thing it is supposed to watch.
echo.
exit /b 3
