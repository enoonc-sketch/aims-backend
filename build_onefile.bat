@echo off
chcp 65001 >nul
setlocal
title AIMS Backend Build Onefile
cd /d "%~dp0"

echo Looking for Python 3.12 / 3.11 / 3.10 (official pre-built
echo llama-cpp-python wheels - no C++ compiler needed) via the "py" launcher...
set "PYCMD="
for %%V in (3.12 3.11 3.10) do (
    if not defined PYCMD (
        py -%%V --version >nul 2>&1
        if not errorlevel 1 set "PYCMD=py -%%V"
    )
)
if not defined PYCMD (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] No Python found. Install Python 3.11 or 3.12 from python.org.
        pause
        exit /b 1
    )
    set "PYCMD=python"
    echo [WARN] Could not find Python 3.10/3.11/3.12 via "py" - falling back to
    echo [WARN] your default "python". If that is 3.13+, the optional local-AI
    echo [WARN] packages below may fail to install - the EXE still builds fine
    echo [WARN] without them; install Python 3.11/3.12 later if you need them.
)
echo Using: %PYCMD%
%PYCMD% --version

if exist .venv (
    echo A .venv folder already exists - checking its Python version:
    .venv\Scripts\python.exe --version 2>nul
    echo ^(delete the .venv folder and re-run this script if you just switched
    echo Python versions and this looks wrong^)
)

if not exist .venv %PYCMD% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -U pip

if exist requirements-exe.lock (
    pip install -r requirements-exe.lock
) else if exist requirements-exe.txt (
    pip install -r requirements-exe.txt
) else (
    pip install "fastapi" "uvicorn" "websockets" "sqlalchemy" "pydantic" "pydantic-settings" "python-dotenv" "python-multipart" "httpx" "bcrypt>=4.1,<6" "defusedxml" "python-jose[cryptography]" "pandas" "openpyxl" "xlrd" "apscheduler==3.10.4" "pyinstaller"
)
if errorlevel 1 (
    echo [ERROR] Core pip install failed
    pause
    exit /b 1
)

echo.
REM Perf opt (report item 8): the optional AI stack (llama / sklearn / statsmodels,
REM ~240MB) is installed ONLY when AIMS_BUILD_AI=1 (full build). Unset / 0 / empty =
REM lite build (~149MB). build_exe_lite.bat is the one-click lite entry.
if not "%AIMS_BUILD_AI%"=="1" (
    echo [Lite build] Skipping optional AI packages (set AIMS_BUILD_AI=1 for the full build).
    goto SKIP_AI
)
echo Installing OPTIONAL local-AI packages (numpy / llama-cpp-python / statsmodels / scikit-learn)...
echo These are optional - if this fails, the EXE still builds and runs, it
echo just won't be able to run local GGUF models until this is fixed later
echo (use Python 3.11/3.12, or install Visual Studio Build Tools with the
echo "Desktop development with C++" workload and re-run this script).
echo --only-binary forces pip to fail fast/cleanly if no wheel matches this
echo Python version, instead of a doomed from-source CMake build.
REM Each spec is individually double-quoted INSIDE the variable value on
REM purpose: >= is a redirection metacharacter to cmd.exe anywhere it appears
REM unquoted on a command line, even after %VAR% expansion - unquoted, this
REM would silently turn "numpy>=1.26.4" into `pip install numpy` plus a stray
REM output file literally named "=1.26.4", not an error you would notice.
set AI_CORE_PKGS="numpy>=1.26.4" "scikit-learn>=1.5.2" "statsmodels>=0.14.2" "joblib>=1.4.2"
set "AI_WHEEL_INDEX=https://abetlen.github.io/llama-cpp-python/whl/cpu"
pip install --no-cache-dir --only-binary=llama-cpp-python --extra-index-url %AI_WHEEL_INDEX% "llama-cpp-python==0.3.2" %AI_CORE_PKGS%
if errorlevel 1 (
    echo       llama-cpp-python==0.3.2 has no prebuilt wheel for this Python version, trying 0.2.90 ...
    pip install --no-cache-dir --only-binary=llama-cpp-python --extra-index-url %AI_WHEEL_INDEX% "llama-cpp-python==0.2.90" %AI_CORE_PKGS%
)
if errorlevel 1 (
    echo [WARN] Optional AI packages failed to install - continuing without local LLM support.
    echo [WARN] No prebuilt llama-cpp-python wheel found for this Python version on the CPU index.
) else (
    echo       Optional AI packages installed OK.
)

:SKIP_AI
if not exist frontend.html (
    if exist "AIMS_?????????.html" copy /Y "AIMS_?????????.html" frontend.html >nul
)
if not exist frontend.html (
    for %%F in (AIMS_*.html) do copy /Y "%%F" frontend.html >nul
)

if exist build rmdir /s /q build
if exist dist\AIMS-Backend.exe del /f dist\AIMS-Backend.exe

pyinstaller --noconfirm --clean --onefile --console --name AIMS-Backend --add-data "frontend.html;." --add-data "cores/vendor;cores/vendor" --add-data "cores/manifest.json;cores" --hidden-import uvicorn --hidden-import fastapi --hidden-import starlette --hidden-import sqlalchemy --hidden-import pydantic --hidden-import pydantic_settings --hidden-import app --hidden-import app.main --hidden-import app.db.session --hidden-import app.db.init_db --hidden-import app.api.v1.router --hidden-import pandas --hidden-import openpyxl --hidden-import xlrd --exclude-module pgvector --exclude-module psycopg2 --exclude-module alembic --exclude-module watchfiles --exclude-module uvloop --exclude-module httptools --exclude-module pytest run_standalone.py

if errorlevel 1 (
    echo BUILD FAILED
    pause
    exit /b 1
)

echo OK: dist\AIMS-Backend.exe
if exist frontend.html copy /Y frontend.html dist\ >nul
pause
endlocal
