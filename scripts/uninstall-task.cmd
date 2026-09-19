@echo off
setlocal
set "TASK=houmai"

echo ============================================
echo  houmai - remove scheduled task
echo ============================================
echo.

schtasks /Delete /TN "%TASK%" /F
if errorlevel 1 (
  echo.
  echo [FAIL] task "%TASK%" was not removed, or it does not exist.
) else (
  echo.
  echo [OK] task "%TASK%" removed. houmai will no longer run automatically.
  echo      the project files and local records are left untouched.
)

reg delete "HKCU\Software\Classes\houmai" /f >nul 2>&1
echo [OK] houmai:// protocol removed (if it was registered).

echo.
pause
