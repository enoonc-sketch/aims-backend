"""Standalone native engine installer (Model Download page, 1.0.9).

Covers the three things that must not silently regress:
- release selection skips the v1.0.0 ``nightly-tag.txt`` release and picks the
  newest ``bNNNNN`` nightly that actually carries the wanted asset;
- downloads are sha256-verified and extraction is zip-slip guarded;
- install/uninstall round-trips on disk (with a localhost fake release, so
  no real GitHub download happens in tests).
"""

from __future__ import annotations

import hashlib
import http.server
import io
import threading
import zipfile

import pytest

from app.services import engine_installer
from app.services.engine_installer import EngineInstallError

# Asset names as published on llama.cpp release b12375 (verified live
# 2026-11-14 via the GitHub Releases API). If upstream renames assets,
# these tests — not a user on Windows — should be the first to notice.
B12375_ASSETS = [
    "llama-b12375-bin-win-cpu-x64.zip",
    "llama-b12375-bin-win-cuda-12.4-x64.zip",
    "cudart-llama-bin-win-cuda-12.4-x64.zip",
    "llama-b12375-bin-win-vulkan-x64.zip",
]


def _fake_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("llama-server.exe", "fake llama-server binary")
        zf.writestr("llama-cli.exe", "fake llama-cli binary")
        zf.writestr("ggml.dll", "fake dll")
    return buf.getvalue()


def _start_file_server(content: bytes):
    """Serve `content` at / on an ephemeral localhost port."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    return f"http://127.0.0.1:{port}/llama.zip", server


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        return _FakeResp(self._payload)


def _releases_payload():
    return [
        {"tag_name": "v1.0.0", "prerelease": False,
         "assets": [{"name": "nightly-tag.txt",
                     "browser_download_url": "https://example.invalid/nightly-tag.txt"}]},
        {"tag_name": "b12375", "prerelease": True,
         "assets": [{"name": n, "browser_download_url": f"https://example.invalid/{n}",
                     "digest": "sha256:deadbeef", "size": 123} for n in B12375_ASSETS]},
    ]


# ------------------------------------------------------------------ catalog
def test_catalog_has_three_windows_engines_with_matching_patterns():
    ids = [e["id"] for e in engine_installer.ENGINE_CATALOG]
    assert ids == ["llama-cpu", "llama-cuda124", "llama-vulkan"]
    import re

    for entry in engine_installer.ENGINE_CATALOG:
        pat = re.compile(entry["asset_pattern"])
        assert any(pat.fullmatch(n) for n in B12375_ASSETS), entry["id"]
    cuda = next(e for e in engine_installer.ENGINE_CATALOG if e["id"] == "llama-cuda124")
    assert re.compile(cuda["companion_pattern"]).fullmatch(
        "cudart-llama-bin-win-cuda-12.4-x64.zip")
    # CUDA is the big one — the UI must warn about the total download size.
    assert cuda["download_size_hint"] > 500_000_000


def test_list_engines_reports_not_installed_on_empty_dir(tmp_path):
    rows = engine_installer.list_engines(models_root=tmp_path)
    assert len(rows) == 3
    assert all(not r["installed"] for r in rows)
    assert all(r["server_path"] is None for r in rows)


# ------------------------------------------------------------------ release selection
def test_resolve_release_skips_versioned_release_without_binaries(monkeypatch):
    monkeypatch.setattr(engine_installer, "_http_client",
                        lambda: _FakeClient(_releases_payload()))
    resolved = engine_installer.resolve_release(r"^llama-b\d+-bin-win-cpu-x64\.zip$")
    assert resolved["tag"] == "b12375"
    assert resolved["asset"]["name"] == "llama-b12375-bin-win-cpu-x64.zip"
    assert resolved["companion"] is None


def test_resolve_release_pairs_cuda_with_cudart_companion(monkeypatch):
    monkeypatch.setattr(engine_installer, "_http_client",
                        lambda: _FakeClient(_releases_payload()))
    resolved = engine_installer.resolve_release(
        r"^llama-b\d+-bin-win-cuda-12\.4-x64\.zip$",
        r"^cudart-llama-bin-win-cuda-12\.4-x64\.zip$")
    assert resolved["asset"]["name"] == "llama-b12375-bin-win-cuda-12.4-x64.zip"
    assert resolved["companion"]["name"] == "cudart-llama-bin-win-cuda-12.4-x64.zip"


def test_resolve_release_raises_when_no_release_has_the_asset(monkeypatch):
    monkeypatch.setattr(engine_installer, "_http_client",
                        lambda: _FakeClient(_releases_payload()))
    with pytest.raises(EngineInstallError):
        engine_installer.resolve_release(r"^llama-b\d+-bin-win-sycl-x64\.zip$")


# ------------------------------------------------------------------ extraction safety
def test_safe_extract_rejects_zip_slip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "x")
    bad = tmp_path / "bad.zip"
    bad.write_bytes(buf.getvalue())
    with pytest.raises(EngineInstallError):
        engine_installer._safe_extract_zip(bad, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_rejects_symlink_entries(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link.exe")
        info.external_attr = 0o120777 << 16  # symlink
        zf.writestr(info, "llama-server.exe")
    bad = tmp_path / "bad.zip"
    bad.write_bytes(buf.getvalue())
    with pytest.raises(EngineInstallError):
        engine_installer._safe_extract_zip(bad, tmp_path / "out")


# ------------------------------------------------------------------ install / uninstall
def test_install_and_uninstall_round_trip_with_fake_release(tmp_path):
    content = _fake_zip_bytes()
    url, server = _start_file_server(content)
    try:
        asset = {"name": "llama-b12375-bin-win-cpu-x64.zip",
                 "browser_download_url": url,
                 "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                 "size": len(content)}
        release = {"tag": "b12375", "asset": asset, "companion": None}
        row = engine_installer.install_engine(
            "llama-cpu", models_root=tmp_path, release=release, allow_non_windows=True)
        assert row["installed"] is True
        assert row["tag"] == "b12375"
        assert row["server_path"] and row["server_path"].endswith("llama-server.exe")
        assert (tmp_path / "engines" / "llama-cpu" / "llama-server.exe").is_file()
        # state file written; list picks it up
        listed = engine_installer.get_engine("llama-cpu", models_root=tmp_path)
        assert listed["installed"] is True
        assert listed["verify_ok"] is False  # non-Windows: no --version run, honestly reported
        out = engine_installer.uninstall_engine("llama-cpu", models_root=tmp_path)
        assert out["uninstalled"] is True
        assert not (tmp_path / "engines" / "llama-cpu").exists()
        assert engine_installer.get_engine("llama-cpu", models_root=tmp_path)["installed"] is False
    finally:
        server.shutdown()


def test_install_rejects_bad_sha256(tmp_path):
    content = _fake_zip_bytes()
    url, server = _start_file_server(content)
    try:
        asset = {"name": "llama-b12375-bin-win-cpu-x64.zip",
                 "browser_download_url": url,
                 "digest": "sha256:" + "0" * 64, "size": len(content)}
        with pytest.raises(EngineInstallError, match="sha256"):
            engine_installer.install_engine(
                "llama-cpu", models_root=tmp_path,
                release={"tag": "b12375", "asset": asset, "companion": None},
                allow_non_windows=True)
        assert not (tmp_path / "engines" / "llama-cpu" / "llama-server.exe").exists()
    finally:
        server.shutdown()


def test_install_unknown_engine_and_non_windows_guard(tmp_path):
    with pytest.raises(EngineInstallError, match="未知"):
        engine_installer.install_engine("llama-nope", models_root=tmp_path,
                                        allow_non_windows=True)
    with pytest.raises(EngineInstallError, match="仅支持 Windows"):
        engine_installer.install_engine("llama-cpu", models_root=tmp_path,
                                        release={"tag": "b1", "asset": {}, "companion": None})


def test_uninstall_unknown_engine_raises(tmp_path):
    with pytest.raises(EngineInstallError, match="未知"):
        engine_installer.uninstall_engine("llama-nope", models_root=tmp_path)


# ------------------------------------------------------------------ API
def test_api_lists_native_engines(client):
    resp = client.get("/api/v1/ai/engines")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["id"] for r in rows] == ["llama-cpu", "llama-cuda124", "llama-vulkan"]
    assert all("installed" in r and "download_size_hint" in r for r in rows)


def test_api_install_unknown_engine_is_400(client):
    resp = client.post("/api/v1/ai/engines/llama-nope/install")
    assert resp.status_code == 400, resp.text


def test_api_uninstall_requires_explicit_confirm(client):
    resp = client.delete("/api/v1/ai/engines/llama-cpu")
    assert resp.status_code == 400, resp.text
    assert "confirm=true" in resp.text
