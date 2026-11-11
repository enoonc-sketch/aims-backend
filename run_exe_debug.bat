@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 开发调试模式：后端 EXE 是 Windows GUI 子系统（正式版无控制台），
rem 设置 AIMS_DEBUG_CONSOLE=1 后，EXE 会 AttachConsole 到本窗口输出日志。
set AIMS_DEBUG_CONSOLE=1
echo Running AIMS-Backend.exe in DEBUG mode (console attached)...
echo.
if not exist "AIMS-Backend.exe" (
  if exist "dist\AIMS-Backend\AIMS-Backend.exe" (
    cd dist\AIMS-Backend
  ) else if exist "dist\AIMS-Backend.exe" (
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
