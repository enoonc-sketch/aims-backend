# -*- mode: python ; coding: utf-8 -*-
"""Simplified PyInstaller spec — SQLite EXE, fewer failure points."""
import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files

block_cipher = None

# Only ASCII-named HTML to avoid encoding issues on Windows
datas = []
for name in ("frontend.html",):
    if os.path.isfile(name):
        datas.append((name, "."))
# Bundle the manifest so /api/v1/ai/cores can list downloadable models in the EXE too
if os.path.isfile("cores/manifest.json"):
    datas.append(("cores/manifest.json", "cores"))
# Bundle open-source analysis + AI cores (SheetJS / Chart.js / diagnosis_rules.json etc.),
# walking subfolders (cores/vendor/ai/*.json) instead of only the top-level files.
if os.path.isdir("cores/vendor"):
    for root, _dirs, files in os.walk("cores/vendor"):
        for fn in files:
            fp = os.path.join(root, fn)
            dest_dir = os.path.join("cores", "vendor", os.path.relpath(root, "cores/vendor"))
            dest_dir = os.path.normpath(dest_dir)
            datas.append((fp, dest_dir))

# llama-cpp-python ships a compiled shared library (llama.dll / libllama.so) as
# package data, and it (plus scikit-learn / statsmodels, used by anomaly.py /
# forecasting.py) needs numpy underneath. None of that is picked up by plain
# hiddenimports — it has to be collected explicitly, or the EXE silently loses
# local-model inference (import fails at runtime, gets caught, and the app just
# falls back to "offline AI-core mode" text with no obvious error anywhere).
binaries = []
binaries += collect_dynamic_libs("llama_cpp")
datas += collect_data_files("llama_cpp")


# Do NOT force-import optional Postgres/pgvector (they break analysis if wheels fail)
hiddenimports = [
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "h11",
    "anyio",
    "anyio._backends._asyncio",
    "sniffio",
    "httpx",
    "multipart",
    "python_multipart",
    "fastapi",
    "starlette",
    "starlette.responses",
    "starlette.routing",
    "starlette.middleware",
    "starlette.middleware.cors",
    "pydantic",
    "pydantic_settings",
    "pydantic_core",
    "sqlalchemy",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "sqlite3",
    "bcrypt",
    "defusedxml",
    "defusedxml.ElementTree",
    "jose",
    "jose.jwt",
    "app",
    "app.main",
    "app.core.config",
    "app.core.security",
    "app.db.base",
    "app.db.session",
    "app.db.init_db",
    "app.models",
    "app.models.system",
    "app.models.device",
    "app.models.measurement",
    "app.models.alarm",
    "app.models.maintenance",
    "app.models.knowledge",
    "app.models.ai",
    "app.models.analysis",
    "app.api.deps",
    "app.api.v1.router",
    "app.api.v1.auth",
    "app.api.v1.dashboard",
    "app.api.v1.devices",
    "app.api.v1.measurements",
    "app.api.v1.alarms",
    "app.api.v1.ai",
    "app.api.v1.ai_devices",
    "app.api.v1.ai_studio",
    "app.schemas.ai_studio",
    "app.schemas.device_search",
    "app.api.v1.maintenance",
    "app.api.v1.assets",
    "app.api.v1.analysis",
    "app.schemas.analysis",
    "app.api.v1.lab",
    "app.api.v1.knowledge",
    "app.api.v1.system",
    "app.services.rag",
    "app.services.ai_cores",
    "app.services.alarms_hooks",
    # Real local-model stack (added for GGUF chat/embedding + trained anomaly/
    # forecasting models) — these are only ever imported lazily inside function
    # bodies (`from app.services import llm_local`, etc.), so make them explicit
    # instead of relying on PyInstaller's static-import scan to find them.
    "app.services.llm_local",
    "app.services.model_registry",
    "app.services.anomaly",
    "app.services.forecasting",
    "app.services.system_info",
    "app.services.model_recommend",
    "app.services.table_import",
    "app.services.data_chain",
    "app.services.ai_context",
    "app.services.device_selection",
    "app.services.device_search",
    "app.services.analysis_reading",
    "app.services.roi_analysis",
    "app.services.lab_analysis",
    "app.services.ai_generate",
    "app.services.studio_data",
    "app.services.py_packages",
    "app.api.v1.gateways",
    "app.api.v1.communication",
    "app.api.v1.dashboard",
    "llama_cpp",
    "numpy",
    "sklearn",
    "sklearn.ensemble",
    "sklearn.ensemble._iforest",
    "statsmodels",
    "statsmodels.tsa.holtwinters",
    "joblib",
    # pandas + its Excel engines: every /import endpoint (measurements, devices,
    # gateways, assets, work orders, lab samples, knowledge docs, analysis) and
    # every /import/template download go through app/services/table_import.py,
    # which is pandas end to end (pd.read_csv/read_excel/ExcelWriter). openpyxl
    # is the .xlsx engine, xlrd is the legacy .xls engine — pandas picks either
    # one dynamically based on file extension, so PyInstaller's static import
    # scan won't find that reference on its own; both must be explicit here.
    "pandas",
    "openpyxl",
    "xlrd",
]

a = Analysis(
    ["run_standalone.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        # numpy is REQUIRED now — llama-cpp-python, scikit-learn and statsmodels
        # (real local LLM / anomaly / forecasting models) all depend on it.
        # Excluding it breaks `import llama_cpp` inside the EXE at runtime; that
        # failure is caught internally and silently falls back to canned
        # offline-mode chat replies, with no visible error anywhere in the UI.
        #
        # pandas is REQUIRED too — every /import and /import/template endpoint
        # (measurements, devices, gateways, assets, work orders, lab samples,
        # knowledge docs, analysis) depends on it via app/services/table_import.py.
        # It used to be excluded here, which made every one of those endpoints
        # crash with "ModuleNotFoundError: No module named 'pandas'" inside the
        # built EXE specifically — pip-installing it on the host machine does
        # nothing for an already-built executable; do not add pandas back here.
        "pytest",
        "alembic",
        "asyncpg",
        "psycopg2",
        "psycopg2_binary",
        "pgvector",
        "torch",
        "tensorflow",
        "IPython",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# onedir is more reliable than onefile on Windows (less antivirus false-positive)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AIMS-Backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AIMS-Backend",
)
