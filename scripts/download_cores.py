#!/usr/bin/env python3
"""Download / copy open-source Data Analysis + AI cores into cores/vendor/ for EXE bundling."""
from __future__ import annotations

import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "cores" / "manifest.json"
VENDOR = ROOT / "cores" / "vendor"
INDEX = VENDOR / "index.json"


def _candidate_urls(core: dict) -> list[str]:
    """Primary url, then any manifest `mirrors` (e.g. ModelScope), then the
    automatic huggingface.co -> hf-mirror.com fallback — same idea as the
    runtime downloader in app/services/ai_cores.py, so a blocked/flaky host
    at build time doesn't hard-fail the whole bundling step."""
    url = core["url"]
    out = [url, *core.get("mirrors", [])]
    if "huggingface.co" in url:
        out.append(url.replace("huggingface.co", "hf-mirror.com", 1))
    seen: set[str] = set()
    deduped = []
    for u in out:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def _with_fallback(core: dict, fn, dest: Path, timeout: int, attempts: int = 2):
    """Try fn(url, dest, timeout) against every candidate URL for `core`, retrying
    each one a couple of times before moving to the next mirror."""
    import time

    errors = []
    for url in _candidate_urls(core):
        for attempt in range(1, attempts + 1):
            try:
                return fn(url, dest, timeout)
            except Exception as e:
                errors.append(f"{url} (第{attempt}次) -> {e}")
                if attempt < attempts:
                    time.sleep(2 * attempt)
    raise RuntimeError("; ".join(errors))


def download(url: str, dest: Path, timeout: int = 60) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "AIMS-CoreDownloader/1.3"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dest.write_bytes(resp.read())
    print(f"  OK  {dest.relative_to(VENDOR)}  ({dest.stat().st_size} bytes)  <- {url}")


def copy_local(url: str, dest: Path) -> None:
    """local://cores/ai/foo.json -> ROOT/cores/ai/foo.json"""
    rel = url.replace("local://", "", 1)
    src = ROOT / rel
    if not src.is_file():
        raise FileNotFoundError(f"local core missing: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    print(f"  OK  {dest.relative_to(VENDOR)}  ({dest.stat().st_size} bytes)  <- {src}")


def download_binary(url: str, dest: Path, timeout: int = 120) -> int:
    """Stream a large file to disk in chunks (used for --include-models), never
    holding the whole thing in memory like download() does for the small JS/JSON cores.
    Resumes from an existing partial (.part) file when the server supports Range,
    so a dropped connection on a multi-GB model doesn't restart from zero."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    resume_from = tmp.stat().st_size if tmp.is_file() else 0
    headers = {"User-Agent": "AIMS-CoreDownloader/1.3"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
    req = urllib.request.Request(url, headers=headers)
    total = resume_from
    mode = "ab" if resume_from else "wb"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resumed = resume_from and resp.status == 206
            if resume_from and not resumed:
                total = 0
                mode = "wb"
            with open(tmp, mode) as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
    except urllib.error.HTTPError as e:
        if e.code == 416:  # already fully downloaded per server
            tmp.unlink(missing_ok=True)
            return download_binary(url, dest, timeout)
        raise
    tmp.replace(dest)
    print(f"  OK  {dest.relative_to(VENDOR)}  ({total} bytes)  <- {url}")
    return total


def main() -> int:
    if not MANIFEST.is_file():
        print(f"[ERROR] manifest not found: {MANIFEST}")
        return 1

    include_models = "--include-models" in sys.argv

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cores = manifest.get("cores") or []
    VENDOR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(" AIMS — download Data Analysis + AI CORES")
    print("=" * 60)
    print(f" Vendor dir: {VENDOR}\n")
    if not include_models:
        print(
            " NOTE: real LLM/embedding models (kind=llm/embedding, potentially several GB)\n"
            " are SKIPPED by this build-time script by default — they are meant to be\n"
            " downloaded on-demand by the end user from the AI Center > 模型下载 tab, not\n"
            " baked into every EXE installer. Pass --include-models to bundle them anyway.\n"
        )

    results = []
    for core in cores:
        cid = core.get("id", "?")
        filename = core["filename"]
        dest = VENDOR / filename
        is_binary_model = core.get("download_type") == "binary"
        if is_binary_model and not include_models:
            print(f"  SKIP {cid} (real model, {core.get('size_bytes_hint', '?')} bytes — use --include-models to bundle)")
            continue
        entry = {
            "id": cid,
            "name": core.get("name"),
            "license": core.get("license"),
            "homepage": core.get("homepage"),
            "filename": filename,
            "purpose": core.get("purpose"),
            "local_path": f"/cores/{filename}",
            "ok": False,
            "kind": "ai" if filename.startswith("ai/") else "data",
        }
        if dest.is_file() and dest.stat().st_size > 50:
            print(f"  SKIP {filename} (exists, {dest.stat().st_size} bytes)")
            entry["ok"] = True
            entry["bytes"] = dest.stat().st_size
            results.append(entry)
            continue
        try:
            print(f"  GET  {cid} ...")
            url = core["url"]
            if url.startswith("local://"):
                copy_local(url, dest)
            elif is_binary_model:
                _with_fallback(core, download_binary, dest, 120)
            else:
                _with_fallback(core, download, dest, 60)
            entry["ok"] = True
            entry["bytes"] = dest.stat().st_size
        except Exception as e:
            entry["error"] = str(e)
            print(f"  FAIL {cid}: {e}")
        results.append(entry)

    INDEX.write_text(json.dumps({"cores": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    ok_n = sum(1 for r in results if r.get("ok"))
    print(f"\nDone: {ok_n}/{len(results)} cores ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
