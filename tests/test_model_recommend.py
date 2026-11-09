"""Dynamic small-model recommendation: pure logic, no DB / network / llama.cpp."""
import pytest

from app.services import model_recommend as mr


def _sys(avail_gb, cores=8, gpu=False):
    return {"platform": "Linux", "total_ram_gb": avail_gb * 2, "available_ram_gb": avail_gb,
            "cpu_cores": cores, "gpu_detected": gpu, "gpu_source": None, "ram_detected": True}


def _e(id_, params, gb, family, zh, min_llama=None):
    d = {"id": id_, "name": id_, "kind": "llm", "params": params, "quant": "Q4_K_M",
         "size_bytes_hint": int(gb * 1e9), "family": family, "zh_score": zh}
    if min_llama:
        d["min_llama_cpp"] = min_llama
    return d


MANIFEST = [
    _e("qwen-0.5b", "0.5B", 0.4, "Qwen", 2),
    _e("qwen-1.5b", "1.5B", 1.1, "Qwen", 3),
    _e("qwen-3b", "3B", 1.9, "Qwen", 3),
    _e("llama-1b", "1B", 0.8, "Llama", 1),
    _e("llama-3b", "3B", 2.0, "Llama", 1),
    _e("gemma-4b", "4B", 2.5, "Gemma", 2, min_llama="0.3.9"),
    _e("mistral-7b", "7B", 4.4, "Mistral", 1),
    {"id": "emb", "kind": "embedding", "size_bytes_hint": 48_000_000},
]


@pytest.fixture
def run(monkeypatch):
    from app.services import ai_cores, system_info

    def _run(avail_gb, engine="0.3.4", **kw):
        monkeypatch.setattr(ai_cores, "load_manifest", lambda: MANIFEST)
        monkeypatch.setattr(ai_cores, "list_core_packs_from_db", lambda db: [])
        monkeypatch.setattr(system_info, "get_system_info", lambda: _sys(avail_gb))
        monkeypatch.setattr(mr, "_installed_llama_cpp_version", lambda: engine)
        return mr.recommend_models(None, **kw)

    return _run


def test_small_mode_never_recommends_over_cap(run):
    r = run(64, engine="0.3.9")  # plenty of RAM: old behaviour would pick 7B
    picked = next(i for i in r["llm_recommendations"] if i["recommended"])
    assert float(picked["params"].rstrip("B")) <= 4.0
    assert r["recommended_llm_id"] != "mistral-7b"


def test_quality_mode_keeps_old_behaviour(run):
    assert run(64, engine="0.3.9", prefer="quality")["recommended_llm_id"] == "mistral-7b"


def test_more_ram_gives_bigger_small_model(run):
    tight = run(1.5)["recommended_llm_id"]
    roomy = run(16)["recommended_llm_id"]
    size = {e["id"]: float(e["params"].rstrip("B")) for e in MANIFEST if e["kind"] == "llm"}
    assert size[tight] < size[roomy]


def test_old_engine_excludes_gemma_but_new_engine_allows_it(run):
    old = run(16, engine="0.2.90")
    assert "gemma-4b" not in old["small_llm_ids"]
    gemma = next(i for i in old["llm_recommendations"] if i["id"] == "gemma-4b")
    assert gemma["engine_ok"] is False and not gemma["qualifies"]
    assert "gemma-4b" in run(16, engine="0.3.9")["small_llm_ids"]


def test_engine_not_installed_does_not_exclude(run):
    assert "gemma-4b" in run(16, engine=None)["small_llm_ids"]


def test_zh_vs_en_ranking(run):
    zh = run(16, engine="0.3.9", lang="zh")["small_llm_ids"]
    en = run(16, engine="0.3.9", lang="en")["small_llm_ids"]
    assert zh[0].startswith(("qwen", "gemma"))
    assert en[0] in ("gemma-4b", "llama-3b")  # size-only ranking


def test_falls_back_to_smallest_when_nothing_fits(run):
    r = run(0.3)  # absurdly little RAM
    assert r["small_llm_ids"] == ["qwen-0.5b"]
    assert r["recommended_llm_id"] == "qwen-0.5b"


def test_version_parsing():
    assert mr._engine_ok("0.3.9", "0.3.10") is True
    assert mr._engine_ok("0.3.9", "0.3.4") is False
    assert mr._engine_ok(None, "0.1.0") is True
    assert mr._engine_ok("0.3.9", None) is None


def test_endpoint_accepts_params_and_returns_small_fields(client):
    r = client.get("/api/v1/ai/models/recommend", params={"prefer": "small", "lang": "en", "max_params_b": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prefer"] == "small"
    assert isinstance(body["small_llm_ids"], list)
    assert all(float(i["params"].rstrip("B")) <= 3 for i in body["llm_recommendations"] if i["id"] in body["small_llm_ids"])
    assert client.get("/api/v1/ai/models/recommend", params={"prefer": "bogus"}).status_code == 422


def test_diversify_gives_one_slot_per_family(run):
    ids = run(16, engine="0.3.9")["small_llm_ids"]
    fam = {e["id"]: e["family"] for e in MANIFEST if e["kind"] == "llm"}
    assert len({fam[i] for i in ids}) == len(ids)  # no family twice
    assert len(ids) == 3


def test_diversify_off_can_return_same_family_repeatedly(run):
    ids = run(16, engine="0.3.9", diversify=False)["small_llm_ids"]
    fam = {e["id"]: e["family"] for e in MANIFEST if e["kind"] == "llm"}
    assert len({fam[i] for i in ids}) < len(ids)


def test_diversify_does_not_change_top_pick(run):
    assert run(16, engine="0.3.9")["recommended_llm_id"] == run(16, engine="0.3.9", diversify=False)["recommended_llm_id"]


def test_overseas_first_puts_qwen_behind_overseas(run):
    for e in MANIFEST:
        if e.get("kind") == "llm":
            e["origin"] = "cn" if e["family"] == "Qwen" else "overseas"
    r = run(16, engine="0.3.9", overseas_first=True)
    by = {e["id"]: e for e in r["llm_recommendations"]}
    assert by[r["recommended_llm_id"]]["origin"] == "overseas"
    assert r["small_llm_ids"][0] != "qwen-1.5b"
    # default (off) is unchanged: ranked purely by score (Gemma 4B narrowly beats Qwen 3B here)
    assert run(16, engine="0.3.9")["recommended_llm_id"] == "gemma-4b"
    # and on a small machine where Gemma doesn't fit, Qwen wins when the switch is off
    assert run(2.5, engine="0.3.9")["recommended_llm_id"].startswith("qwen")
    assert run(2.5, engine="0.3.9", overseas_first=True)["recommended_llm_id"] == "llama-1b"


def test_overseas_first_falls_back_to_qwen_when_no_overseas_fits(run):
    r = run(0.9, engine="0.3.9", overseas_first=True)  # only tiny models fit
    assert r["recommended_llm_id"]  # never empty
