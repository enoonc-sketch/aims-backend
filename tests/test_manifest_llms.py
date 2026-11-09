import json
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "cores" / "manifest.json"


def _llms():
    return [c for c in json.loads(MANIFEST.read_text(encoding="utf-8"))["cores"] if c.get("kind") == "llm"]


def test_llm_entries_have_required_fields_and_unique_ids():
    llms = _llms()
    assert len({c["id"] for c in llms}) == len(llms)
    for c in llms:
        for key in ("id", "name", "url", "filename", "params", "quant", "size_bytes_hint", "engine"):
            assert c.get(key), f"{c.get('id')} missing {key}"
        assert c["url"].endswith(".gguf") and c["filename"].endswith(".gguf")
        assert c["filename"].startswith("llm/")


def test_manifest_has_non_qwen_llms():
    assert any("qwen" not in c["id"] for c in _llms())
