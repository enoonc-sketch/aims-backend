"""GET /ai/packages 的 installable / bundled 标志。

自 1.0.8 平行包移植（后端部分）。平行包里同文件的其余测试（前端“已内置”状态卡、
构建脚本强制内置引擎）对应的是另一种引擎方案，本版不合，故不在此列。
"""
from __future__ import annotations

from app.services import py_packages


def test_list_packages_marks_everything_installable_outside_the_exe(monkeypatch):
    monkeypatch.setattr(py_packages, "is_frozen", lambda: False)
    pk = py_packages.list_packages()
    assert pk and all(p["installable"] is True and p["bundled"] is False for p in pk)


def test_list_packages_in_frozen_exe_is_bundled_state_not_installable(monkeypatch):
    monkeypatch.setattr(py_packages, "is_frozen", lambda: True)
    monkeypatch.setattr(py_packages, "is_installed", lambda name: (name == "llama_cpp", "0.3.2" if name == "llama_cpp" else None))
    by_import = {}
    for p in py_packages.list_packages():
        assert p["installable"] is False
        by_import.setdefault(p["import_name"], []).append(p)
    assert all(p["bundled"] is True for p in by_import["llama_cpp"])
    assert all(p["bundled"] is False for name, ps in by_import.items() if name != "llama_cpp" for p in ps)


def test_packages_api_exposes_the_flags(client):
    pk = client.get("/api/v1/ai/packages").json()
    assert pk and {"installable", "bundled"} <= set(pk[0])


def test_installing_in_frozen_exe_is_still_refused(client, monkeypatch):
    monkeypatch.setattr(py_packages, "is_frozen", lambda: True)
    r = client.post("/api/v1/ai/packages/llama-cpu/install")
    assert r.status_code == 400
