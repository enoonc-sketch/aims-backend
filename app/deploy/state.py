"""Deployment state (aims_data/deployment.json) — the machine-readable record of
what is installed, where, and which versions have been applied to it.

Deliberately a JSON side-file rather than a DB table: the installer has to read
"which version is installed / did the cluster already exist" *before* the database
is reachable, and an uninstall has to read it after the service is gone.

Also mirrored into the `system_settings` table (see provision.py) so the running
application — and the System page — can show the same information.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.deploy.paths import Layout, load_layout

DEFAULT_PORT = 55432


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_state() -> dict[str, Any]:
    return {
        "schema": 1,
        "product": "AIMS Backend",
        "app_version": None,
        "build_id": None,
        "installed_at": None,
        "updated_at": None,
        "pg": {
            "major": None,
            "port": DEFAULT_PORT,
            "host": "127.0.0.1",
            "data_dir": None,
            "superuser": "aims_dba",
            "app_user": "aims",
            "database": "aims",
            "service": "AIMSPostgreSQL",
            "vector": None,          # True / False — pgvector available in this cluster
            "timescale": None,       # True / False — TimescaleDB available
        },
        "paths": {},
        "history": [],
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        # A corrupt state file must not brick the installation: fall back to defaults
        # and keep the broken file around for support.
        try:
            path.with_suffix(path.suffix + ".corrupt").write_text(
                path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        except Exception:
            pass
        return None


def read_state(layout: Layout | None = None) -> dict[str, Any]:
    layout = layout or load_layout()
    data = _read_json(layout.state_file) or {}
    state = default_state()
    # shallow-merge, one level deep for "pg"
    for k, v in (data or {}).items():
        if k == "pg" and isinstance(v, dict):
            state["pg"].update(v)
        else:
            state[k] = v
    return state


def write_state(state: dict[str, Any], layout: Layout | None = None) -> Path:
    layout = layout or load_layout()
    layout.data_dir.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(layout.data_dir), prefix=".deployment-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, layout.state_file)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return layout.state_file


def record_version(state: dict[str, Any], version: str, action: str, *, build_id: str | None = None,
                   note: str = "") -> dict[str, Any]:
    """Append an entry to the upgrade history (newest last) and bump the version."""
    state["app_version"] = version
    if build_id:
        state["build_id"] = build_id
    state["updated_at"] = _now()
    if action == "install" and not state.get("installed_at"):
        state["installed_at"] = state["updated_at"]
    history = state.setdefault("history", [])
    if not history or history[-1].get("version") != version or history[-1].get("action") != action:
        history.append({"version": version, "action": action, "at": state["updated_at"], "note": note})
        # keep the file small but never lose the original install record
        if len(history) > 50:
            state["history"] = history[:1] + history[-49:]
    return state


def state_path_str(layout: Layout | None = None) -> str:
    return str((layout or load_layout()).state_file)
