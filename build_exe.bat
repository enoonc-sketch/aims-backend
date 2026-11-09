@echo off
cd /d "%~dp0"
echo ============================================================
echo  AIMS Backend - Build EXE
echo ============================================================
echo Working dir: %CD%
echo.

echo Looking for Python 3.12 / 3.11 / 3.10 (these have official pre-built
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
        echo [ERROR] No Python found in PATH, and no matching version via "py".
        echo Install Python 3.11 or 3.12 from python.org, then check the box
        echo "Add python.exe to PATH" during install, then re-run this script.
        goto END
    )
    set "PYCMD=python"
    echo [WARN] Could not find Python 3.10/3.11/3.12 via the "py" launcher.
    echo [WARN] Falling back to your default "python". If that is 3.13 or
    echo [WARN] newer, the optional local-AI packages further down may fail
    echo [WARN] to install - that's OK, the app still builds and runs fine
    echo [WARN] without them ^(see requirements-exe-ai.txt^). To use local
    echo [WARN] LLM features, install Python 3.11 or 3.12 and re-run this
    echo [WARN] script so it's picked up automatically.
)

echo Using: %PYCMD%
%PYCMD% --version
echo.

if exist .venv (
    echo A .venv folder already exists - checking its Python version:
    .venv\Scripts\python.exe --version 2>nul
    echo ^(If you just switched Python versions and this looks wrong, delete
    echo the .venv folder and re-run this script to recreate it.^)
    echo.
)

echo [1/4] Create venv...
if not exist .venv (
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] venv failed
        goto END
    )
)
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] activate venv failed
    goto END
)

echo.
echo [2/4] Install pip packages (core, required)...
python -m pip install -U pip setuptools wheel
if exist requirements-exe.lock (
    pip install -r requirements-exe.lock
) else if exist requirements-exe.txt (
    pip install -r requirements-exe.txt
) else (
    pip install fastapi "uvicorn" "websockets" sqlalchemy pydantic pydantic-settings python-dotenv python-multipart httpx "bcrypt>=4.1,<6" "defusedxml" "python-jose[cryptography]" "apscheduler==3.10.4" "pyinstaller>=6.15.0"
)
if errorlevel 1 (
    echo [ERROR] Core pip install failed
    goto END
)

echo.
REM Perf opt (report item 8): AIMS_BUILD_AI=0 skips the optional AI stack
REM (llama / sklearn / statsmodels, ~240MB) for a ~149MB lite EXE.
REM Default is the full build; see build_exe_lite.bat.
if "%AIMS_BUILD_AI%"=="0" (
    echo [Lite build] Skipping optional AI packages (AIMS_BUILD_AI=0).
    goto SKIP_AI
)
echo Installing OPTIONAL local-AI packages (numpy / llama-cpp-python / statsmodels / scikit-learn)...
echo These enable running downloaded GGUF models fully offline. llama-cpp-python
echo is a compiled package with no pre-built wheel for every Python version on
echo the community CPU wheel index below - --only-binary forces pip to fail
echo FAST and CLEANLY if no matching wheel exists, instead of falling back to a
echo from-source CMake build that needs Visual Studio's C++ build tools and
echo fails with a confusing wall of CMake errors. Several known-good versions
echo are tried in order in case the newest one lacks a wheel for this Python
echo version. This whole step is optional: if all of them fail, the EXE will
echo still be built and will run fine - local LLM features just won't be
echo available until you rebuild with Python 3.11/3.12 (best wheel coverage).
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
    echo [WARN] Fix: rebuild using Python 3.11 or 3.12 ^(best wheel coverage^), or install Visual
    echo [WARN] Studio Build Tools "Desktop development with C++" and re-run this script.
) else (
    echo       Optional AI packages installed OK.
)

echo.
:SKIP_AI
echo [3/4] Check frontend.html...
if not exist frontend.html (
    for %%F in (*.html) do (
        copy /Y "%%F" frontend.html >nul
        goto :html_ok
    )
)
:html_ok
if exist frontend.html (echo frontend.html OK) else (echo [WARN] no frontend.html)

echo.

echo.
echo Downloading open-source Data Analysis CORES (SheetJS / Chart.js)...
python scripts\download_cores.py
if not exist cores\vendor mkdir cores\vendor
echo.

echo [4/4] PyInstaller...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

if exist build_exe.spec (
    pyinstaller --noconfirm --clean build_exe.spec
) else (
    goto CLI
)
if errorlevel 1 goto CLI
goto OK

:CLI
echo Using command-line build...
pyinstaller --noconfirm --clean --onedir --console --name AIMS-Backend --add-data "frontend.html;." --add-data "cores/vendor;cores/vendor" --add-data "cores/manifest.json;cores" --hidden-import uvicorn --hidden-import fastapi --hidden-import starlette --hidden-import sqlalchemy --hidden-import pydantic --hidden-import pydantic_settings --hidden-import app --hidden-import app.main --hidden-import app.db.session --hidden-import app.db.init_db --hidden-import app.api.v1.router --hidden-import pandas --hidden-import openpyxl --hidden-import xlrd --exclude-module pgvector --exclude-module psycopg2 --exclude-module alembic --exclude-module watchfiles --exclude-module uvloop --exclude-module httptools --exclude-module pytest run_standalone.py
if errorlevel 1 (
    echo.
    echo BUILD FAILED - copy the error above
    goto END
)

:OK
echo.
echo ============================================================
echo  BUILD OK
echo  Run: dist\AIMS-Backend\AIMS-Backend.exe
echo  URL: http://127.0.0.1:8000/ui
echo  Login: admin / admin123
echo ============================================================
goto END

:END
echo.
echo Press any key to close...
pause >nul
