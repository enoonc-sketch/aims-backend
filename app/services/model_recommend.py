"""Recommends which local LLM / embedding model (from cores/manifest.json) fits
this machine right now, based on live RAM/CPU/GPU — powers the "real-time
dynamic model recommendation" panel on AI Center -> 模型下载.

Called fresh on every request (no caching of the *recommendation*, only of the
underlying manifest/system reads, which are already cheap) so it reflects
current free RAM, not just a one-time guess made at install time.
"""
from __future__ import annotations

from typing import Any, Optional

# How much headroom a GGUF file needs once loaded: the raw weights, plus KV
# cache / context buffers / llama.cpp overhead. This is a rule-of-thumb, not a
# precise measurement — it only has to be good enough to rank options.
_RAM_OVERHEAD_FACTOR = 1.35

# required_ram <= available * COMFORTABLE -> "comfortable"
# required_ram <= available * TIGHT       -> "tight" (usable, but little headroom
#                                             for the rest of the app + OS)
# otherwise                                -> "insufficient"
_COMFORTABLE_HEADROOM = 0.6
_TIGHT_HEADROOM = 0.85

_QUALIFYING_SPEEDS = ("fast", "medium")
_QUALIFYING_FITS = ("comfortable", "tight")

# "Small model" mode (prefer="small"): only models up to this many billion
# params are considered, ranked by a simple transparent score
#     score = params_b + zh_score * _ZH_WEIGHT      (zh_score only when lang="zh")
# where `zh_score` (1-3) is a rough per-model Chinese-ability rating kept in
# cores/manifest.json — an editorial estimate, tune it there, not here.
DEFAULT_SMALL_MAX_B = 4.0
_ZH_WEIGHT = 0.8
_SMALL_TOP_N = 3


def _installed_llama_cpp_version() -> Optional[str]:
    try:
        import llama_cpp

        return getattr(llama_cpp, "__version__", None)
    except Exception:
        return None


def _ver_tuple(v: Optional[str]) -> tuple[int, ...]:
    import re

    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def _engine_ok(min_version: Optional[str], installed: Optional[str]) -> Optional[bool]:
    """True/False when we can tell whether the installed llama-cpp-python is new
    enough for this model's architecture; None when there is no requirement or the
    engine isn't installed (unknown — never used to exclude a model)."""
    if not min_version:
        return True
    if not installed:
        return None
    return _ver_tuple(installed) >= _ver_tuple(min_version)


def _ram_fit(required_gb: float, available_gb: float) -> str:
    if available_gb <= 0:
        return "unknown"
    if required_gb <= available_gb * _COMFORTABLE_HEADROOM:
        return "comfortable"
    if required_gb <= available_gb * _TIGHT_HEADROOM:
        return "tight"
    return "insufficient"


def _params_to_billions(params: Optional[str]) -> float:
    if not params:
        return 0.0
    try:
        return float(str(params).upper().rstrip("B"))
    except Exception:
        return 0.0


def _speed_label(params: Optional[str], cpu_cores: int, gpu_detected: bool) -> str:
    if gpu_detected:
        return "fast"
    b = _params_to_billions(params)
    if b <= 0:
        return "fast"  # embeddings / unknown-size — always cheap
    if b <= 2:
        return "fast"
    if b <= 4:
        return "fast" if cpu_cores >= 4 else "medium"
    # 7B and up
    if cpu_cores >= 8:
        return "medium"
    return "slow"


def _evaluate_entry(
    entry: dict[str, Any],
    row_present: bool,
    active: bool,
    sys_info: dict[str, Any],
    lang: str = "zh",
    installed_llama_cpp: Optional[str] = None,
) -> dict[str, Any]:
    size_bytes = entry.get("size_bytes_hint") or 0
    size_gb = size_bytes / 1e9
    required_gb = round(size_gb * _RAM_OVERHEAD_FACTOR, 2)
    fit = _ram_fit(required_gb, sys_info["available_ram_gb"])
    speed = _speed_label(entry.get("params"), sys_info["cpu_cores"], sys_info["gpu_detected"])
    engine_ok = _engine_ok(entry.get("min_llama_cpp"), installed_llama_cpp)
    score = _params_to_billions(entry.get("params"))
    if lang == "zh":
        score += float(entry.get("zh_score") or 0) * _ZH_WEIGHT
    return {
        "id": entry["id"],
        "name": entry.get("name"),
        "kind": entry.get("kind"),
        "params": entry.get("params"),
        "quant": entry.get("quant"),
        "size_bytes": size_bytes,
        "required_ram_gb": required_gb,
        "ram_fit": fit,
        "speed_label": speed,
        "family": entry.get("family"),
        "origin": entry.get("origin"),
        "score": round(score, 2),
        "engine_ok": engine_ok,
        "min_llama_cpp": entry.get("min_llama_cpp"),
        "qualifies": fit in _QUALIFYING_FITS and speed in _QUALIFYING_SPEEDS and engine_ok is not False,
        "in_db": row_present,
        "active": active,
        "recommended": False,  # filled in by recommend_models()
    }


def _pick_best(evaluated: list[dict[str, Any]]) -> Optional[str]:
    if not evaluated:
        return None
    qualifying = [e for e in evaluated if e["qualifies"]]
    pool = qualifying or evaluated
    # Prefer the largest model (best quality) among whatever pool applies;
    # ties broken by better ram_fit, then by not already needing a fresh download.
    fit_rank = {"comfortable": 2, "tight": 1, "unknown": 0, "insufficient": -1}
    best = max(
        pool,
        key=lambda e: (
            _params_to_billions(e.get("params")),
            fit_rank.get(e["ram_fit"], -1),
            e["in_db"],
        ),
    )
    return best["id"]


def _rank_small(
    evaluated: list[dict[str, Any]], max_b: float, diversify: bool = True, overseas_first: bool = False
) -> list[str]:
    """Best small models for *this machine right now*, best first.

    1. candidates: <= max_b params, fits RAM, fast enough, engine new enough;
    2. prefer models with "comfortable" RAM headroom over merely "tight" ones;
    3. rank by score (size + Chinese ability), already-downloaded wins ties;
       with diversify=True each model family (Qwen / Llama / Gemma ...) takes at
       most one slot, so the top-N shows different families instead of three
       Qwen sizes — the #1 pick is unchanged, only the alternatives differ;
       with overseas_first=True every qualifying non-"cn" model outranks every
       "cn" (Qwen) one, so Qwen only shows up as an alternative (or when no
       overseas model fits this machine at all);
    4. if nothing qualifies (very weak machine / old engine), fall back to the
       single smallest model the installed engine can load — better a slow
       answer than no recommendation.
    """
    small = [e for e in evaluated if 0 < _params_to_billions(e.get("params")) <= max_b]
    cands = [e for e in small if e["qualifies"]]
    comfortable = [e for e in cands if e["ram_fit"] == "comfortable"]
    pool = comfortable or cands
    if pool:
        def _key(e):
            return (overseas_first and e.get("origin") != "cn", e["score"], e["in_db"])

        pool = sorted(pool, key=_key, reverse=True)
        if diversify:
            seen: set[str] = set()
            firsts = []
            for e in pool:
                fam = e.get("family") or e["id"]
                if fam not in seen:
                    seen.add(fam)
                    firsts.append(e)
            # families that only have a "tight" fit still deserve a slot if the
            # comfortable pool didn't cover them and there is room left
            if comfortable and len(firsts) < _SMALL_TOP_N:
                for e in sorted(cands, key=_key, reverse=True):
                    fam = e.get("family") or e["id"]
                    if fam not in seen:
                        seen.add(fam)
                        firsts.append(e)
            pool = firsts
        return [e["id"] for e in pool[:_SMALL_TOP_N]]
    loadable = [e for e in evaluated if e["engine_ok"] is not False and _params_to_billions(e.get("params")) > 0]
    if loadable:
        return [min(loadable, key=lambda e: _params_to_billions(e.get("params")))["id"]]
    return []


def recommend_models(
    db,
    prefer: str = "small",
    lang: str = "zh",
    max_small_params_b: float = DEFAULT_SMALL_MAX_B,
    diversify: bool = True,
    overseas_first: bool = False,
) -> dict[str, Any]:
    from app.services import ai_cores, system_info

    sys_info = system_info.get_system_info()
    installed_llama_cpp = _installed_llama_cpp_version()
    packs = {p["id"]: p for p in ai_cores.list_core_packs_from_db(db)}

    llm_eval: list[dict[str, Any]] = []
    emb_eval: list[dict[str, Any]] = []
    for entry in ai_cores.load_manifest():
        kind = entry.get("kind")
        if kind not in ("llm", "embedding"):
            continue
        pack = packs.get(entry["id"], {})
        item = _evaluate_entry(entry, bool(pack.get("in_db")), bool(pack.get("active")), sys_info, lang, installed_llama_cpp)
        (llm_eval if kind == "llm" else emb_eval).append(item)

    small_ids = _rank_small(llm_eval, max_small_params_b, diversify, overseas_first)
    if prefer == "quality":
        recommended_llm = _pick_best(llm_eval)
    else:
        recommended_llm = small_ids[0] if small_ids else _pick_best(llm_eval)
    recommended_embedding = _pick_best(emb_eval)
    for item in llm_eval:
        item["recommended"] = item["id"] == recommended_llm
    for item in emb_eval:
        item["recommended"] = item["id"] == recommended_embedding

    llm_eval.sort(key=lambda e: _params_to_billions(e.get("params")))

    return {
        "system": sys_info,
        "llm_recommendations": llm_eval,
        "embedding_recommendations": emb_eval,
        "recommended_llm_id": recommended_llm,
        "recommended_embedding_id": recommended_embedding,
        "prefer": prefer,
        "small_llm_ids": small_ids,
        "llama_cpp_version": installed_llama_cpp,
    }
