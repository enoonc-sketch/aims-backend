import hashlib
import http.server
import threading

import pytest

from app.services import ai_cores


def test_candidate_urls_adds_hf_mirror_fallback():
    url = "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/model.gguf"
    candidates = ai_cores._candidate_urls(url)
    assert candidates[0] == url
    assert candidates[1] == "https://hf-mirror.com/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/model.gguf"


def test_candidate_urls_leaves_non_hf_urls_alone():
    url = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"
    assert ai_cores._candidate_urls(url) == [url]


def _start_file_server(content: bytes):
    """Serve `content` at / on an ephemeral localhost port; returns (base_url, thread)."""

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
    return f"http://127.0.0.1:{port}/model.gguf", server


def test_download_binary_to_disk_streams_and_hashes_correctly(tmp_path):
    content = b"fake-gguf-bytes" * 100000  # a few MB, enough to cross a couple of chunk boundaries
    url, server = _start_file_server(content)
    try:
        entry = {"id": "test-model", "url": url}
        dest = tmp_path / "model.gguf"
        size, sha = ai_cores._download_binary_to_disk(entry, dest)
        assert size == len(content)
        assert sha == hashlib.sha256(content).hexdigest()
        assert dest.read_bytes() == content
        assert not dest.with_suffix(dest.suffix + ".part").exists()
    finally:
        server.shutdown()


def test_download_binary_to_disk_falls_back_to_second_candidate(tmp_path, monkeypatch):
    content = b"mirror-content"
    good_url, server = _start_file_server(content)
    bad_url = "http://127.0.0.1:1/nope.gguf"  # nothing listens here -> connection refused

    monkeypatch.setattr(ai_cores, "_candidate_urls", lambda url: [bad_url, good_url])
    try:
        entry = {"id": "test-model", "url": "https://huggingface.co/whatever/model.gguf"}
        dest = tmp_path / "model2.gguf"
        size, sha = ai_cores._download_binary_to_disk(entry, dest)
        assert size == len(content)
        assert sha == hashlib.sha256(content).hexdigest()
    finally:
        server.shutdown()


def test_download_binary_to_disk_raises_combined_error_when_all_candidates_fail(monkeypatch, tmp_path):
    bad_url_1 = "http://127.0.0.1:1/a.gguf"
    bad_url_2 = "http://127.0.0.1:2/b.gguf"
    monkeypatch.setattr(ai_cores, "_candidate_urls", lambda url: [bad_url_1, bad_url_2])

    entry = {"id": "test-model", "url": "https://huggingface.co/whatever/model.gguf"}
    with pytest.raises(RuntimeError) as exc_info:
        ai_cores._download_binary_to_disk(entry, tmp_path / "model3.gguf")
    msg = str(exc_info.value)
    assert bad_url_1 in msg and bad_url_2 in msg


def test_binary_candidate_urls_prefers_manifest_mirror_over_hf_mirror():
    entry = {
        "url": "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/model.gguf",
        "mirrors": ["https://modelscope.cn/models/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/master/model.gguf"],
    }
    candidates = ai_cores._binary_candidate_urls(entry)
    assert candidates == [
        entry["url"],
        entry["mirrors"][0],
        "https://hf-mirror.com/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/model.gguf",
    ]


def test_binary_candidate_urls_dedupes():
    entry = {"url": "https://example.com/model.gguf", "mirrors": ["https://example.com/model.gguf"]}
    assert ai_cores._binary_candidate_urls(entry) == ["https://example.com/model.gguf"]


def _start_resumable_server(content: bytes):
    """Like _start_file_server, but honours a Range: bytes=N- header with 206,
    so resume-from-partial-file can be exercised."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start = int(rng.split("=", 1)[1].split("-", 1)[0])
                body = content[start:]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(content) - 1}/{len(content)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
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
    return f"http://127.0.0.1:{port}/model.gguf", server


def test_download_binary_to_disk_resumes_partial_file(tmp_path):
    content = b"abcdefgh" * 500000  # a few MB
    url, server = _start_resumable_server(content)
    try:
        entry = {"id": "test-model", "url": url}
        dest = tmp_path / "model.gguf"
        tmp = dest.with_suffix(dest.suffix + ".part")
        # Pretend the first half already downloaded from a previous, interrupted attempt.
        half = len(content) // 2
        tmp.write_bytes(content[:half])

        size, sha = ai_cores._download_binary_to_disk(entry, dest)
        assert size == len(content)
        assert sha == hashlib.sha256(content).hexdigest()
        assert dest.read_bytes() == content
        assert not tmp.exists()
    finally:
        server.shutdown()


def test_fetch_core_bytes_falls_back_to_mirror(monkeypatch, tmp_path):
    content = b'{"ok": true}'
    good_url, server = _start_file_server(content)
    bad_url = "http://127.0.0.1:1/nope.json"

    monkeypatch.setattr(ai_cores, "_candidate_urls", lambda url: [bad_url, good_url])
    try:
        entry = {"id": "test-pack", "url": "https://huggingface.co/whatever/pack.json"}
        raw = ai_cores._fetch_core_bytes(entry)
        assert raw == content
    finally:
        server.shutdown()


def test_download_survives_bracketed_ipv6_in_no_proxy(monkeypatch, tmp_path):
    """Regression: httpx.Client() raises InvalidURL on NO_PROXY entries like
    "[::1]" (it parses ':1]' as a port) while building the client, before any
    request is made — so every core download failed in such environments.
    The downloader must fall back to trust_env=False instead."""
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,[::1]")
    content = b"no-proxy-ipv6" * 1000
    url, server = _start_file_server(content)
    try:
        entry = {"id": "test-model", "url": url}
        dest = tmp_path / "model.gguf"
        size, sha = ai_cores._download_binary_to_disk(entry, dest)
        assert size == len(content)
        assert sha == hashlib.sha256(content).hexdigest()
        assert dest.read_bytes() == content
    finally:
        server.shutdown()
