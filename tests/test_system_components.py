"""阶段 3-5 补齐：统一组件清单 API（总方案 8.1）+ 模型卸载/删除分离（7.3/7.4）。"""
import shutil
from pathlib import Path

import pytest


def test_components_endpoint_lists_real_components(client):
    resp = client.get("/api/v1/system/components")
    assert resp.status_code == 200
    data = resp.json()
    names = {c["name"] for c in data["components"]}
    assert "Backend (FastAPI)" in names
    assert data["app_version"] and data["python_version"]
    backend = next(c for c in data["components"] if c["name"].startswith("Backend"))
    assert backend["status"] == "running" and backend["version"]
    # AI 引擎组件必须存在且状态如实（未装 llama_cpp 时 not-installed，不得伪造）
    engine = next(c for c in data["components"] if "AI Engine" in c["name"])
    assert engine["status"] in ("installed", "not-installed")
    if engine["status"] == "installed":
        assert engine["version"] and engine["path"]


def test_components_requires_auth(anon_client):
    assert anon_client.get("/api/v1/system/components").status_code == 401


# ------------------------------------------------------------------ 模型卸载/删除
@pytest.fixture()
def fake_binary_model(db_session, monkeypatch, tmp_path):
    """构造一个已"下载"的假 GGUF 模型（真实文件 + DB 行）。"""
    from app.models.ai import AICorePack
    from app.services import ai_cores

    entry = next(e for e in ai_cores.load_manifest() if ai_cores._entry_kind(e) == "llm"
                 and e.get("download_type") == "binary")
    models_dir = tmp_path / "Models"
    models_dir.mkdir()
    f = models_dir / entry["filename"]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"GGUF" + b"x" * 2048)

    monkeypatch.setattr("app.core.config.settings.MODELS_DIR", str(models_dir), raising=False)
    row = AICorePack(core_id=entry["id"], name=entry["id"], kind="ai",
                     filename=entry["filename"], storage="disk", file_path=str(f),
                     sha256="deadbeef", size_bytes=f.stat().st_size)
    db_session.add(row)
    db_session.commit()
    return entry, f


def test_delete_requires_confirm(client, fake_binary_model):
    entry, f = fake_binary_model
    r = client.delete(f"/api/v1/ai/cores/{entry['id']}")
    assert r.status_code == 400 and "confirm" in r.json()["detail"]
    assert f.exists()  # 未确认不动文件


def test_delete_active_model_refused(client, db_session, fake_binary_model):
    from app.services import model_registry
    entry, f = fake_binary_model
    model_registry.set_active("llm", entry["id"])
    r = client.delete(f"/api/v1/ai/cores/{entry['id']}?confirm=true")
    assert r.status_code == 409
    assert f.exists()


def test_delete_removes_file_and_resets_row(client, db_session, fake_binary_model):
    from app.models.ai import AICorePack
    from app.services import model_registry
    entry, f = fake_binary_model
    # 把激活模型切到另一个（默认模型禁止删除，须先切换）
    other = next(e for e in __import__("app.services.ai_cores", fromlist=["load_manifest"]).load_manifest()
                 if __import__("app.services.ai_cores", fromlist=["_entry_kind"])._entry_kind(e) == "llm"
                 and e["id"] != entry["id"])
    model_registry.set_active("llm", other["id"])
    r = client.delete(f"/api/v1/ai/cores/{entry['id']}?confirm=true")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True and body["file_removed"] is True and body["freed_bytes"] > 0
    assert not f.exists()
    row = db_session.query(AICorePack).filter(AICorePack.core_id == entry["id"]).one()
    assert row.file_path is None and row.sha256 is None
    # manifest 中仍可见、标记为未下载（可重新下载）
    r2 = client.get("/api/v1/ai/cores")
    pack = next(p for p in r2.json() if p["id"] == entry["id"])
    assert pack["in_db"] is False and pack["active"] is False


def test_delete_rules_pack_refused(client):
    """内置规则包（JSON）与业务绑定，不允许通过模型管理删除。"""
    from app.services import ai_cores
    entry = next(e for e in ai_cores.load_manifest() if ai_cores._entry_kind(e) == "ai")
    r = client.delete(f"/api/v1/ai/cores/{entry['id']}?confirm=true")
    assert r.status_code == 409 and "规则包" in r.json()["detail"]


def test_delete_outside_models_dir_refused(client, db_session, monkeypatch, tmp_path):
    """DB 记录被篡改指向任意文件时，删除必须拒绝（任意文件删除防护）。"""
    from app.models.ai import AICorePack
    from app.services import ai_cores

    entry = next(e for e in ai_cores.load_manifest() if ai_cores._entry_kind(e) == "llm"
                 and e.get("download_type") == "binary")
    victim = tmp_path / "important.txt"
    victim.write_text("do not delete")
    monkeypatch.setattr("app.core.config.settings.MODELS_DIR", str(tmp_path / "Models"), raising=False)
    db_session.add(AICorePack(core_id=entry["id"], name=entry["id"], kind="ai",
                              filename=entry["filename"], storage="disk",
                              file_path=str(victim), sha256="x", size_bytes=10))
    db_session.commit()
    r = client.delete(f"/api/v1/ai/cores/{entry['id']}?confirm=true")
    assert r.status_code == 409
    assert victim.exists()  # 文件未被误删


# ------------------------------------------------------------------ 模型目录扫描导入
def test_scan_local_models_registers_untracked_gguf(client, db_session, monkeypatch, tmp_path):
    """把一个真实 GGUF 文件放进模型目录后扫描 → 应登记（SHA-256 + local-import 来源）。"""
    from app.models.ai import AICorePack
    from app.services import ai_cores

    models_dir = tmp_path / "Models"
    (models_dir / "llm").mkdir(parents=True)
    f = models_dir / "llm" / "my-offline-model.gguf"
    f.write_bytes(b"GGUF" + b"weights" * 300)

    monkeypatch.setattr("app.core.config.settings.MODELS_DIR", str(models_dir), raising=False)
    r = client.post("/api/v1/ai/cores/scan-local")
    assert r.status_code == 200
    body = r.json()
    assert len(body["registered"]) == 1
    reg = body["registered"][0]
    assert reg["filename"] == "llm/my-offline-model.gguf"
    assert len(reg["sha256"]) == 64
    row = db_session.query(AICorePack).filter(AICorePack.core_id == reg["id"]).one()
    assert row.source_url == "local-import" and row.sha256 == reg["sha256"]
    # 幂等：再扫一次不重复登记
    r2 = client.post("/api/v1/ai/cores/scan-local")
    assert r2.json()["registered"] == []


def test_scan_local_models_rejects_non_gguf(client, db_session, monkeypatch, tmp_path):
    models_dir = tmp_path / "Models"
    models_dir.mkdir(parents=True)
    (models_dir / "fake.gguf").write_bytes(b"NOTGGUF-malicious")
    monkeypatch.setattr("app.core.config.settings.MODELS_DIR", str(models_dir), raising=False)
    r = client.post("/api/v1/ai/cores/scan-local")
    assert r.status_code == 200
    assert r.json()["registered"] == []
    assert any("非GGUF" in s for s in r.json()["skipped"])


# ------------------------------------------------------------------ 引擎信息端点
def test_engine_info_endpoint(client):
    r = client.get("/api/v1/ai/engine")
    assert r.status_code == 200
    data = r.json()
    assert "llama.cpp" in data["engine"]
    assert data["params"]["ctx"] and data["hardware"]["cpu_cores"]
    # 未安装 llama_cpp 时 installed 如实为 False，version 为 null（不伪造）
    if not data["installed"]:
        assert data["version"] is None
