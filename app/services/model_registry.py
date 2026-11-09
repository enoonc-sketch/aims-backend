"""Tracks which downloaded local model (LLM / embedding) is currently active.

Kept as a tiny JSON side-file (not a DB table) so switching the active model
at runtime needs no migration and works identically in SQLite-standalone/EXE
mode and in the Docker/Postgres deployment.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("aims.model_registry")
_lock = threading.Lock()

_DEFAULTS = {
    # Default to the lightest LLM (~400MB) so a fresh install's first download
    # is fast and reliable; once any model is actually downloaded, users can
    # switch to a bigger one (3B/7B) from AI Center -> 模型下载 for better quality.
    "active_llm": "llm-qwen2.5-0.5b-instruct-q4",
    "active_embedding": "embedding-bge-small-zh",
}


def _registry_path() -> Path:
    from app.core.config import settings

    return settings.models_dir_path / "active.json"


def _read() -> dict[str, Any]:
    path = _registry_path()
    if not path.is_file():
        return dict(_DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        merged = dict(_DEFAULTS)
        merged.update(data or {})
        return merged
    except Exception:
        logger.warning("Could not read model registry at %s, using defaults", path)
        return dict(_DEFAULTS)


def _write(data: dict[str, Any]) -> None:
    path = _registry_path()
    with _lock:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_active(kind: str) -> Optional[str]:
    """kind: 'llm' | 'embedding'"""
    key = "active_llm" if kind == "llm" else "active_embedding"
    return _read().get(key)


def set_active(kind: str, core_id: str) -> None:
    key = "active_llm" if kind == "llm" else "active_embedding"
    data = _read()
    data[key] = core_id
    _write(data)
    # Invalidate any cached llama.cpp handle for this kind so the next call
    # actually loads the newly-selected model.
    from app.services import llm_local

    llm_local.unload(kind)
