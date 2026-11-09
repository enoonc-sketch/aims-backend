@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Running AIMS-Backend.exe from this folder (keeps window open)...
echo.
if not exist "AIMS-Backend.exe" (
  if exist "dist\AIMS-Backend.exe" (
    cd dist
  ) else (
    echo AIMS-Backend.exe not found. Build first with build_exe.bat
    pause
    exit /b 1
  )
)
AIMS-Backend.exe
echo.
echo Exit code: %ERRORLEVEL%
if exist aims_crash.log (
  echo ---- aims_crash.log ----
  type aims_crash.log
)
pause
