"""Offline AI engine: which models are on disk, which one is active, does it work.

The "AI engine" of AIMS is llama.cpp (compiled into llama-cpp-python, bundled in
the EXE) plus GGUF model files. There is no separate service to deploy and no
network call: everything runs in the application process. So "自动部署 AI 引擎"
comes down to three things this module guarantees at install time:

  1. the bundled GGUF files are where the manifest says they are;
  2. ``cores/models/active.json`` points at models that actually exist (so the AI
     Center does not open on "尚未选择要激活的模型");
  3. the DB rows that the "模型下载" page reads exist for those files, so the UI
     shows them as already downloaded instead of offering a 400 MB download on an
     offline machine;
  4. ``probe()``/``warmup()`` prove the inference backend really loads — the
     failure mode this replaces is "silently falls back to canned text".

If no model is bundled (lite build), every function degrades to a no-op and the
app keeps its rule-based offline AI (diagnosis_rules.json etc.), unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from app.deploy.paths import Layout, load_layout

logger = logging.getLogger("aims.deploy.ai")

DEFAULT_LLM = "llm-qwen2.5-0.5b-instruct-q4"
DEFAULT_EMBEDDING = "embedding-bge-small-zh"
# bge-small-zh-v1.5 outputs 512 dimensions. The pgvector column is created with
# AI_EMBEDDING_DIM, so the generated config pins it to the bundled model's size.
DEFAULT_EMBEDDING_DIM = 512


def manifest_path(layout: Layout) -> Path | None:
    for cand in (layout.root / "cores" / "manifest.json",
                 layout.root / "cores" / "vendor" / "manifest.json"):
        if cand.is_file():
            return cand
    return None


def load_manifest(layout: Layout | None = None) -> list[dict[str, Any]]:
    layout = layout or load_layout()
    path = manifest_path(layout)
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("core manifest unreadable: %s", path)
        return []
    return [c for c in data.get("cores", []) if isinstance(c, dict)]


def _is_model(entry: dict[str, Any]) -> bool:
    return entry.get("kind") in ("llm", "embedding") and entry.get("download_type") == "binary"


def model_file(layout: Layout, entry: dict[str, Any]) -> Path:
    """模型文件的真实位置。

    产品安装把内置模型放在只读的程序目录（cores\\models，随安装包分发），
    用户后来下载/替换的模型放在可写的数据目录（%ProgramData%\\AIMS\\Models）。
    查找顺序是"数据目录优先"，这样管理员换模型不用碰 Program Files；
    两处都没有时返回数据目录里的路径，作为后续下载的落点。
    """
    name = str(entry.get("filename", ""))
    primary = layout.models_dir / name
    if primary.is_file():
        return primary
    bundled = layout.bundled_models_dir / name
    if bundled.is_file():
        return bundled
    return primary


def bundled_models(layout: Layout | None = None) -> list[dict[str, Any]]:
    """Manifest entries whose file is already present on disk."""
    layout = layout or load_layout()
    out = []
    for entry in load_manifest(layout):
        if not _is_model(entry):
            continue
        path = model_file(layout, entry)
        if path.is_file() and path.stat().st_size > 1024:
            out.append({**entry, "path": str(path), "size_bytes": path.stat().st_size})
    return out


def registry_path(layout: Layout) -> Path:
    return layout.models_dir / "active.json"


def ensure_registry(layout: Layout | None = None) -> dict[str, str]:
    """Point active.json at models that exist; leave a working file untouched."""
    layout = layout or load_layout()
    present = {m["id"]: m for m in bundled_models(layout)}
    if not present:
        return {}
    path = registry_path(layout)
    current: dict[str, Any] = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            current = {}

    def pick(kind: str, default_id: str) -> str | None:
        cur = current.get(f"active_{kind}")
        if cur and cur in present and present[cur].get("kind") == kind:
            return cur
        if default_id in present:
            return default_id
        same_kind = [m for m in present.values() if m.get("kind") == kind]
        return same_kind[0]["id"] if same_kind else None

    active = {
        "active_llm": pick("llm", DEFAULT_LLM),
        "active_embedding": pick("embedding", DEFAULT_EMBEDDING),
    }
    merged = {k: v for k, v in active.items() if v}
    if merged and merged != {k: current.get(k) for k in merged}:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = {k: v for k, v in current.items() if k not in merged}
        existing.update(merged)
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("active.json -> %s", merged)
    return merged


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def register_in_db(*, calculate_hash: bool = True) -> int:
    """Upsert ai_core_packs rows for the bundled models (idempotent).

    Uses the app's own session factory, so it must run *after* the environment
    (DATABASE_URL) has been set — which is exactly what `provision` does.
    """
    models = bundled_models()
    if not models:
        return 0
    from datetime import datetime, timezone

    from app.db.session import get_session_factory
    from app.models.ai import AICorePack

    session = get_session_factory()()
    written = 0
    try:
        for entry in models:
            path = Path(entry["path"])
            row = session.query(AICorePack).filter(AICorePack.core_id == entry["id"]).one_or_none()
            need_hash = calculate_hash and (row is None or row.sha256 is None or row.file_path != str(path))
            if row is None:
                row = AICorePack(core_id=entry["id"])
                session.add(row)
            unchanged = (row.storage == "disk" and row.file_path == str(path)
                         and row.size_bytes == path.stat().st_size
                         and (row.sha256 or not calculate_hash))
            row.name = entry.get("name")
            row.kind = entry.get("kind") or "llm"
            row.license = entry.get("license")
            row.homepage = entry.get("homepage")
            row.filename = entry.get("filename")
            row.purpose = entry.get("purpose")
            row.source_url = entry.get("url")
            row.storage = "disk"
            row.file_path = str(path)
            row.size_bytes = path.stat().st_size
            if need_hash:
                try:
                    row.sha256 = _sha256(path)
                except OSError:
                    pass
            if not row.downloaded_at or not unchanged:
                row.downloaded_at = datetime.now(timezone.utc)
            written += 1
        session.commit()
    finally:
        session.close()
    return written


def sync_bundled_models(layout: Layout | None = None, *, announce=None) -> list[str]:
    """把安装包内的只读模型复制到可写的模型目录。

    为什么需要这一步：程序目录（C:\\Program Files\\AIMS）对普通用户只读，而
    llama.cpp、active.json、数据库里的模型登记都需要一个**唯一且可写**的模型
    目录。安装时一次性复制过去，之后下载、替换、删除都只发生在
    %ProgramData%\\AIMS\\Models，升级覆盖程序目录时也不会把模型删掉。

    幂等：目标文件已存在且大小一致就跳过，不做无谓的 GB 级拷贝。
    """
    layout = layout or load_layout()
    say = announce or (lambda _m: None)
    if layout.models_dir == layout.bundled_models_dir:
        return []
    copied: list[str] = []
    for entry in load_manifest(layout):
        if not _is_model(entry):
            continue
        name = str(entry.get("filename", "")).strip()
        if not name:
            continue
        src = layout.bundled_models_dir / name
        dst = layout.models_dir / name
        if not src.is_file() or src.stat().st_size <= 1024:
            continue
        try:
            if dst.is_file() and dst.stat().st_size == src.stat().st_size:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + ".part")
            say(f"  模型部署 : {name}（{src.stat().st_size / 1e6:.0f} MB）")
            shutil.copyfile(src, tmp)
            tmp.replace(dst)
            copied.append(name)
        except OSError as exc:
            logger.warning("could not stage model %s: %s", name, exc)
            say(f"  ! 模型 {name} 复制失败：{exc}")
    return copied


def probe(layout: Layout | None = None, *, deep: bool = False) -> dict[str, Any]:
    """What the AI engine looks like right now (used by `doctor` and the report)."""
    layout = layout or load_layout()
    info: dict[str, Any] = {"backend": None, "backend_error": None, "models": [], "active": {},
                            "ready": False, "warmup": None}
    try:
        import llama_cpp

        info["backend"] = getattr(llama_cpp, "__version__", "installed")
    except Exception as exc:  # noqa: BLE001
        info["backend_error"] = f"{exc.__class__.__name__}: {exc}"
        return info

    info["models"] = [{"id": m["id"], "file": m.get("filename"), "size_mb": round(m["size_bytes"] / 1e6, 1),
                       "kind": m.get("kind")} for m in bundled_models(layout)]
    registry = registry_path(layout)
    if registry.is_file():
        try:
            info["active"] = json.loads(registry.read_text(encoding="utf-8"))
        except Exception:
            info["active"] = {}
    info["ready"] = bool(info["models"]) and bool(info["active"].get("active_llm"))
    if deep and info["models"]:
        info["warmup"] = warmup(layout)
    return info


def warmup(layout: Layout | None = None, *, kind: str = "embedding") -> dict[str, Any]:
    """Load the active model once and run one inference — the only real proof the
    engine works on this machine (missing DLLs / unsupported CPU only show up here)."""
    layout = layout or load_layout()
    active = {}
    try:
        active = json.loads(registry_path(layout).read_text(encoding="utf-8"))
    except Exception:
        pass
    core_id = active.get(f"active_{kind}")
    entry = next((m for m in bundled_models(layout) if m["id"] == core_id), None)
    if entry is None:
        return {"ok": False, "reason": "没有可用的本地模型文件"}
    try:
        from llama_cpp import Llama

        kwargs = {"model_path": entry["path"], "verbose": False,
                  "n_threads": max(1, min(8, (os.cpu_count() or 4)))}
        if kind == "embedding":
            kwargs.update(embedding=True, n_ctx=512)
        else:
            kwargs.update(n_ctx=2048, n_batch=256)
        model = Llama(**kwargs)
        if kind == "embedding":
            vec = model.create_embedding(["AIMS 离线自检"])["data"][0]["embedding"]
            dim = len(vec)
        else:
            out = model.create_completion("ping", max_tokens=4)
            dim = len(out["choices"][0]["text"] or "")
        del model
        return {"ok": True, "model": core_id, "dim": dim}
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI warmup failed: %s", exc)
        return {"ok": False, "model": core_id, "reason": f"{exc.__class__.__name__}: {exc}"}


def recommended_threads() -> int:
    return max(1, min(8, os.cpu_count() or 4))
