@echo off
cd /d "%~dp0"
echo This window will stay open.
echo Current folder: %CD%
echo.
echo Listing files:
dir /b *.bat *.txt *.py *.spec 2>nul
echo.
echo Python:
where python
python --version
echo.
echo Now starting build_exe.bat ...
echo ============================================================
call build_exe.bat
echo ============================================================
echo build_exe.bat finished, errorlevel=%ERRORLEVEL%
echo.
pause
