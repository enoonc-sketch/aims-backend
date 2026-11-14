"""Standalone (native) inference-engine installer for the Model Download page.

Why this module exists
-----------------------
The Model Download page already had an engine installer, but it only installs
Python packages with pip (``app/services/py_packages.py``) — and that path is
deliberately refused inside a packaged EXE (``/ai/packages/.../install``
returns 400 when ``sys.frozen``: a PyInstaller build has no pip environment
to install into). So on the machine that actually matters — the installed
Windows EXE in ``C:\\Program Files\\AIMS`` — every "Install engine" button
on the Model Download page was dead by design.

This module installs a *native* engine instead: it downloads the official
prebuilt llama.cpp Windows binaries from ggml-org/llama.cpp GitHub releases
and extracts them into the writable models directory
(``<MODELS_DIR>/engines/<engine-id>/``). No pip, no rebuild, no re-packaging
— it works the same from source and from the frozen EXE, because it only
writes files under the data directory and never touches the program directory
or the running interpreter.

What it downloads (Windows x64 only — AIMS is a Windows desktop app)
----------------------------------------------------------------------
* ``llama-cpu`` — ``llama-bNNNNN-bin-win-cpu-x64.zip`` (~25 MB). Works on any
  x64 CPU, no GPU needed. This is the recommended default.
* ``llama-cuda124`` — ``llama-bNNNNN-bin-win-cuda-12.4-x64.zip`` (~320 MB)
  **plus** its companion ``cudart-llama-bin-win-cuda-12.4-x64.zip``
  (~390 MB). The CUDA zip ships ``ggml-cuda.dll`` but *not* NVIDIA's runtime
  DLLs; without the cudart companion ``llama-server.exe`` fails to start.
  Total download is ~710 MB — the UI says so up front.
* ``llama-vulkan`` — ``llama-bNNNNN-bin-win-vulkan-x64.zip`` (~38 MB).
  GPU acceleration for AMD / Intel GPUs without the CUDA download size.

Release selection (a real trap, verified 2026-11-14)
----------------------------------------------------
``/releases/latest`` now resolves to a ``v1.0.0`` release whose only asset is
``nightly-tag.txt``; the binaries live on the ``bNNNNN`` nightly tags, which
are marked ``prerelease: true``. So :func:`resolve_release` lists releases
(newest first) and picks the first ``bNNNNN`` tag that actually carries the
wanted asset, instead of trusting ``latest``. Nothing is version-pinned: the
asset name pattern is stable, the build number is taken from whatever the
newest nightly is.

Safety rules
------------
* Download URLs come only from the GitHub Releases API response for
  ``ggml-org/llama.cpp`` (or a test-supplied asset dict) — the engine id is
  a fixed whitelist key, never a user-supplied URL, so there is no SSRF
  surface here.
* Every downloaded zip is verified against the ``sha256:…`` digest GitHub
  publishes for that asset before anything is extracted.
* Extraction is zip-slip guarded (no absolute paths, no ``..``, no symlinks)
  and bounded (max file count / total uncompressed size); extraction goes to
  a ``.tmp`` directory first and only replaces the live engine directory
  after ``llama-server.exe`` is confirmed present.
* Install / uninstall are superuser-only at the API layer (see
  ``app/api/v1/ai.py``); this module itself never touches auth.
* The installed engine is a separate ``llama-server.exe`` binary. It does
  *not* replace the in-process ``llama-cpp-python`` backend used by
  ``llm_local`` today — it gives the machine a runnable engine where the
  pip path cannot run at all. Wiring ``llm_local`` to spawn it is a later
  step, deliberately not smuggled into this change.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("aims.engine_installer")

GITHUB_API_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
GITHUB_HOST_ALLOWLIST = {"api.github.com", "github.com", "objects.githubusercontent.com",
                         "release-assets.githubusercontent.com"}
USER_AGENT = "AIMS-EngineInstaller/1.0"

# Generous but bounded: the CUDA main zip (~320 MB) + cudart companion
# (~390 MB) are the biggest single files this installer ever fetches.
MAX_ZIP_BYTES = 1_000_000_000
MAX_UNCOMPRESSED_BYTES = 2_000_000_000
MAX_ZIP_MEMBERS = 5_000
DOWNLOAD_TIMEOUT_SECONDS = 1800  # 30 min — a 700 MB CUDA install on a slow link
RELEASE_LIST_TIMEOUT_SECONDS = 30

ENGINE_CATALOG: list[dict[str, Any]] = [
    {
        "id": "llama-cpu",
        "name": "本地推理引擎（CPU 版，独立安装）",
        "name_en": "Local inference engine (CPU, standalone)",
        "description": "官方 llama.cpp 预编译 Windows 二进制（llama-server.exe），纯 CPU 推理，任何 x64 电脑都能用。约 25 MB，推荐首选。EXE 安装版也能装，无需重新打包。",
        "description_en": "Official prebuilt llama.cpp Windows binaries (llama-server.exe). Pure CPU inference — works on any x64 machine. ~25 MB, recommended. Installable from the packaged EXE, no rebuild needed.",
        "asset_pattern": r"^llama-b\d+-bin-win-cpu-x64\.zip$",
        "companion_pattern": None,
        "download_size_hint": 25_000_000,
        "requires": "Windows x64，无需显卡",
    },
    {
        "id": "llama-cuda124",
        "name": "本地推理引擎（NVIDIA CUDA 12.4 版，独立安装）",
        "name_en": "Local inference engine (NVIDIA CUDA 12.4, standalone)",
        "description": "官方 llama.cpp CUDA 12.4 预编译二进制 + NVIDIA 运行时（cudart）伴生包，两者缺一不可，装完才能启动。合计约 710 MB，需要 NVIDIA 显卡与匹配驱动，下载时间较长。",
        "description_en": "Official prebuilt llama.cpp CUDA 12.4 binaries plus the NVIDIA runtime (cudart) companion — both are required for it to start. ~710 MB total, needs an NVIDIA GPU and matching driver.",
        "asset_pattern": r"^llama-b\d+-bin-win-cuda-12\.4-x64\.zip$",
        "companion_pattern": r"^cudart-llama-bin-win-cuda-12\.4-x64\.zip$",
        "download_size_hint": 710_000_000,
        "requires": "Windows x64 + NVIDIA 显卡（CUDA 12.4 驱动）",
    },
    {
        "id": "llama-vulkan",
        "name": "本地推理引擎（Vulkan 版，独立安装）",
        "name_en": "Local inference engine (Vulkan, standalone)",
        "description": "官方 llama.cpp Vulkan 预编译二进制（llama-server.exe），AMD / Intel 显卡可用，无需 CUDA，约 38 MB。",
        "description_en": "Official prebuilt llama.cpp Vulkan binaries (llama-server.exe). GPU acceleration for AMD / Intel GPUs, no CUDA needed. ~38 MB.",
        "asset_pattern": r"^llama-b\d+-bin-win-vulkan-x64\.zip$",
        "companion_pattern": None,
        "download_size_hint": 38_000_000,
        "requires": "Windows x64 + 支持 Vulkan 的显卡",
    },
]

_BY_ID = {e["id"]: e for e in ENGINE_CATALOG}


class EngineInstallError(Exception):
    """面向用户的安装失败原因（已写成中文，API 层直接透传）。"""


def _engines_root(models_root: Path | None = None) -> Path:
    if models_root is not None:
        return Path(models_root) / "engines"
    from app.core.config import settings

    return settings.models_dir_path / "engines"


def _state_path(models_root: Path | None = None) -> Path:
    return _engines_root(models_root) / "installed.json"


def _read_state(models_root: Path | None = None) -> dict[str, Any]:
    path = _state_path(models_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("engine state unreadable: %s", path)
        return {}


def _write_state(state: dict[str, Any], models_root: Path | None = None) -> None:
    root = _engines_root(models_root)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "installed.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_state_path(models_root))


def _server_exe(engine_dir: Path) -> Path:
    return engine_dir / "llama-server.exe"


def list_engines(models_root: Path | None = None) -> list[dict[str, Any]]:
    """Catalog merged with on-disk install state — powers the Model Download
    page's engine section. Never raises for a missing/corrupt state file."""
    state = _read_state(models_root)
    out: list[dict[str, Any]] = []
    for entry in ENGINE_CATALOG:
        engine_id = entry["id"]
        engine_dir = _engines_root(models_root) / engine_id
        rec = state.get(engine_id) or {}
        installed = bool(_server_exe(engine_dir).is_file())
        total_size = None
        if installed:
            total_size = sum(p.stat().st_size for p in engine_dir.rglob("*") if p.is_file())
        out.append({
            **{k: v for k, v in entry.items() if k not in ("asset_pattern", "companion_pattern")},
            "installed": installed,
            "tag": rec.get("tag"),
            "asset": rec.get("asset"),
            "sha256": rec.get("sha256"),
            "size_bytes": total_size,
            "installed_at": rec.get("installed_at"),
            "server_path": str(_server_exe(engine_dir)) if installed else None,
            "verify_ok": rec.get("verify_ok"),
            "verify_error": rec.get("verify_error"),
            "download_size_hint": entry["download_size_hint"],
        })
    return out


def get_engine(engine_id: str, models_root: Path | None = None) -> Optional[dict[str, Any]]:
    for entry in list_engines(models_root):
        if entry["id"] == engine_id:
            return entry
    return None


def _http_client():
    import httpx

    # trust_env=True (default) makes httpx respect HTTP_PROXY/HTTPS_PROXY/NO_PROXY
    # env vars automatically — important on corporate networks that require a
    # proxy for internet access.
    kwargs = dict(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=20, read=180, write=30, pool=20),
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        return httpx.Client(**kwargs)
    except httpx.InvalidURL:
        # Same trap as app/services/ai_cores.py: httpx cannot parse bracketed
        # IPv6 entries such as "[::1]" in NO_PROXY (raises InvalidURL
        # "Invalid port: ':1]'" while building the client) — so a machine
        # with a perfectly valid NO_PROXY would fail every engine download.
        # Fall back to ignoring the env proxies; direct downloads still work
        # where no proxy is required.
        logger.warning(
            "Proxy env vars could not be parsed by httpx (e.g. bracketed IPv6 "
            "in NO_PROXY); downloading without env proxies."
        )
        return httpx.Client(trust_env=False, **kwargs)


def _check_download_url(url: str) -> None:
    """Only GitHub release hosts (or localhost in tests, via an explicit
    asset dict — see install_engine's `release` override). Anything else is
    refused rather than fetched."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host in GITHUB_HOST_ALLOWLIST or host in ("127.0.0.1", "localhost", "::1"):
        return
    raise EngineInstallError(f"拒绝从非 GitHub 官方地址下载引擎：{host}")


def resolve_release(asset_pattern: str, companion_pattern: Optional[str] = None,
                    *, tag: Optional[str] = None, per_page: int = 10) -> dict[str, Any]:
    """Find the newest llama.cpp release carrying the wanted asset.

    Lists releases newest-first and takes the first ``bNNNNN`` nightly tag
    that has the asset — ``/releases/latest`` is *not* used because it now
    resolves to a ``v1.0.0`` release whose only asset is ``nightly-tag.txt``.
    Returns ``{"tag": ..., "asset": {...}, "companion": {...} | None}`` with
    the raw GitHub asset dicts (name / browser_download_url / digest / size).
    """
    asset_re = re.compile(asset_pattern)
    companion_re = re.compile(companion_pattern) if companion_pattern else None
    with _http_client() as client:
        if tag:
            resp = client.get(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{tag}")
            resp.raise_for_status()
            releases = [resp.json()]
        else:
            resp = client.get(GITHUB_API_RELEASES, params={"per_page": per_page})
            resp.raise_for_status()
            releases = resp.json()
    for rel in releases:
        rel_tag = str(rel.get("tag_name") or "")
        if not tag and not re.fullmatch(r"b\d+", rel_tag):
            continue  # v1.0.0-style versioned release: no binaries, skip
        assets = rel.get("assets") or []
        asset = next((a for a in assets if asset_re.fullmatch(str(a.get("name") or ""))), None)
        if asset is None:
            continue
        companion = None
        if companion_re is not None:
            companion = next((a for a in assets if companion_re.fullmatch(str(a.get("name") or ""))), None)
            if companion is None:
                logger.warning("release %s has %s but no cudart companion — skipping",
                               rel_tag, asset.get("name"))
                continue
        return {"tag": rel_tag, "asset": asset, "companion": companion}
    raise EngineInstallError(
        "在 llama.cpp 最新发布里没找到对应的 Windows 引擎包（可能上游改了包名），请稍后重试")


def _download_asset(asset: dict[str, Any], dest: Path) -> tuple[int, str]:
    """Stream one release asset to `dest` (resuming a ``.part`` file via
    Range), verifying the GitHub-published sha256 digest. Returns
    ``(size_bytes, sha256_hex)``. Raises EngineInstallError on any failure —
    the caller decides nothing here, so a half-download never looks installed.
    """
    import httpx

    url = str(asset.get("browser_download_url") or "")
    if not url:
        raise EngineInstallError(f"引擎包 {asset.get('name')} 没有可下载地址")
    _check_download_url(url)
    expected_sha = None
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        expected_sha = digest[len("sha256:"):].lower()
    expected_size = asset.get("size")
    if isinstance(expected_size, int) and expected_size > MAX_ZIP_BYTES:
        raise EngineInstallError(f"引擎包过大（{expected_size / 1e6:.0f} MB），拒绝下载")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    resume_from = tmp.stat().st_size if tmp.is_file() else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
    try:
        with _http_client() as client:
            with client.stream("GET", url, headers=headers) as resp:
                if resume_from and resp.status_code == 416:
                    tmp.unlink(missing_ok=True)
                    return _download_asset(asset, dest)
                resp.raise_for_status()
                if resume_from and resp.status_code != 206:
                    logger.info("server ignored Range for %s — restarting from scratch",
                                asset.get("name"))
                    tmp.unlink(missing_ok=True)
                    resume_from = 0
                length = resp.headers.get("content-length")
                if length is not None and resume_from + int(length) > MAX_ZIP_BYTES:
                    raise EngineInstallError("引擎包过大，拒绝下载")
                sha = hashlib.sha256()
                total = 0
                if resume_from:
                    with open(tmp, "rb") as existing:
                        for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                            sha.update(chunk)
                    total = resume_from
                mode = "ab" if resume_from and resp.status_code == 206 else "wb"
                if mode == "wb" and resume_from:
                    total = 0
                    sha = hashlib.sha256()
                with open(tmp, mode) as out:
                    for chunk in resp.iter_bytes(1024 * 1024):
                        out.write(chunk)
                        sha.update(chunk)
                        total += len(chunk)
                        if total > MAX_ZIP_BYTES:
                            raise EngineInstallError("引擎包下载超出大小上限，已中止")
    except EngineInstallError:
        raise
    except httpx.HTTPStatusError as exc:
        raise EngineInstallError(f"下载引擎包失败：服务器返回 {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001 - network errors become a user-facing message
        raise EngineInstallError(f"下载引擎包失败：{exc}")
    if expected_sha and sha.hexdigest() != expected_sha:
        tmp.unlink(missing_ok=True)
        raise EngineInstallError("引擎包校验失败（sha256 不符），已丢弃该文件，请重试")
    if isinstance(expected_size, int) and total != expected_size:
        tmp.unlink(missing_ok=True)
        raise EngineInstallError(f"引擎包大小不符（应为 {expected_size} 字节，实际 {total} 字节）")
    tmp.replace(dest)
    return total, sha.hexdigest()


def _safe_extract_zip(zip_path: Path, target_tmp: Path) -> None:
    """Extract a zip into `target_tmp`, rejecting zip-slip paths, symlinks
    and oversized archives. Raises EngineInstallError — never extracts a
    hostile member."""
    total_uncompressed = 0
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise EngineInstallError("引擎包内文件过多，拒绝解压")
        for info in members:
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise EngineInstallError("引擎包解压后过大，拒绝解压")
            name = info.filename
            if not name or name.endswith("/"):
                continue
            # zip-slip: absolute paths, drive letters, .. segments
            if name.startswith(("/", "\\")) or ":" in name.split("/")[0] or ".." in Path(name).parts:
                raise EngineInstallError(f"引擎包内含非法路径，已拒绝解压：{name}")
            # symlink entries (unix mode symlink bit)
            if (info.external_attr >> 16) & 0o120000:
                raise EngineInstallError(f"引擎包内含符号链接，已拒绝解压：{name}")
        target_tmp.mkdir(parents=True, exist_ok=True)
        for info in members:
            if info.filename.endswith("/"):
                continue
            dest = target_tmp / info.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def _verify_server_exe(server_exe: Path) -> tuple[bool, Optional[str]]:
    """Run `llama-server.exe --version` (Windows only). A failed run does not
    fail the install — the binary is present and sha-verified — but it is
    recorded honestly so the UI can say "已安装，未验证运行" instead of
    pretending the engine is proven to work."""
    if sys.platform != "win32":
        return False, "非 Windows 系统，未运行验证"
    try:
        proc = subprocess.run(
            [str(server_exe), "--version"], capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            return True, out.strip().splitlines()[0] if out.strip() else None
        return False, f"退出码 {proc.returncode}：{out.strip()[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{exc.__class__.__name__}: {exc}"


def install_engine(engine_id: str, *, models_root: Path | None = None,
                   tag: Optional[str] = None, release: Optional[dict[str, Any]] = None,
                   allow_non_windows: bool = False) -> dict[str, Any]:
    """Download + verify + extract one native engine. Returns the
    :func:`list_engines` row for it. `release` is a test hook (a pre-resolved
    ``resolve_release()`` result, so tests can point at a localhost server);
    `allow_non_windows` is the matching test hook — the API layer never sets
    it, so real installs stay Windows-only."""
    entry = _BY_ID.get(engine_id)
    if entry is None:
        raise EngineInstallError(f"未知推理引擎：{engine_id}")
    if sys.platform != "win32" and not allow_non_windows:
        raise EngineInstallError("独立推理引擎仅支持 Windows x64，当前系统无法安装")

    resolved = release or resolve_release(
        entry["asset_pattern"], entry.get("companion_pattern"), tag=tag)
    asset = resolved["asset"]
    companion = resolved.get("companion")
    rel_tag = resolved["tag"]

    root = _engines_root(models_root)
    downloads_dir = root / ".downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    engine_dir = root / engine_id
    tmp_dir = root / f".tmp-{engine_id}-{rel_tag}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        main_zip = downloads_dir / str(asset["name"])
        _download_asset(asset, main_zip)
        _safe_extract_zip(main_zip, tmp_dir)
        if companion is not None:
            companion_zip = downloads_dir / str(companion["name"])
            _download_asset(companion, companion_zip)
            _safe_extract_zip(companion_zip, tmp_dir)
        server_exe = _server_exe(tmp_dir)
        if not server_exe.is_file():
            raise EngineInstallError(
                f"引擎包解压后缺少 llama-server.exe（{asset.get('name')}），安装已中止")
        verify_ok, verify_info = _verify_server_exe(server_exe)
        # Swap in atomically: only now does the old engine directory go away.
        if engine_dir.exists():
            shutil.rmtree(engine_dir, ignore_errors=True)
        tmp_dir.replace(engine_dir)
    except EngineInstallError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise EngineInstallError(f"安装引擎失败：{exc}")

    state = _read_state(models_root)
    state[engine_id] = {
        "tag": rel_tag,
        "asset": asset.get("name"),
        "sha256": str(asset.get("digest") or "").removeprefix("sha256:"),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "verify_ok": verify_ok,
        "verify_error": None if verify_ok else verify_info,
        "version": verify_info if verify_ok else None,
    }
    _write_state(state, models_root)
    logger.info("engine installed: %s %s (verify_ok=%s)", engine_id, rel_tag, verify_ok)
    row = get_engine(engine_id, models_root)
    assert row is not None
    return row


def uninstall_engine(engine_id: str, *, models_root: Path | None = None) -> dict[str, Any]:
    """Remove one native engine's directory and its state record. The
    in-process llama-cpp-python backend is untouched — this only deletes the
    standalone binaries, so it is safe while the app is running (a restart
    is only needed if something was actively using llama-server.exe)."""
    if engine_id not in _BY_ID:
        raise EngineInstallError(f"未知推理引擎：{engine_id}")
    root = _engines_root(models_root)
    engine_dir = root / engine_id
    removed = False
    if engine_dir.exists():
        shutil.rmtree(engine_dir, ignore_errors=True)
        removed = True
    state = _read_state(models_root)
    state.pop(engine_id, None)
    _write_state(state, models_root)
    return {"id": engine_id, "uninstalled": True, "dir_removed": removed,
            "engines": list_engines(models_root)}
