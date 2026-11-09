"""Best-effort detection of the machine's RAM / CPU / GPU, used to power the
"real-time model recommendation" panel on the AI Center -> 模型下载 page.

No new dependency is added (no psutil) — this reads /proc/meminfo on Linux,
uses the Win32 API via ctypes on Windows, and falls back to `sysctl` on macOS.
Every path degrades to a conservative guess rather than raising, since this
only ever feeds a *recommendation*, never a hard requirement.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from typing import Any

logger = logging.getLogger("aims.system_info")

# Used when detection fails entirely, so the recommendation logic always has
# *something* to work with (and callers can see detected=False and say so).
_FALLBACK_TOTAL_RAM_GB = 8.0
_FALLBACK_AVAILABLE_RAM_GB = 4.0


def _read_linux_meminfo() -> tuple[float, float] | None:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split(":", 1)
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                val = parts[1].strip().split()[0]  # "12345 kB" -> "12345"
                if key in ("MemTotal", "MemAvailable", "MemFree", "Cached", "Buffers"):
                    info[key] = int(val)
        if "MemTotal" not in info:
            return None
        total_kb = info["MemTotal"]
        # MemAvailable (kernel >= 3.14) is the accurate "usable without swapping"
        # figure; fall back to Free+Cached+Buffers on ancient kernels that lack it.
        available_kb = info.get(
            "MemAvailable", info.get("MemFree", 0) + info.get("Cached", 0) + info.get("Buffers", 0)
        )
        return total_kb / 1e6, available_kb / 1e6  # kB -> GB (decimal, matches vendor sizing)
    except Exception:
        logger.exception("Failed reading /proc/meminfo")
        return None


def _read_windows_meminfo() -> tuple[float, float] | None:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            return None
        return stat.ullTotalPhys / 1e9, stat.ullAvailPhys / 1e9
    except Exception:
        logger.exception("Failed reading Windows memory status")
        return None


def _read_macos_meminfo() -> tuple[float, float] | None:
    try:
        total_out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3
        )
        total_bytes = int(total_out.stdout.strip())
        vm_out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3)
        page_size = 4096
        free_pages = 0
        for line in vm_out.stdout.splitlines():
            if line.startswith("Mach Virtual Memory Statistics"):
                continue
            if "page size of" in line:
                try:
                    page_size = int(line.split("page size of")[1].strip().split()[0])
                except Exception:
                    pass
            for label in ("Pages free", "Pages inactive"):
                if line.startswith(label):
                    try:
                        free_pages += int(line.split(":")[1].strip().rstrip("."))
                    except Exception:
                        pass
        available_bytes = free_pages * page_size
        return total_bytes / 1e9, (available_bytes / 1e9) or (total_bytes / 1e9 * 0.4)
    except Exception:
        logger.exception("Failed reading macOS memory status")
        return None


def _detect_gpu() -> tuple[bool, str]:
    """Best-effort only — false negatives are fine (worst case we under-recommend
    a slightly smaller model than the GPU could actually handle)."""
    if shutil.which("nvidia-smi"):
        return True, "nvidia-smi"
    if platform.system() == "Darwin" and platform.machine() in ("arm64",):
        return True, "apple-silicon"
    # Windows: a lightweight, best-effort WMIC/dxdiag-free check is not reliable
    # without extra deps, so we don't guess further here — CPU-only is the safe
    # default absent stronger evidence.
    return False, ""


def get_system_info() -> dict[str, Any]:
    system = platform.system()  # 'Linux' | 'Windows' | 'Darwin'
    mem: tuple[float, float] | None = None
    if system == "Linux":
        mem = _read_linux_meminfo()
    elif system == "Windows":
        mem = _read_windows_meminfo()
    elif system == "Darwin":
        mem = _read_macos_meminfo()

    detected = mem is not None
    total_gb, available_gb = mem if mem else (_FALLBACK_TOTAL_RAM_GB, _FALLBACK_AVAILABLE_RAM_GB)

    gpu_detected, gpu_source = _detect_gpu()
    cpu_cores = os.cpu_count() or 4

    return {
        "platform": system or "unknown",
        "total_ram_gb": round(total_gb, 1),
        "available_ram_gb": round(available_gb, 1),
        "cpu_cores": cpu_cores,
        "gpu_detected": gpu_detected,
        "gpu_source": gpu_source or None,
        "ram_detected": detected,
    }
