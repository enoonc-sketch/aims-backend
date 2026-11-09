"""Runtime installer for the optional local-AI Python stack (llama-cpp-python
in its CPU/CUDA variants, numpy, scikit-learn, statsmodels) — lets AI Center
install these into the running backend's own environment with one click,
instead of only ever being able to set them up by editing
requirements-exe-ai.txt and re-running build_exe.bat / build_onefile.bat.

Only works when the backend is running as plain Python (dev server, Docker,
`python run_standalone.py`, etc.) — see is_frozen(). A PyInstaller-frozen EXE
has no pip/site-packages to install into at runtime; for that deployment the
package list still needs to be baked in at build time, so install_package()
refuses early with a clear message pointing at the build scripts instead of
failing confusingly deep inside a subprocess call.
"""
from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from typing import Optional

# Curated catalog: multiple llama-cpp-python variants (all satisfy the same
# `import llama_cpp`, so only one is really "installed" at a time — picking a
# different one just reinstalls over whatever's there) plus the other
# optional local-AI packages the app can use if present.
#
# llama-cpp-python entries list several known-good pinned `versions` instead
# of a single version or an open ">=" floor: it's a compiled package with no
# wheel for every Python version on this community index, and an open floor
# lets pip resolve to "whatever's newest on PyPI" even when that exact
# version has no (or a broken) wheel here — which either grabs a corrupt
# wheel or, worse, silently falls back to compiling from source via CMake,
# which fails on any machine without Visual Studio's C++ build tools. Every
# install attempt below is also forced `only_binary=True`, so a version with
# no matching wheel fails fast and clean instead of attempting that doomed
# source build — see install_package().
PACKAGES: list[dict] = [
    {
        "id": "llama-cpu",
        "name": "本地大模型推理引擎（CPU 版）",
        "name_en": "Local LLM inference engine (CPU)",
        "description": "运行「模型下载」页下载的 GGUF 模型（聊天/向量）所需。纯 CPU 推理，兼容性最好，无需显卡。",
        "description_en": "Needed to run GGUF models downloaded in the Models tab. Pure CPU inference — works everywhere, no GPU needed.",
        "import_name": "llama_cpp",
        "versions": ["0.3.4", "0.3.2", "0.2.90"],
        "extra_index_url": "https://abetlen.github.io/llama-cpp-python/whl/cpu",
        "only_binary": True,
    },
    {
        "id": "llama-cuda121",
        "name": "本地大模型推理引擎（NVIDIA CUDA 12.1 版）",
        "name_en": "Local LLM inference engine (NVIDIA CUDA 12.1)",
        "description": "如果有 NVIDIA 显卡并已安装匹配的 CUDA 12.1 驱动，推理速度比 CPU 版快很多。仅支持 Python 3.10/3.11/3.12。",
        "description_en": "Much faster than the CPU build if you have an NVIDIA GPU with a matching CUDA 12.1 driver. Python 3.10/3.11/3.12 only.",
        "import_name": "llama_cpp",
        "versions": ["0.3.4", "0.2.90"],
        "extra_index_url": "https://abetlen.github.io/llama-cpp-python/whl/cu121",
        "only_binary": True,
    },
    {
        "id": "llama-cuda124",
        "name": "本地大模型推理引擎（NVIDIA CUDA 12.4 版）",
        "name_en": "Local LLM inference engine (NVIDIA CUDA 12.4)",
        "description": "适用于较新驱动的 NVIDIA 显卡（CUDA 12.4）。仅支持 Python 3.10/3.11/3.12。",
        "description_en": "For newer NVIDIA drivers (CUDA 12.4). Python 3.10/3.11/3.12 only.",
        "import_name": "llama_cpp",
        "versions": ["0.3.4"],
        "extra_index_url": "https://abetlen.github.io/llama-cpp-python/whl/cu124",
        "only_binary": True,
    },
    {
        "id": "numpy",
        "name": "NumPy 数值计算库",
        "name_en": "NumPy",
        "description": "本地推理引擎、异常检测、趋势预测都依赖它，通常会随上面任一推理引擎一起自动装上。",
        "description_en": "Required underneath the inference engine, anomaly detection, and forecasting. Usually pulled in automatically by any engine above.",
        "import_name": "numpy",
        "pip_args": ["numpy>=1.26.4"],
    },
    {
        "id": "sklearn",
        "name": "scikit-learn（设备异常检测）",
        "name_en": "scikit-learn (anomaly detection)",
        "description": "AI 中心「诊断」页的统计异常检测模型使用。",
        "description_en": "Used by the Diagnosis tab's statistical anomaly-detection model.",
        "import_name": "sklearn",
        "pip_args": ["scikit-learn>=1.5.2"],
    },
    {
        "id": "statsmodels",
        "name": "statsmodels（趋势预测）",
        "name_en": "statsmodels (trend forecasting)",
        "description": "AI 中心「预测」页的剩余寿命/趋势预测模型使用。",
        "description_en": "Used by the Prediction tab's remaining-useful-life / trend forecasting model.",
        "import_name": "statsmodels",
        "pip_args": ["statsmodels>=0.14.2"],
    },
]

_BY_ID = {p["id"]: p for p in PACKAGES}

# Generous but bounded: a CUDA wheel can be several hundred MB on a slow link.
INSTALL_TIMEOUT_SECONDS = 900


def is_frozen() -> bool:
    """True inside a PyInstaller-built EXE (no pip / site-packages to install into)."""
    return bool(getattr(sys, "frozen", False))


def is_installed(import_name: str) -> tuple[bool, Optional[str]]:
    """(installed, version_or_None). `importlib.util.find_spec` alone would
    report modules already cached in sys.modules from an earlier failed
    partial install as "found"; invalidate_caches() first so this reflects
    what's on disk right now, not stale in-process state."""
    importlib.invalidate_caches()
    spec = importlib.util.find_spec(import_name)
    if spec is None:
        return False, None
    try:
        mod = importlib.import_module(import_name)
        return True, getattr(mod, "__version__", None)
    except Exception:
        # present on disk but broken/partial — still "installed" for display
        # purposes; the install action will overwrite it with a clean copy.
        return True, None


def list_packages() -> list[dict]:
    out = []
    for p in PACKAGES:
        installed, version = is_installed(p["import_name"])
        out.append({**{k: v for k, v in p.items() if k != "pip_args"}, "installed": installed, "version": version})
    return out


class PackageInstallError(Exception):
    pass


def _run_pip(args: list[str]) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--prefer-binary", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS)


def _externally_managed_message(pip_args: list[str]) -> str:
    return (
        "安装失败：当前 Python 环境受系统保护，不允许直接装包（PEP 668，常见于 Linux 系统自带的 "
        "Python，而不是虚拟环境）。请在虚拟环境里运行本服务后再试，"
        "例如 `python -m venv .venv && .venv/bin/activate` 之后重新启动后端；"
        "或者（了解风险的前提下）手动执行 "
        f"`{sys.executable} -m pip install --break-system-packages {' '.join(pip_args)}`。"
    )


def install_package(package_id: str) -> dict:
    """Runs `python -m pip install` in THIS process's own interpreter/
    environment, so a package installed here is importable by the rest of
    the app immediately — no restart needed for pure-Python packages; a
    restart is still recommended for compiled ones (llama-cpp-python ships a
    native DLL/.so) since Windows can be picky about loading a brand new
    native library into a process that's already running.

    Packages with a `versions` list (the llama-cpp-python variants) try each
    pinned version in order against their extra_index_url, with
    `--only-binary` forcing every attempt to fail fast and cleanly if no
    matching wheel exists — instead of silently falling back to a from-source
    CMake build that needs a C++ toolchain most machines don't have. Only
    once every listed version has failed to find a wheel is the whole
    install reported as failed.
    """
    pkg = _BY_ID.get(package_id)
    if pkg is None:
        raise PackageInstallError(f"未知安装包: {package_id}")

    if is_frozen():
        raise PackageInstallError(
            "当前运行的是打包后的 EXE，无法在应用内安装新的 Python 包（没有 pip 环境可装）。"
            "请在项目源码里把对应依赖加入 requirements-exe-ai.txt，然后重新运行 "
            "build_exe.bat 或 build_onefile.bat 重新打包。"
        )

    if "versions" in pkg:
        attempts: list[tuple[str, list[str], subprocess.CompletedProcess]] = []
        result = None
        for version in pkg["versions"]:
            pip_args = [
                f"{pkg['import_name'].replace('_', '-')}=={version}"
                if pkg["import_name"] != "llama_cpp"
                else f"llama-cpp-python=={version}",
                "--extra-index-url",
                pkg["extra_index_url"],
            ]
            if pkg.get("only_binary"):
                pip_args = ["--only-binary=llama-cpp-python", *pip_args]
            try:
                result = _run_pip(pip_args)
            except subprocess.TimeoutExpired:
                raise PackageInstallError(f"安装超时（超过 {INSTALL_TIMEOUT_SECONDS // 60} 分钟），请检查网络后重试。")
            except Exception as e:
                raise PackageInstallError(f"启动安装进程失败: {e}")
            attempts.append((version, pip_args, result))
            if result.returncode == 0:
                break

        assert result is not None
        if result.returncode != 0:
            last_log = "\n".join(filter(None, [result.stdout[-2000:] if result.stdout else "", result.stderr[-2000:] if result.stderr else ""]))
            if "externally-managed-environment" in last_log:
                raise PackageInstallError(_externally_managed_message(attempts[-1][1]))
            tried = ", ".join(v for v, _, _ in attempts)
            raise PackageInstallError(
                f"安装失败：已尝试版本 {tried}，均未找到适用于当前 Python 版本的预编译 wheel。"
                f"建议改用 Python 3.11 或 3.12 重新运行本服务后再试。\n\n最后一次尝试的输出：\n{last_log}"
            )
        installed, version = is_installed(pkg["import_name"])
        return {"package_id": package_id, "installed": installed, "version": version, "log_tail": result.stdout[-4000:] + result.stderr[-4000:]}

    # Simple packages (numpy/sklearn/statsmodels): a single, plain pip_args list.
    try:
        result = _run_pip(pkg["pip_args"])
    except subprocess.TimeoutExpired:
        raise PackageInstallError(f"安装超时（超过 {INSTALL_TIMEOUT_SECONDS // 60} 分钟），请检查网络后重试。")
    except Exception as e:
        raise PackageInstallError(f"启动安装进程失败: {e}")

    log_tail = "\n".join(filter(None, [result.stdout[-4000:] if result.stdout else "", result.stderr[-4000:] if result.stderr else ""]))
    if result.returncode != 0:
        if "externally-managed-environment" in log_tail:
            raise PackageInstallError(_externally_managed_message(pkg["pip_args"]))
        raise PackageInstallError(f"安装失败（pip 退出码 {result.returncode}）：\n{log_tail}")

    installed, version = is_installed(pkg["import_name"])
    return {"package_id": package_id, "installed": installed, "version": version, "log_tail": log_tail}
