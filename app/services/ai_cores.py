"""Load open-source AI cores (diagnosis rules, health weights, prompts) from vendor dir,
and — the newer bit — download/persist those same cores (plus the Data Analysis JS libs)
into the database so the frontend can list and fetch "downloaded models" straight from
the DB instead of only from disk.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("aims.ai_cores")


def _vendor_candidates() -> list[Path]:
    import sys
    out: list[Path] = []
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).parent / "cores" / "vendor")
        out.append(Path(sys.executable).parent / "vendor")
        if hasattr(sys, "_MEIPASS"):
            out.append(Path(sys._MEIPASS) / "cores" / "vendor")
    # source tree
    here = Path(__file__).resolve().parent.parent.parent
    out.append(here / "cores" / "vendor")
    out.append(here / "cores" / "ai")  # fallback: raw packs before copy
    out.append(Path.cwd() / "cores" / "vendor")
    out.append(Path.cwd() / "cores" / "ai")
    return out


def find_core_file(*relative: str) -> Optional[Path]:
    rel = Path(*relative)
    for base in _vendor_candidates():
        # try ai/ subfolder and flat
        for cand in (base / rel, base / "ai" / rel.name if rel.name else base, base / rel.name):
            if cand.is_file():
                return cand
    return None


@lru_cache(maxsize=8)
def load_json_core(name: str) -> dict[str, Any]:
    """Load diagnosis_rules.json / health_weights.json / etc."""
    path = find_core_file("ai", name) or find_core_file(name)
    if path is None:
        logger.warning("AI core not found: %s", name)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        logger.info("Loaded AI core %s from %s", name, path)
        return data
    except Exception as e:
        logger.exception("Failed loading AI core %s: %s", name, e)
        return {}


def match_diagnosis(symptom: str, component_name: str) -> dict[str, Any]:
    pack = load_json_core("diagnosis_rules.json")
    rules = pack.get("rules") or []
    symptom_l = (symptom or "").lower()
    comp_l = (component_name or "").lower()

    best = None
    best_score = -1
    for rule in rules:
        score = 0
        for s in rule.get("symptoms") or []:
            if s.lower() in symptom_l or symptom_l in s.lower():
                score += 2
        comps = rule.get("components") or ["*"]
        if "*" in comps:
            score += 1
        else:
            for c in comps:
                if c.lower() in comp_l or comp_l in c.lower():
                    score += 3
        if score > best_score:
            best_score = score
            best = rule

    if best is None or best_score <= 0:
        default = pack.get("default") or {}
        return {
            "diagnosis": default.get("diagnosis", "No matching rule in AI core pack."),
            "confidence": float(default.get("confidence", 0.4)),
            "recommendation": default.get("recommendation", ""),
            "rule_id": None,
            "severity": None,
            "core": pack.get("name", "diagnosis_rules"),
        }

    diagnosis_text = best.get("diagnosis", "")
    severity = best.get("severity")
    if severity == "high":
        diagnosis_text = f"[HIGH SEVERITY] {diagnosis_text}"

    return {
        "diagnosis": diagnosis_text,
        "confidence": float(best.get("confidence", 0.7)),
        "recommendation": best.get("recommendation", ""),
        "rule_id": best.get("id"),
        "severity": severity,
        "core": pack.get("name", "diagnosis_rules"),
    }


def health_penalties() -> dict[str, Any]:
    pack = load_json_core("health_weights.json")
    return pack or {
        "base_score": 100,
        "penalties": {
            "active_alarm_warning": 5,
            "active_alarm_critical": 15,
            "device_status_warning": 10,
            "device_status_fault": 25,
            "device_status_offline": 20,
            "low_data_availability": 8,
            "trained_anomaly_detected": 12,
        },
        "min_expected_points_24h": 4,
    }


def prompts() -> dict[str, Any]:
    return load_json_core("rag_prompts.json") or {}


def prediction_config() -> dict[str, Any]:
    return load_json_core("prediction_baseline.json") or {
        "model": "linear_extrapolation",
        "default_horizon_hours": 24,
        "min_points": 3,
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


# Hardcoded copy of cores/manifest.json's content, used only if the manifest file
# itself can't be found anywhere (e.g. an EXE build that forgot to bundle it).
# This guarantees the AI Center > Models tab is never empty.
_FALLBACK_MANIFEST: list[dict[str, Any]] = [
    {"id": "xlsx", "name": "SheetJS Community Edition (xlsx)", "license": "Apache-2.0",
     "homepage": "https://github.com/SheetJS/sheetjs",
     "url": "https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js",
     "filename": "xlsx.full.min.js", "purpose": "Parse .xlsx/.xls for Data Analysis import"},
    {"id": "chartjs", "name": "Chart.js", "license": "MIT",
     "homepage": "https://www.chartjs.org/",
     "url": "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js",
     "filename": "chart.umd.min.js", "purpose": "Trend / curve charts on Data Analysis page"},
    {"id": "chartjs-adapter-date", "name": "chartjs-adapter-date-fns", "license": "MIT",
     "homepage": "https://github.com/chartjs/chartjs-adapter-date-fns",
     "url": "https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js",
     "filename": "chartjs-adapter-date-fns.bundle.min.js", "purpose": "Time axis for Chart.js"},
    {"id": "ai-diagnosis-rules", "name": "Analyzer Diagnosis Rule Pack", "license": "MIT",
     "homepage": "local://cores/ai/diagnosis_rules.json", "url": "local://cores/ai/diagnosis_rules.json",
     "filename": "ai/diagnosis_rules.json", "purpose": "Offline fault diagnosis rules for AI Center"},
    {"id": "ai-health-weights", "name": "Health Score Weight Pack", "license": "MIT",
     "homepage": "local://cores/ai/health_weights.json", "url": "local://cores/ai/health_weights.json",
     "filename": "ai/health_weights.json", "purpose": "Configurable penalties for device health scoring"},
    {"id": "ai-rag-prompts", "name": "RAG / Chat Prompt Pack", "license": "MIT",
     "homepage": "local://cores/ai/rag_prompts.json", "url": "local://cores/ai/rag_prompts.json",
     "filename": "ai/rag_prompts.json", "purpose": "System prompts for RAG and offline chat"},
    {"id": "ai-prediction-baseline", "name": "Linear Baseline Prediction Core", "license": "MIT",
     "homepage": "local://cores/ai/prediction_baseline.json", "url": "local://cores/ai/prediction_baseline.json",
     "filename": "ai/prediction_baseline.json", "purpose": "Open baseline time-series extrapolation config"},
]


def _manifest_candidates() -> list[Path]:
    """Every place manifest.json could plausibly live — mirrors _vendor_candidates()
    so this also works from a frozen/EXE build, not just the source tree."""
    import sys
    out: list[Path] = []
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).parent / "cores" / "manifest.json")
        out.append(Path(sys.executable).parent / "manifest.json")
        if hasattr(sys, "_MEIPASS"):
            out.append(Path(sys._MEIPASS) / "cores" / "manifest.json")
    out.append(_repo_root() / "cores" / "manifest.json")
    out.append(Path.cwd() / "cores" / "manifest.json")
    for vendor_dir in _vendor_candidates():
        out.append(vendor_dir / "manifest.json")
        out.append(vendor_dir.parent / "manifest.json")
    return out


def load_manifest() -> list[dict[str, Any]]:
    """The full list of downloadable cores/models (AI rule packs + Data Analysis JS libs).
    Not cached, so fixing/adding the manifest file doesn't require a restart to notice —
    and falls back to a hardcoded copy if the file can't be found anywhere, so this list
    is never empty."""
    for path in _manifest_candidates():
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cores = data.get("cores") or []
                if cores:
                    return cores
            except Exception:
                logger.exception("Failed to read cores manifest: %s", path)
    logger.warning("cores/manifest.json not found in any candidate location — using built-in fallback list")
    return _FALLBACK_MANIFEST


def get_manifest_entry(core_id: str) -> Optional[dict[str, Any]]:
    for entry in load_manifest():
        if entry.get("id") == core_id:
            return entry
    return None


def _candidate_urls(url: str) -> list[str]:
    """Return [url, mirror_url] for Hugging Face links.

    huggingface.co is unreachable (or very slow/flaky) without a proxy from
    inside mainland China, which is where this app's downloads most often
    fail in practice. hf-mirror.com mirrors the exact same repo/file layout,
    so swapping the host is a drop-in fallback — and it works fine outside
    China too, so trying it costs nothing when huggingface.co already works.
    """
    if "huggingface.co" in url:
        mirror = url.replace("huggingface.co", "hf-mirror.com", 1)
        return [url, mirror]
    return [url]


def _binary_candidate_urls(entry: dict[str, Any]) -> list[str]:
    """All URLs worth trying for a binary (model weight) download, in order:
    the manifest's own `url`, then any explicit `mirrors` list on the entry
    (e.g. a ModelScope copy — much faster/more reliable than any huggingface.co
    host from inside mainland China), then the automatic hf-mirror.com
    substitution as a last resort. Deduplicated, order preserved."""
    base = _candidate_urls(entry["url"])  # [url] or [url, hf-mirror substitute]
    primary, *hf_fallback = base
    out: list[str] = [primary, *entry.get("mirrors", []), *hf_fallback]
    seen: set[str] = set()
    deduped: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


# Network errors worth retrying against the *same* URL a couple of times
# before giving up on it and moving to the next mirror — a dropped connection
# or a momentary read stall shouldn't throw away a multi-hundred-MB partial
# download and force a full restart.
def _is_retryable(exc: Exception) -> bool:
    import httpx

    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
                         httpx.ConnectError, httpx.RemoteProtocolError, httpx.PoolTimeout)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def _describe_download_error(exc: Exception) -> str:
    """Turn a raw httpx/network exception into a message that actually tells
    the person what to check, instead of a bare Python exception repr."""
    import httpx

    if isinstance(exc, httpx.ConnectTimeout):
        return "连接超时 — 请检查网络连接，或是否需要配置代理才能访问外网"
    if isinstance(exc, httpx.ReadTimeout):
        return "下载中读取超时 — 网络可能不稳定，可以重试一次"
    if isinstance(exc, httpx.ConnectError):
        return "无法建立连接 — 请检查网络/DNS，或是否需要配置代理（HTTP_PROXY/HTTPS_PROXY 环境变量）才能访问外网"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"服务器返回错误状态码 {exc.response.status_code} — 模型文件地址可能已失效"
    if "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return "SSL 证书校验失败 — 常见于公司网络的中间代理/防火墙拦截了 HTTPS 流量"
    return str(exc)


def _http_client():
    import httpx

    # trust_env=True (default) makes httpx respect HTTP_PROXY/HTTPS_PROXY/NO_PROXY
    # env vars automatically — important on corporate networks that require a
    # proxy for internet access, which plain urllib does NOT do by default.
    kwargs = dict(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=20, read=180, write=30, pool=20),
        headers={"User-Agent": "AIMS-CoreDownloader/1.3"},
    )
    try:
        return httpx.Client(**kwargs)
    except httpx.InvalidURL:
        # httpx cannot parse bracketed IPv6 entries such as "[::1]" in NO_PROXY
        # (it raises InvalidURL "Invalid port: ':1]'" while building the client,
        # before any request is made) — so a machine with a perfectly valid
        # NO_PROXY would fail every core download. Fall back to ignoring the
        # env proxies; direct downloads still work where no proxy is required.
        logger.warning(
            "Proxy env vars could not be parsed by httpx (e.g. bracketed IPv6 "
            "in NO_PROXY); downloading without env proxies."
        )
        return httpx.Client(trust_env=False, **kwargs)


def _stream_one_attempt(url: str, tmp: Path, entry: dict[str, Any], progress_every: int) -> tuple[int, str]:
    """Single GET attempt against one URL, resuming from an existing .part file
    via a Range header when possible. Raises on any failure; caller decides
    whether to retry the same URL, fall through to the next mirror, or give up."""
    import hashlib

    resume_from = tmp.stat().st_size if tmp.is_file() else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}

    with _http_client() as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resume_from and resp.status_code == 416:
                # Our partial file is already complete (or the server disagrees
                # about the remote size) — drop it and restart clean.
                resp.close()
                tmp.unlink(missing_ok=True)
                return _stream_one_attempt(url, tmp, entry, progress_every)
            resp.raise_for_status()

            resumed = resume_from and resp.status_code == 206
            sha = hashlib.sha256()
            total = 0
            if resumed:
                # Seed the running hash with what's already on disk so the final
                # sha256 covers the whole file, not just the newly-fetched tail.
                with open(tmp, "rb") as existing:
                    for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                        sha.update(chunk)
                total = resume_from
                logger.info("Resuming %s download from %.0f MB", entry.get("id"), total / 1e6)
            else:
                if resume_from:
                    logger.info("Server ignored Range for %s (got %s) — restarting from scratch",
                                entry.get("id"), resp.status_code)
                tmp.unlink(missing_ok=True)

            last_logged = total
            with open(tmp, "ab" if resumed else "wb") as out:
                for chunk in resp.iter_bytes(1024 * 1024):
                    out.write(chunk)
                    sha.update(chunk)
                    total += len(chunk)
                    if total - last_logged >= progress_every:
                        logger.info("Downloading %s: %.0f MB", entry.get("id"), total / 1e6)
                        last_logged = total
    return total, sha.hexdigest()


def _download_binary_to_disk(
    entry: dict[str, Any], dest: Path, progress_every: int = 50_000_000, attempts_per_url: int = 2,
) -> tuple[int, str]:
    """Stream a large model file (GGUF etc.) straight to disk in chunks — never holds
    the whole thing in memory — and returns (size_bytes, sha256_hex).

    Tries every URL from _binary_candidate_urls() in order (primary host, any
    manifest-declared mirrors such as ModelScope, then the automatic
    hf-mirror.com substitution) so a single blocked/slow host doesn't fail the
    whole download. Within one URL, transient errors (timeouts, connection
    resets, 5xx) are retried a few times with backoff instead of immediately
    giving up — and each retry resumes from the partial file on disk via HTTP
    Range rather than re-downloading bytes already received, so a flaky
    connection doesn't force a multi-GB restart."""
    import time

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    candidates = _binary_candidate_urls(entry)

    errors: list[str] = []
    for url in candidates:
        for attempt in range(1, attempts_per_url + 1):
            try:
                total, sha_hex = _stream_one_attempt(url, tmp, entry, progress_every)
                tmp.replace(dest)
                return total, sha_hex
            except Exception as e:
                errors.append(f"{url} (第{attempt}次) → {_describe_download_error(e)}")
                logger.warning("Download attempt %d/%d failed for %s from %s: %s",
                                attempt, attempts_per_url, entry.get("id"), url, e)
                if attempt < attempts_per_url and _is_retryable(e):
                    time.sleep(min(2 ** attempt, 10))
                    continue
                break  # exhausted retries (or non-retryable) — try the next mirror

    raise RuntimeError("；".join(errors))


def _fetch_core_bytes(entry: dict[str, Any]) -> bytes:
    """Resolve a manifest entry's content, either from the bundled local JSON pack
    or by downloading it from its CDN url (with the same huggingface.co →
    hf-mirror.com fallback as the binary path, for any HF-hosted small files)."""
    url = entry["url"]
    if url.startswith("local://"):
        rel = url.replace("local://", "", 1)
        rel_path = Path(rel)
        # Try the exact repo-relative path first (source tree), then fall back to
        # searching the same vendor candidate locations used for frozen/EXE builds.
        candidates = [_repo_root() / rel_path, Path.cwd() / rel_path]
        found = next((c for c in candidates if c.is_file()), None)
        if found is None:
            found = find_core_file(rel_path.name)
        if found is None:
            raise FileNotFoundError(f"local core missing: {rel}")
        return found.read_bytes()

    errors: list[str] = []
    for candidate_url in _candidate_urls(url):
        try:
            with _http_client() as client:
                resp = client.get(candidate_url)
                resp.raise_for_status()
                return resp.content
        except Exception as e:
            errors.append(f"{candidate_url} → {_describe_download_error(e)}")
            logger.warning("Fetch failed for %s from %s: %s", entry.get("id"), candidate_url, e)
            continue
    raise RuntimeError("；".join(errors))


def _entry_kind(entry: dict[str, Any]) -> str:
    if entry.get("kind") in ("llm", "embedding"):
        return entry["kind"]
    return "ai" if str(entry.get("filename", "")).startswith("ai/") else "data"


def list_core_packs_from_db(db) -> list[dict[str, Any]]:
    """Manifest entries merged with whatever has already been downloaded into the DB."""
    from app.models.ai import AICorePack
    from app.services import model_registry

    rows = {row.core_id: row for row in db.query(AICorePack).all()}
    active_llm = model_registry.get_active("llm")
    active_embedding = model_registry.get_active("embedding")

    out: list[dict[str, Any]] = []
    for entry in load_manifest():
        cid = entry.get("id")
        row = rows.get(cid)
        kind = _entry_kind(entry)
        present = row is not None and (
            row.storage != "disk" or (row.file_path and Path(row.file_path).is_file())
        )
        out.append(
            {
                "id": cid,
                "name": entry.get("name"),
                "kind": kind,
                "license": entry.get("license"),
                "homepage": entry.get("homepage"),
                "filename": entry.get("filename"),
                "purpose": entry.get("purpose"),
                "in_db": present,
                "size_bytes": row.size_bytes if row else entry.get("size_bytes_hint"),
                "downloaded_at": row.downloaded_at if row else None,
                "engine": entry.get("engine"),
                "params": entry.get("params"),
                "quant": entry.get("quant"),
                "active": (kind == "llm" and cid == active_llm) or (kind == "embedding" and cid == active_embedding),
            }
        )
    return out


def download_core_to_db(db, core_id: str) -> dict[str, Any]:
    """Download (or read the bundled local copy of) one manifest core.

    Small JSON/JS packs are upserted whole into ai_core_packs.content_json/content_text
    (unchanged behaviour). Real binary models (kind: llm/embedding, download_type:
    binary) are streamed straight to disk under settings.MODELS_DIR and only their
    path/size/sha256 are stored in the DB row — this is the piece that makes the
    "AI Model / Core Packs" tab actually download real model weights, not just KB-sized
    config files."""
    from app.models.ai import AICorePack

    entry = get_manifest_entry(core_id)
    if entry is None:
        raise ValueError(f"Unknown core id: {core_id}")

    filename = entry["filename"]
    kind = _entry_kind(entry)
    is_binary = entry.get("download_type") == "binary"

    row = db.query(AICorePack).filter(AICorePack.core_id == core_id).one_or_none()
    if row is None:
        row = AICorePack(core_id=core_id)
        db.add(row)

    row.name = entry.get("name")
    row.kind = kind
    row.license = entry.get("license")
    row.homepage = entry.get("homepage")
    row.filename = filename
    row.purpose = entry.get("purpose")
    row.source_url = entry.get("url")

    if is_binary:
        from app.core.config import settings

        dest = settings.models_dir_path / filename
        size, sha = _download_binary_to_disk(entry, dest)
        row.storage = "disk"
        row.file_path = str(dest)
        row.sha256 = sha
        row.content_json = None
        row.content_text = None
        row.size_bytes = size
    else:
        raw = _fetch_core_bytes(entry)
        content_json = None
        content_text = None
        if filename.endswith(".json"):
            try:
                content_json = json.loads(raw.decode("utf-8"))
            except Exception:
                content_text = raw.decode("utf-8", errors="replace")
        else:
            content_text = raw.decode("utf-8", errors="replace")

        row.storage = "db"
        row.file_path = None
        row.sha256 = None
        row.content_json = content_json
        row.content_text = content_text
        row.size_bytes = len(raw)

        # Best-effort: also drop a copy on disk so the offline/EXE file-based loaders
        # (find_core_file / load_json_core) keep working even if nobody re-runs
        # scripts/download_cores.py.
        try:
            mirror = _repo_root() / "cores" / "vendor" / filename
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_bytes(raw)
            load_json_core.cache_clear()
        except Exception:
            logger.warning("Could not mirror %s to cores/vendor/ on disk", filename)

    row.downloaded_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)

    return {
        "id": core_id,
        "name": row.name,
        "kind": row.kind,
        "license": row.license,
        "homepage": row.homepage,
        "filename": row.filename,
        "purpose": row.purpose,
        "in_db": True,
        "size_bytes": row.size_bytes,
        "downloaded_at": row.downloaded_at,
        "engine": entry.get("engine"),
        "params": entry.get("params"),
        "quant": entry.get("quant"),
    }


def get_model_path(db, core_id: str) -> Optional[Path]:
    """Resolve the on-disk path of a downloaded binary model (LLM / embedding)."""
    from app.models.ai import AICorePack

    row = db.query(AICorePack).filter(AICorePack.core_id == core_id).one_or_none()
    if row and row.file_path and Path(row.file_path).is_file():
        return Path(row.file_path)
    # fall back to the predictable manifest-derived path, in case the DB row
    # went missing but the file is still on disk (e.g. DB was reset)
    entry = get_manifest_entry(core_id)
    if entry and entry.get("download_type") == "binary":
        from app.core.config import settings

        cand = settings.models_dir_path / entry["filename"]
        if cand.is_file():
            return cand
    return None


def get_core_pack_content(db, core_id: str) -> Optional[dict[str, Any]]:
    from app.models.ai import AICorePack

    row = db.query(AICorePack).filter(AICorePack.core_id == core_id).one_or_none()
    if row is None:
        return None
    return {
        "id": row.core_id,
        "filename": row.filename,
        "kind": row.kind,
        "content_json": row.content_json,
        "content_text": row.content_text,
    }


def list_ai_cores() -> list[dict[str, Any]]:
    found = []
    for name in (
        "diagnosis_rules.json",
        "health_weights.json",
        "rag_prompts.json",
        "prediction_baseline.json",
    ):
        path = find_core_file("ai", name) or find_core_file(name)
        found.append({
            "id": name,
            "available": path is not None,
            "path": str(path) if path else None,
        })
    return found
