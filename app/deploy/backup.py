"""Backup / verify / restore for the bundled PostgreSQL.

A backup is a ``pg_dump -Fc`` custom-format archive (the only format that
``pg_restore --clean --if-exists --single-transaction`` can put back over a live
schema in one atomic step), plus a sidecar ``.meta.json`` that records the app
version, schema signature and SHA-256 so a restore can tell the customer what they
are about to roll back to *before* touching anything.

Layout::

    aims_data/backups/aims-20260101-020000.dump
    aims_data/backups/aims-20260101-020000.dump.meta.json
    aims_data/backups/pre-restore-20260102-101500.dump   (never pruned)

Retention: newest ``AIMS_BACKUP_KEEP`` archives (default 14) are kept; the safety
copy taken before every restore is exempt so a failed restore is always reversible.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from app.deploy import APP_VERSION
from app.deploy.paths import Layout, load_layout
from app.deploy.pg import Postgres, PgError

logger = logging.getLogger("aims.deploy.backup")

PREFIX = "aims-"
SAFETY_PREFIX = "pre-restore-"
META_SUFFIX = ".meta.json"


def _sha256(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _stamp(now: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))


def backup_keep() -> int:
    try:
        return max(0, int(os.environ.get("AIMS_BACKUP_KEEP", "14")))
    except ValueError:
        return 14


def create_backup(pg: Postgres, *, password: str, reason: str = "manual",
                  layout: Layout | None = None, stamp: str | None = None,
                  safety: bool = False, keep: int | None = None) -> dict[str, Any]:
    """Take a full logical backup. Returns the metadata dict written next to it."""
    layout = layout or pg.layout
    layout.backups_dir.mkdir(parents=True, exist_ok=True)
    name = f"{SAFETY_PREFIX if safety else PREFIX}{stamp or _stamp()}.dump"
    dest = layout.backups_dir / name
    if dest.exists():  # two backups inside the same second
        dest = dest.with_name(dest.stem + "-b" + dest.suffix)

    started = time.time()
    pg.dump(dest, password=password)
    if not pg.dump_ok(dest):
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise PgError("生成的备份文件校验失败（pg_restore -l 无法读取），已删除")

    meta = {
        "file": dest.name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "reason": reason,
        "app_version": APP_VERSION,
        "database": pg.database,
        "size_bytes": dest.stat().st_size,
        "sha256": _sha256(dest),
        "duration_s": round(time.time() - started, 1),
        "format": "pg_dump custom (Fc)",
    }
    (dest.with_suffix(dest.suffix + META_SUFFIX)).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not safety:
        prune(layout, keep if keep is not None else backup_keep())
    logger.info("Backup created: %s (%.1f MB, %.1fs)", dest, meta["size_bytes"] / 1e6, meta["duration_s"])
    return meta


def prune(layout: Layout | None = None, keep: int | None = None) -> list[str]:
    """Delete the oldest automatic backups beyond `keep`. Safety copies are kept."""
    layout = layout or load_layout()
    keep = backup_keep() if keep is None else keep
    if keep <= 0:
        return []
    removed: list[str] = []
    files = sorted(layout.backups_dir.glob(f"{PREFIX}*.dump"), key=lambda p: p.stat().st_mtime)
    for old in files[:max(0, len(files) - keep)]:
        try:
            old.unlink()
            old.with_suffix(old.suffix + META_SUFFIX).unlink(missing_ok=True)
            removed.append(old.name)
        except OSError as exc:
            logger.warning("删除旧备份失败 %s: %s", old, exc)
    return removed


def list_backups(layout: Layout | None = None) -> list[dict[str, Any]]:
    """Newest first."""
    layout = layout or load_layout()
    out: list[dict[str, Any]] = []
    for path in layout.backups_dir.glob("*.dump"):
        meta: dict[str, Any] = {}
        meta_path = path.with_suffix(path.suffix + META_SUFFIX)
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        st = path.stat()
        out.append({
            "file": path.name,
            "path": str(path),
            "size_bytes": st.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "reason": meta.get("reason", "unknown"),
            "app_version": meta.get("app_version"),
            "sha256": meta.get("sha256"),
            "safety": path.name.startswith(SAFETY_PREFIX),
        })
    return sorted(out, key=lambda b: b["mtime"], reverse=True)


def resolve(layout: Layout, which: str = "latest") -> Path:
    if which in ("", "latest"):
        items = [b for b in list_backups(layout) if not b["safety"]]
        if not items:
            raise PgError("没有可用的备份文件")
        return Path(items[0]["path"])
    p = Path(which)
    if p.is_file():
        return p
    cand = layout.backups_dir / Path(which).name
    if cand.is_file():
        return cand
    raise PgError(f"找不到备份文件：{which}")


def verify(pg: Postgres, path: Path, *, meta: dict | None = None) -> dict[str, Any]:
    """Read-only validation: archive is parseable and (if known) the hash matches."""
    if not path.is_file():
        raise PgError(f"备份文件不存在：{path}")
    out: dict[str, Any] = {"file": path.name, "ok": False, "sha256_match": None}
    if meta is None:
        mp = path.with_suffix(path.suffix + META_SUFFIX)
        if mp.is_file():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                meta = None
    if meta and meta.get("sha256"):
        out["sha256_match"] = (_sha256(path) == meta["sha256"])
    out["ok"] = pg.dump_ok(path) and (out["sha256_match"] is not False)
    return out


def restore(pg: Postgres, path: Path, *, password: str, layout: Layout | None = None,
            safety_backup: bool = True) -> dict[str, Any]:
    """Restore a dump over the live database, after saving the current state."""
    layout = layout or pg.layout
    check = verify(pg, path)
    if not check["ok"]:
        raise PgError(f"备份文件校验失败，已中止恢复：{path.name}")

    result: dict[str, Any] = {"restored": path.name, "safety_backup": None}
    if safety_backup:
        try:
            meta = create_backup(pg, password=password, reason="pre-restore", safety=True,
                                 layout=layout)
            result["safety_backup"] = meta["file"]
        except PgError as exc:
            raise PgError(f"恢复前的安全备份失败，已中止（不会冒险覆盖数据）：{exc}") from exc

    pg.restore(path, password=password, clean=True)
    result["restored_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return result


def copy_to(path: Path, dest: Path) -> Path:
    """Copy a backup somewhere else (used by `backup --export`)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return dest
