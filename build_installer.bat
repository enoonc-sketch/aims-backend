@echo off
REM ============================================================================
REM  AIMS Backend — 本地打安装包（Windows）
REM  先调 build_exe.bat 打出 dist\AIMS-Backend，再用 Inno Setup 6 的
REM  ISCC.exe 编译 installer\AIMS-Backend.iss，产出
REM  installer_output\AIMS-Backend-Setup.exe。
REM  需要本机装好 Inno Setup 6（https://jrsoftware.org/isdl.php，
REM  或 choco install innosetup）。
REM ============================================================================
cd /d "%~dp0"
call "%~dp0build_exe.bat"
if not exist "dist\AIMS-Backend\AIMS-Backend.exe" (
    echo [ERROR] dist\AIMS-Backend\AIMS-Backend.exe not found, build_exe.bat failed?
    pause
    exit /b 1
)
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo [ERROR] Inno Setup 6 (ISCC.exe) not found.
    echo Install it from https://jrsoftware.org/isdl.php or: choco install innosetup
    pause
    exit /b 1
)
echo Building installer with "%ISCC%" ...
"%ISCC%" "%~dp0installer\AIMS-Backend.iss"
if errorlevel 1 (
    echo [ERROR] ISCC failed
    pause
    exit /b 1
)
echo.
echo ============================================================
echo  INSTALLER OK
echo  installer_output\AIMS-Backend-Setup.exe
echo ============================================================
pause
