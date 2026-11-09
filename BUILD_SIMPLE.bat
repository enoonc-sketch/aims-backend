@echo off
cd /d "%~dp0"
echo ============================================================
echo  AIMS simple build
echo ============================================================
echo.

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
  python --version >nul 2>&1
  if errorlevel 1 (
    echo Python not found. Install Python 3.11 or 3.12 from python.org and
    echo check "Add python.exe to PATH" during install.
    pause
    exit /b 1
  )
  set "PYCMD=python"
  echo [WARN] Could not find Python 3.10/3.11/3.12 via "py" - falling back to
  echo [WARN] your default "python". If that is 3.13+, the optional local-AI
  echo [WARN] packages below may fail to install - the app still builds fine
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
python -m pip install -U pip setuptools wheel

echo.
echo Installing packages (latest compatible versions)...
pip install fastapi "uvicorn[standard]" sqlalchemy pydantic pydantic-settings python-dotenv python-multipart httpx "bcrypt>=4.1,<6" "defusedxml" "python-jose[cryptography]" "pyinstaller>=6.15.0"
if errorlevel 1 (
  echo pip install failed
  pause
  exit /b 1
)

echo.
echo Installing OPTIONAL local-AI packages (numpy / llama-cpp-python / statsmodels / scikit-learn)...
echo These are optional - if this fails, the EXE still builds and runs, it
echo just won't be able to run local GGUF models until this is fixed later
echo (use Python 3.11/3.12, or install Visual Studio Build Tools with the
echo "Desktop development with C++" workload and re-run this script).
if exist requirements-exe-ai.txt (
  pip install --prefer-binary --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu -r requirements-exe-ai.txt
  if errorlevel 1 (
    echo [WARN] Optional AI packages failed to install - continuing without local LLM support.
  )
)

if not exist frontend.html (
  for %%F in (*.html) do (
    copy /Y "%%F" frontend.html >nul
    goto :gothtml
  )
)
:gothtml

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.

echo.
echo Downloading open-source Data Analysis CORES (SheetJS / Chart.js)...
python scripts\download_cores.py
if not exist cores\vendor mkdir cores\vendor
echo.

echo Building EXE...
pyinstaller --noconfirm --clean --onedir --console --name AIMS-Backend --add-data "frontend.html;." --add-data "cores/vendor;cores/vendor" --add-data "cores/manifest.json;cores" --hidden-import app --hidden-import app.main --hidden-import app.db.session --hidden-import app.db.init_db --hidden-import app.api.v1.router --hidden-import uvicorn --hidden-import fastapi --hidden-import sqlalchemy --hidden-import pydantic_settings --exclude-module pgvector --exclude-module psycopg2 --exclude-module alembic --exclude-module pytest run_standalone.py

echo.
if exist dist\AIMS-Backend\AIMS-Backend.exe (
  echo SUCCESS: dist\AIMS-Backend\AIMS-Backend.exe
) else (
  echo FAILED - see errors above
)
pause
