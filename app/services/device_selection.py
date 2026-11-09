"""Pure-Python core of AI Center's cross-menu device search and 选型 (selection).

No database, no FastAPI, no pydantic in here on purpose: everything works on
plain "profile" dicts (one per device, assembled from the Devices / Comm / Data /
Alarms / Maintenance / Assets / Lab tables by app/services/device_search.py), so
the ranking rules can be unit-tested without any infrastructure.

Two features live here:

* 搜索 (search): free text such as "在线 GC H2S" or "Analyzer-002" is parsed into
  hard *facets* (status / component / analyzer type / protocol) plus free
  *terms* matched across code, name, model, location, tags, gateways, assets...
* 选型 (selection): rank existing devices — and, from the fleet evidence in this
  platform, device *models* — against a requirement (which components must be
  measured, analyzer type, protocol, online only, ...). Every score comes with
  bilingual reasons so the UI can show *why* a device was picked.

Profile shape (all keys optional except id/device_code)::

    {"id": 1, "device_code": "Analyzer-001", "name": "...", "status": "online",
     "health_score": 96.5, "protocol": "Modbus TCP", "location": "...",
     "ip_address": "...", "model_name": "GC-2000", "manufacturer": "...",
     "analyzer_type": "GC",
     "components": [{"name": "Nitrogen", "unit": "%", "gateway": "GW-001"}],
     "components_with_data": ["Nitrogen"], "gateways": ["GW-001"],
     "asset_code": "AST-1", "asset_name": "...",
     "active_alarms": 0, "open_work_orders": 0, "points_24h": 24, "lab_samples": 0,
     "certifications": {"hydrogen sulfide": {"status": "certified", "score": 96.0, "expired": False}},
     "roi_flags": ["high_maintenance_ratio"]}

`certifications` and `roi_flags` are both optional and, when present, feed the AI-selection score
(see `_cert_signal` / `_cost_signal`): AI-certification evidence from app/services/lab_analysis.py
(via lab_ai_certifications) and cost-side flags from app/services/roi_analysis.py. A device that
was simply never certified, or has no linked asset, is not penalised for either — these are
bonuses/penalties only when there is actual evidence.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Vocabulary (analyzer domain, bilingual EN/CN)
# --------------------------------------------------------------------------
# canonical key -> (display name, aliases). Keys are lower-case canonical names.
COMPONENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "nitrogen": ("Nitrogen", ("nitrogen", "n2", "氮气", "氮")),
    "oxygen": ("Oxygen", ("oxygen", "o2", "氧气")),
    "carbon monoxide": ("Carbon Monoxide", ("carbon monoxide", "co", "一氧化碳")),
    "carbon dioxide": ("Carbon Dioxide", ("carbon dioxide", "co2", "二氧化碳")),
    "methane": ("Methane", ("methane", "ch4", "甲烷")),
    "ethylene": ("Ethylene", ("ethylene", "c2h4", "乙烯")),
    "propylene": ("Propylene", ("propylene", "c3h6", "丙烯")),
    "ethane": ("Ethane", ("ethane", "c2h6", "乙烷")),
    "propane": ("Propane", ("propane", "c3h8", "丙烷")),
    "hydrogen": ("Hydrogen", ("hydrogen", "h2", "氢气", "氢")),
    "hydrogen sulfide": (
        "Hydrogen Sulfide",
        ("hydrogen sulfide", "hydrogen sulphide", "h2s", "硫化氢"),
    ),
    "1,3-butadiene": ("1,3-Butadiene", ("1,3-butadiene", "butadiene", "丁二烯")),
    "all butenes": ("All Butenes", ("all butenes", "butenes", "丁烯")),
    "all pentenes": ("All Pentenes", ("all pentenes", "pentenes", "戊烯")),
    "isobutane": ("Isobutane", ("isobutane", "i-butane", "异丁烷")),
    "n-butane": ("n-Butane", ("n-butane", "正丁烷")),
    "isopentane": ("Isopentane", ("isopentane", "i-pentane", "异戊烷")),
    "n-pentane": ("n-Pentane", ("n-pentane", "正戊烷")),
    "hexane+": ("Hexane+", ("hexane+", "hexane", "c6+", "己烷")),
}

STATUS_ALIASES: dict[str, tuple[str, ...]] = {
    "online": ("online", "在线", "运行中"),
    "offline": ("offline", "离线", "掉线"),
    "fault": ("fault", "faulty", "故障"),
    "warning": ("warning", "告警", "报警", "预警"),
}

TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "GC": ("gc", "gas chromatograph", "chromatograph", "气相色谱", "色谱"),
    "FTIR": ("ftir", "傅里叶", "红外光谱"),
    "IR": ("ir", "infrared", "红外"),
    "MS": ("ms", "mass spectrometer", "mass spec", "质谱"),
    "UV": ("uv", "紫外"),
}

PROTOCOL_ALIASES: dict[str, tuple[str, ...]] = {
    "Modbus TCP": ("modbus tcp", "modbustcp", "modbus"),
    "OPC UA": ("opc ua", "opc-ua", "opcua"),
    "MQTT": ("mqtt",),
    "REST API": ("rest api",),
    "HART": ("hart",),
}

# Words that carry no search meaning in a natural-language request.
_STOP_EN = frozenset(
    "a an the of for with that which who and or to me my find search show list give get "
    "need want device devices analyzer analyzers analyser analysers unit units can could "
    "please is are any all measure measures measuring recommend select selection best good "
    "i we you it in on at by".split()
)
_STOP_CN = tuple(
    sorted(
        (
            "请帮我", "帮我", "请问", "请", "找一下", "找出", "找到", "查一下", "查找", "搜索", "检索",
            "一下", "有哪些", "哪些", "哪个", "哪台", "有没有", "设备", "分析仪", "仪器", "一台",
            "一个", "能够测", "可以测", "能测", "可测", "测量", "推荐", "选型", "选择", "需要",
            "想要", "适合", "所有", "全部", "现在", "目前", "当前", "状态", "并且", "而且", "或者",
            "的", "和", "与", "及", "我",
        ),
        key=len,
        reverse=True,
    )
)

# --------------------------------------------------------------------------
# Normalisation & alias tables
# --------------------------------------------------------------------------


def _normalize(text: Any) -> str:
    """NFKC (full-width -> half-width), lower-case, collapse whitespace."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).lower()
    return re.sub(r"\s+", " ", s).strip()


def _is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def _build_alias_list(table: dict[str, Iterable[str]], *, include_key: bool = False) -> list[tuple[str, tuple[str, ...]]]:
    """[(alias, (canonical keys...))] sorted longest alias first so that
    "carbon dioxide" / "硫化氢" are consumed before their shorter fragments."""
    by_alias: dict[str, list[str]] = {}
    for key, aliases in table.items():
        names = list(aliases)
        if include_key:
            names.append(key)
        for a in names:
            a = _normalize(a)
            if a:
                by_alias.setdefault(a, [])
                if key not in by_alias[a]:
                    by_alias[a].append(key)
    return sorted(((a, tuple(k)) for a, k in by_alias.items()), key=lambda x: (-len(x[0]), x[0]))


_COMP_ALIASES = _build_alias_list({k: v[1] for k, v in COMPONENTS.items()}, include_key=True)
_STATUS_ALIAS_LIST = _build_alias_list(STATUS_ALIASES)
_TYPE_ALIAS_LIST = _build_alias_list(TYPE_ALIASES)
_PROTO_ALIAS_LIST = _build_alias_list(PROTOCOL_ALIASES)

# first-key lookup for exact alias -> canonical component
_ALIAS_TO_COMP: dict[str, str] = {a: keys[0] for a, keys in _COMP_ALIASES}

_pattern_cache: dict[str, re.Pattern] = {}


def _alias_pattern(alias: str) -> re.Pattern:
    """Whole-token regex for an ASCII alias. '-' and '_' count as word
    characters so 'h2' never fires inside a device code like 'GC-H2' and 'ir'
    never fires inside 'IR-1500'."""
    p = _pattern_cache.get(alias)
    if p is None:
        p = re.compile(r"(?<![a-z0-9_\-])" + re.escape(alias) + r"(?![a-z0-9_\-])")
        _pattern_cache[alias] = p
    return p


def _extract(text: str, alias_list: list[tuple[str, tuple[str, ...]]]) -> tuple[list[str], str]:
    """Find every alias in `text`; return (canonical keys in order found, text
    with the matched spans blanked out so they aren't re-matched later)."""
    found: list[str] = []
    for alias, keys in alias_list:
        if _is_ascii(alias):
            pat = _alias_pattern(alias)
            if not pat.search(text):
                continue
            text = pat.sub(" ", text)
        else:
            if alias not in text:
                continue
            text = text.replace(alias, " ")
        for k in keys:
            if k not in found:
                found.append(k)
    return found, text


def canon_component(name: Any) -> str:
    """Canonical key for a component / TAG name so 'CO2', 'co2' and
    'Carbon Dioxide' all compare equal. Unknown names fall back to their
    normalised text (so custom tags still match themselves)."""
    n = _normalize(name)
    n = re.sub(r"\s*[\(\[（].*?[\)\]）]\s*$", "", n)  # drop trailing unit e.g. "H2S (ppm)"
    return _ALIAS_TO_COMP.get(n, n)


def component_display(key: str) -> str:
    return COMPONENTS[key][0] if key in COMPONENTS else key


# --------------------------------------------------------------------------
# Query parsing
# --------------------------------------------------------------------------
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_TOKEN = re.compile(r"[a-z0-9][a-z0-9_\-\.\+/:]*")


def _clean_terms(text: str) -> list[str]:
    terms: list[str] = []
    cjk_text = text
    for sw in _STOP_CN:
        cjk_text = cjk_text.replace(sw, " ")
    for run in _CJK_RUN.findall(cjk_text):
        if len(run) >= 2 and run not in terms:
            terms.append(run)
    for tok in _ASCII_TOKEN.findall(text):
        tok = tok.strip("-_./:")
        if tok and tok not in _STOP_EN and tok not in terms:
            terms.append(tok)
    return terms


def parse_query(q: Optional[str]) -> dict:
    """Split a free-text request into hard facets and soft terms.

        "在线的能测硫化氢的色谱"  ->  status=[online], components=[hydrogen sulfide],
                                     analyzer_types=[GC], terms=[]
        "Analyzer-002"           ->  terms=["analyzer-002"]
    """
    text = _normalize(q)
    comps, text = _extract(text, _COMP_ALIASES)
    protos, text = _extract(text, _PROTO_ALIAS_LIST)
    types, text = _extract(text, _TYPE_ALIAS_LIST)
    statuses, text = _extract(text, _STATUS_ALIAS_LIST)
    return {
        "terms": _clean_terms(text),
        "components": comps,
        "statuses": statuses,
        "analyzer_types": types,
        "protocols": protos,
    }


def has_facets(parsed: dict) -> bool:
    return bool(
        parsed.get("components") or parsed.get("statuses") or parsed.get("analyzer_types") or parsed.get("protocols")
    )


def _split_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw: list[str] = [str(v) for v in value]
    else:
        raw = re.split(r"[;；|、\n]+", str(value))
    return [v.strip() for v in raw if v and v.strip()]


def merge_filters(
    parsed: dict,
    *,
    status: Any = None,
    analyzer_type: Any = None,
    component: Any = None,
    protocol: Any = None,
) -> dict:
    """Fold explicit API/filter parameters into a parsed query (union, no
    duplicates). Explicit values are canonicalised the same way as free text."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in parsed.items()}

    def add(key: str, val: str) -> None:
        if val and val not in out[key]:
            out[key].append(val)

    for v in _split_values(status):
        st, _ = _extract(_normalize(v), _STATUS_ALIAS_LIST)
        add("statuses", st[0] if st else _normalize(v))
    for v in _split_values(analyzer_type):
        ty, _ = _extract(_normalize(v), _TYPE_ALIAS_LIST)
        if ty:
            for t in ty:
                add("analyzer_types", t)
        else:
            add("analyzer_types", v.strip().upper())
    for v in _split_values(component):
        add("components", canon_component(v))
    for v in _split_values(protocol):
        pr, _ = _extract(_normalize(v), _PROTO_ALIAS_LIST)
        add("protocols", pr[0] if pr else v.strip())
    return out


def describe_parsed(parsed: dict) -> dict:
    """UI-friendly view of what the parser understood."""
    return {
        "terms": list(parsed.get("terms") or []),
        "components": [component_display(k) for k in parsed.get("components") or []],
        "statuses": list(parsed.get("statuses") or []),
        "analyzer_types": list(parsed.get("analyzer_types") or []),
        "protocols": list(parsed.get("protocols") or []),
    }


# --------------------------------------------------------------------------
# Profile helpers
# --------------------------------------------------------------------------


def _comp_names(profile: dict) -> list[str]:
    out = []
    for c in profile.get("components") or []:
        out.append(str(c.get("name")) if isinstance(c, dict) else str(c))
    return [n for n in out if n and n != "None"]


def _comp_keys(profile: dict) -> set[str]:
    return {canon_component(n) for n in _comp_names(profile)}


def _data_keys(profile: dict) -> set[str]:
    return {canon_component(n) for n in profile.get("components_with_data") or []}


def _low(v: Any) -> str:
    return _normalize(v)


def _type_matches(profile: dict, types: Iterable[str]) -> bool:
    at = _low(profile.get("analyzer_type"))
    mn = _low(profile.get("model_name"))
    for t in types:
        tl = _low(t)
        if tl and (at == tl or (mn and mn.startswith(tl))):
            return True
    return False


def _protocol_matches(profile: dict, protocols: Iterable[str]) -> bool:
    pr = _low(profile.get("protocol"))
    return any(_low(p) and _low(p) in pr for p in protocols)


def to_hit(profile: dict, score: float = 0.0, matched_in: Optional[list[str]] = None) -> dict:
    """Flatten a profile into the JSON shape returned to the UI."""
    return {
        "id": profile.get("id"),
        "device_code": profile.get("device_code"),
        "name": profile.get("name"),
        "status": profile.get("status") or "offline",
        "health_score": float(profile.get("health_score") or 0),
        "protocol": profile.get("protocol"),
        "location": profile.get("location"),
        "ip_address": profile.get("ip_address"),
        "model_name": profile.get("model_name"),
        "manufacturer": profile.get("manufacturer"),
        "analyzer_type": profile.get("analyzer_type"),
        "components": _comp_names(profile),
        "gateways": list(profile.get("gateways") or []),
        "asset_code": profile.get("asset_code"),
        "active_alarms": int(profile.get("active_alarms") or 0),
        "open_work_orders": int(profile.get("open_work_orders") or 0),
        "points_24h": int(profile.get("points_24h") or 0),
        "lab_samples": int(profile.get("lab_samples") or 0),
        "score": round(float(score), 1),
        "matched_in": list(matched_in or []),
    }


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
# (field, weight) — the best-matching field per term counts, not the sum.
_FIELD_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("name", 35),
    ("model_name", 30),
    ("manufacturer", 20),
    ("location", 20),
    ("ip_address", 15),
    ("analyzer_type", 15),
    ("components", 15),
    ("asset", 15),
    ("gateways", 15),
    ("protocol", 10),
    ("status", 5),
)


def _searchable(profile: dict) -> dict[str, str]:
    names = _comp_names(profile)
    comp_blob = " | ".join([_low(n) for n in names] + [canon_component(n) for n in names])
    return {
        "device_code": _low(profile.get("device_code")),
        "name": _low(profile.get("name")),
        "model_name": _low(profile.get("model_name")),
        "manufacturer": _low(profile.get("manufacturer")),
        "location": _low(profile.get("location")),
        "ip_address": _low(profile.get("ip_address")),
        "analyzer_type": _low(profile.get("analyzer_type")),
        "components": comp_blob,
        "asset": _low(f"{profile.get('asset_code') or ''} {profile.get('asset_name') or ''}"),
        "gateways": _low(" ".join(profile.get("gateways") or [])),
        "protocol": _low(profile.get("protocol")),
        "status": _low(profile.get("status")),
    }


def _term_in(term: str, text: str) -> bool:
    """Does `term` match inside `text`? Short ASCII terms (1-2 chars) must start a
    word — otherwise typing 'b' would match the 'b' in 'Modbus' and 'rack b' would
    return Rack A devices. Longer terms and CJK terms match anywhere."""
    if len(term) <= 2 and _is_ascii(term):
        return re.search(r"(?<![a-z0-9])" + re.escape(term), text) is not None
    return term in text


def search_score(profile: dict, parsed: dict) -> Optional[tuple[float, list[str]]]:
    """None = excluded. Facets are hard filters; terms are AND-ed substrings
    matched across the searchable fields, best field per term wins."""
    keys = _comp_keys(profile)
    for req in parsed.get("components") or []:
        if req not in keys:
            return None
    if parsed.get("statuses") and _low(profile.get("status")) not in parsed["statuses"]:
        return None
    if parsed.get("analyzer_types") and not _type_matches(profile, parsed["analyzer_types"]):
        return None
    if parsed.get("protocols") and not _protocol_matches(profile, parsed["protocols"]):
        return None

    score = 0.0
    matched: list[str] = []
    score += 10 * len(parsed.get("components") or [])
    score += 5 * len(parsed.get("statuses") or [])
    score += 8 * len(parsed.get("analyzer_types") or [])
    score += 5 * len(parsed.get("protocols") or [])
    for label, present in (
        ("component", parsed.get("components")),
        ("status", parsed.get("statuses")),
        ("type", parsed.get("analyzer_types")),
        ("protocol", parsed.get("protocols")),
    ):
        if present:
            matched.append(label)

    fields = _searchable(profile)
    for term in parsed.get("terms") or []:
        best, best_field = 0, None
        code = fields["device_code"]
        if code == term:
            best, best_field = 100, "device_code"
        elif code.startswith(term):
            best, best_field = 70, "device_code"
        elif term in code:
            best, best_field = 50, "device_code"
        for field, weight in _FIELD_WEIGHTS:
            if weight > best and _term_in(term, fields[field]):
                best, best_field = weight, field
        if best_field is None:
            return None  # every term must match somewhere
        score += best
        if best_field not in matched:
            matched.append(best_field)
    return score, matched


def search(profiles: list[dict], parsed: dict, limit: int = 20) -> tuple[int, list[dict]]:
    """(total matches, top `limit` hits). Match-all queries list by device code;
    everything else by relevance."""
    scored: list[tuple[float, dict, list[str]]] = []
    for p in profiles:
        r = search_score(p, parsed)
        if r is not None:
            scored.append((r[0], p, r[1]))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("device_code") or "")))
    hits = [to_hit(p, s, m) for s, p, m in scored[: max(0, limit)]]
    return len(scored), hits


# --------------------------------------------------------------------------
# 选型 (selection)
# --------------------------------------------------------------------------
W_COVERAGE, W_HEALTH, W_STATUS, W_DATA, W_COMMS = 35, 20, 15, 10, 5
W_TYPE, W_PROTOCOL, W_LOCATION, W_KEYWORDS = 15, 5, 5, 5
W_CERT, W_COST = 8, 6
_COST_FLAG_PENALTY = {"high_maintenance_ratio": 4.0, "corrective_heavy": 4.0, "negative_roi": 4.0}
_COST_FLAG_TEXT = {
    "high_maintenance_ratio": ("维护成本占比偏高", "high maintenance-cost ratio"),
    "corrective_heavy": ("故障性工单占比偏高", "mostly corrective work orders"),
    "negative_roi": ("按当前收益估算 ROI 为负", "negative ROI at the current benefit estimate"),
    "near_end_of_life": ("账面净值接近折旧期末", "near the end of its depreciation life"),
}
_STATUS_POINTS = {"online": 15, "warning": 8, "offline": 3, "fault": 0}
MAX_ALARM_PENALTY, MAX_WO_PENALTY = 15.0, 10.0

MODEL_BASIS = {
    "zh": "型号推荐基于本平台已部署设备的实际运行数据（覆盖的组分、健康分、在线率、报警/工单），"
    "不包含厂商规格书参数（量程、精度、价格）。",
    "en": "Model recommendations are derived from how devices of that model actually perform on this "
    "platform (components covered, health, uptime, alarms/work orders). They do not include vendor "
    "spec-sheet data such as range, accuracy or price.",
}


def build_requirement(
    query: Optional[str] = None,
    *,
    components: Any = None,
    analyzer_type: Any = None,
    protocol: Any = None,
    online_only: bool = False,
    min_health: float = 0.0,
    location: Optional[str] = None,
) -> dict:
    """Merge a natural-language request with explicit form fields."""
    parsed = merge_filters(
        parse_query(query),
        analyzer_type=analyzer_type,
        component=components,
        protocol=protocol,
    )
    return {
        "components": list(parsed["components"]),
        "analyzer_types": list(parsed["analyzer_types"]),
        "protocols": list(parsed["protocols"]),
        "online_only": bool(online_only or "online" in parsed["statuses"]),
        "min_health": max(0.0, min(100.0, float(min_health or 0))),
        "location": _low(location),
        "keywords": list(parsed["terms"]),
    }


def describe_requirement(req: dict) -> dict:
    return {
        "components": [component_display(k) for k in req.get("components") or []],
        "analyzer_types": list(req.get("analyzer_types") or []),
        "protocols": list(req.get("protocols") or []),
        "online_only": bool(req.get("online_only")),
        "min_health": float(req.get("min_health") or 0),
        "location": req.get("location") or None,
        "keywords": list(req.get("keywords") or []),
    }


def _reason(code: str, ok: bool, zh: str, en: str) -> dict:
    return {"code": code, "ok": ok, "zh": zh, "en": en}


def _join(items: Iterable[str]) -> str:
    return ", ".join(items)


def _fit(required: list[str], covered: list[str]) -> str:
    if not required:
        return "n/a"
    if len(covered) == len(required):
        return "full"
    return "partial" if covered else "none"


def _keyword_hit(profile: dict, keywords: list[str]) -> float:
    if not keywords:
        return 0.0
    f = _searchable(profile)
    blob = " ".join(f[k] for k in ("device_code", "name", "model_name", "manufacturer", "location"))
    return sum(1 for k in keywords if _term_in(k, blob)) / len(keywords)


def _cert_signal(profile: dict, covered: list[str]) -> tuple[float, float, list[dict]]:
    """(possible, earned, reasons) from AI-certification evidence (app/services/lab_analysis.py,
    persisted per device+component) for the components this device actually covers. Components
    that were never certified are simply left out — silence is not a penalty. "insufficient" is
    treated the same as "never certified": it is a data problem, not a verdict on the device."""
    certs = profile.get("certifications") or {}
    relevant = [(k, certs[k]) for k in covered if k in certs and certs[k].get("status") in ("certified", "conditional", "failed")]
    if not relevant:
        return 0.0, 0.0, []
    credit = {"certified": 1.0, "conditional": 0.5, "failed": 0.0}
    share = sum((0.3 if c.get("expired") else credit[c["status"]]) for _, c in relevant) / len(relevant)
    names = ", ".join(component_display(k) for k, _ in relevant)
    tags = sorted({c["status"] + ("(expired)" if c.get("expired") else "") for _, c in relevant})
    ok = share >= 0.99
    reason = _reason("certification", ok, f"化验认证：{names} — {'/'.join(tags)}", f"Lab certification: {names} — {'/'.join(tags)}")
    return W_CERT, W_CERT * share, [reason]


def _cost_signal(profile: dict) -> tuple[float, float, list[dict]]:
    """(possible, earned, reasons) from cost-side ROI flags (app/services/roi_analysis.py,
    computed from the device's asset + maintenance history, no benefit estimate needed). Devices
    with no linked asset simply have no flags and are not penalised. "near_end_of_life" is reported
    but never scored — a device near the end of its depreciation life can still be the right pick for
    a measurement today — so it must never move `possible`/`earned`, only add a reason."""
    flags = profile.get("roi_flags") or []
    reasons: list[dict] = []
    possible = earned = 0.0
    penalized = [f for f in flags if f in _COST_FLAG_PENALTY]
    if penalized:
        penalty = min(W_COST, sum(_COST_FLAG_PENALTY[f] for f in penalized))
        possible, earned = W_COST, W_COST - penalty
        zh = "、".join(_COST_FLAG_TEXT[f][0] for f in penalized)
        en = ", ".join(_COST_FLAG_TEXT[f][1] for f in penalized)
        reasons.append(_reason("cost_health", penalty == 0, f"维护成本健康度提示：{zh}", f"Maintenance cost flags: {en}"))
    if "near_end_of_life" in flags:
        zh, en = _COST_FLAG_TEXT["near_end_of_life"]
        reasons.append(_reason("near_end_of_life", True, zh, en))
    return possible, earned, reasons


def score_device(profile: dict, req: dict, min_points: int = 4) -> Optional[dict]:
    """Score one device against a requirement. None = excluded by a hard
    constraint (online-only / minimum health). Scores are normalised to 0-100
    over the criteria the requirement actually specified, then reduced by an
    alarm / work-order penalty."""
    status = _low(profile.get("status")) or "offline"
    health = float(profile.get("health_score") or 0)
    if req.get("online_only") and status != "online":
        return None
    if health < float(req.get("min_health") or 0):
        return None

    reasons: list[dict] = []
    earned = possible = 0.0

    # Component coverage — the core of 选型.
    required = list(req.get("components") or [])
    keys = _comp_keys(profile)
    data_keys = _data_keys(profile)
    covered = [k for k in required if k in keys]
    missing = [k for k in required if k not in keys]
    if required:
        possible += W_COVERAGE
        earned += W_COVERAGE * len(covered) / len(required)
        if not missing:
            reasons.append(_reason(
                "coverage", True,
                f"覆盖全部所需组分：{_join(component_display(k) for k in covered)}",
                f"Covers all required components: {_join(component_display(k) for k in covered)}",
            ))
        else:
            reasons.append(_reason(
                "coverage", False,
                f"缺少组分：{_join(component_display(k) for k in missing)}",
                f"Missing components: {_join(component_display(k) for k in missing)}",
            ))

    # Health
    possible += W_HEALTH
    earned += W_HEALTH * max(0.0, min(100.0, health)) / 100
    reasons.append(_reason("health", health >= 80, f"健康分 {health:.0f}", f"Health score {health:.0f}"))

    # Availability
    possible += W_STATUS
    earned += _STATUS_POINTS.get(status, 3)
    zh_status = {"online": "在线", "offline": "离线", "warning": "告警", "fault": "故障"}.get(status, status)
    reasons.append(_reason("status", status == "online", f"当前状态：{zh_status}", f"Status: {status}"))

    # Data readiness — can the AI analyse it right now?
    possible += W_DATA
    points = int(profile.get("points_24h") or 0)
    if required:
        ready = [k for k in covered if k in data_keys]
        earned += W_DATA * len(ready) / len(required)
        data_ok = len(ready) == len(required)
        reasons.append(_reason(
            "data", data_ok,
            "所需组分近24小时均有数据，可直接做AI分析" if data_ok else "部分所需组分近24小时无数据，AI分析前需先导入数据",
            "Recent data exists for every required component" if data_ok
            else "Some required components have no data in the last 24h — import data before AI analysis",
        ))
    else:
        data_ok = points >= min_points
        earned += W_DATA if data_ok else (W_DATA / 2 if points > 0 else 0)
        reasons.append(_reason(
            "data", data_ok,
            f"近24小时有 {points} 个数据点" if points else "近24小时无数据，AI分析前需先导入数据",
            f"{points} data points in the last 24h" if points else "No data in the last 24h — import data before AI analysis",
        ))

    # Comms chain (device -> TAG -> gateway)
    possible += W_COMMS
    comps = [c for c in (profile.get("components") or []) if isinstance(c, dict)]
    if comps:
        bound = sum(1 for c in comps if c.get("gateway"))
        earned += W_COMMS * bound / len(comps)
        reasons.append(_reason(
            "comms", bound == len(comps),
            "TAG 均已绑定通讯网关" if bound == len(comps) else f"{len(comps) - bound} 个 TAG 未绑定网关",
            "All TAGs bound to a gateway" if bound == len(comps) else f"{len(comps) - bound} TAG(s) not bound to a gateway",
        ))

    # Optional preferences
    types = list(req.get("analyzer_types") or [])
    type_ok: Optional[bool] = None
    if types:
        possible += W_TYPE
        ok = type_ok = _type_matches(profile, types)
        earned += W_TYPE if ok else 0
        reasons.append(_reason(
            "type", ok,
            ("分析仪类型匹配：" if ok else "类型不匹配，需要：") + _join(types),
            ("Analyzer type matches: " if ok else "Type mismatch, wanted: ") + _join(types),
        ))
    protos = list(req.get("protocols") or [])
    if protos:
        possible += W_PROTOCOL
        ok = _protocol_matches(profile, protos)
        earned += W_PROTOCOL if ok else 0
        reasons.append(_reason(
            "protocol", ok,
            ("通讯协议匹配：" if ok else "协议不匹配，需要：") + _join(protos),
            ("Protocol matches: " if ok else "Protocol mismatch, wanted: ") + _join(protos),
        ))
    loc = req.get("location") or ""
    if loc:
        possible += W_LOCATION
        ok = loc in _low(profile.get("location"))
        earned += W_LOCATION if ok else 0
        reasons.append(_reason("location", ok, ("位置匹配：" if ok else "位置不匹配：") + loc,
                               ("Location matches: " if ok else "Location mismatch: ") + loc))
    kws = list(req.get("keywords") or [])
    if kws:
        possible += W_KEYWORDS
        share = _keyword_hit(profile, kws)
        earned += W_KEYWORDS * share
        if share < 1:
            reasons.append(_reason("keywords", False, f"未匹配关键词：{_join(kws)}", f"Keywords not matched: {_join(kws)}"))

    # AI-certification and cost-health evidence, when there is any (see the module docstring).
    for sig_possible, sig_earned, sig_reasons in (_cert_signal(profile, covered), _cost_signal(profile)):
        possible += sig_possible
        earned += sig_earned
        reasons.extend(sig_reasons)

    # Operational load penalties
    alarms = int(profile.get("active_alarms") or 0)
    wos = int(profile.get("open_work_orders") or 0)
    penalty = min(MAX_ALARM_PENALTY, 5.0 * alarms) + min(MAX_WO_PENALTY, 3.0 * wos)
    if alarms:
        reasons.append(_reason("alarms", False, f"有 {alarms} 条活动报警", f"{alarms} active alarm(s)"))
    if wos:
        reasons.append(_reason("work_orders", False, f"有 {wos} 个未关闭工单", f"{wos} open work order(s)"))

    score = max(0.0, min(100.0, 100.0 * earned / possible - penalty))
    return {
        "profile": profile,
        "score": round(score, 1),
        "fit": _fit(required, covered),
        "type_ok": type_ok,
        "missing_components": [component_display(k) for k in missing],
        "reasons": reasons,
    }


def _tier(r: dict) -> tuple[int, int]:
    """Sort tier: can it measure what's required (coverage), then is it the
    analyzer type asked for. Both are about whether the device can do the job at
    all, so they outrank health / uptime — a healthy GC is still the wrong pick
    when an FTIR was requested."""
    return (_FIT_ORDER[r["fit"]], 1 if r.get("type_ok") is False else 0)


_FIT_ORDER = {"full": 0, "partial": 1, "n/a": 0, "none": 2}


def rank_devices(profiles: list[dict], req: dict, limit: int = 5, min_points: int = 4) -> tuple[int, list[dict]]:
    """(number of devices that passed the hard constraints, top `limit`)."""
    results = [r for r in (score_device(p, req, min_points) for p in profiles) if r is not None]
    # Devices that cover the required components (and are the requested type)
    # always outrank those that don't, whatever their health/uptime — a perfectly
    # healthy device that can't measure H2S is still the wrong choice for an H2S job.
    results.sort(key=lambda r: (*_tier(r), -r["score"], str(r["profile"].get("device_code") or "")))
    return len(results), results[: max(0, limit)]


def rank_models(profiles: list[dict], req: dict, limit: int = 5) -> list[dict]:
    """Rank device *models* by how their fielded units perform on this platform."""
    groups: dict[str, list[dict]] = {}
    for p in profiles:
        name = (p.get("model_name") or "").strip()
        if name:
            groups.setdefault(name, []).append(p)

    required = list(req.get("components") or [])
    types = list(req.get("analyzer_types") or [])
    protos = list(req.get("protocols") or [])
    out: list[dict] = []
    for name, units in groups.items():
        n = len(units)
        avg_health = sum(float(u.get("health_score") or 0) for u in units) / n
        online_rate = sum(1 for u in units if _low(u.get("status")) == "online") / n
        avg_alarms = sum(int(u.get("active_alarms") or 0) for u in units) / n
        avg_wos = sum(int(u.get("open_work_orders") or 0) for u in units) / n
        union: set[str] = set()
        for u in units:
            union |= _comp_keys(u)
        covered = [k for k in required if k in union]
        missing = [k for k in required if k not in union]
        first = units[0]
        proto_set = sorted({str(u.get("protocol")) for u in units if u.get("protocol")})

        earned = possible = 0.0
        reasons: list[dict] = []
        if required:
            possible += W_COVERAGE
            earned += W_COVERAGE * len(covered) / len(required)
            if not missing:
                reasons.append(_reason("coverage", True,
                                       f"该型号设备可覆盖所需组分：{_join(component_display(k) for k in covered)}",
                                       f"Units of this model cover: {_join(component_display(k) for k in covered)}"))
            else:
                reasons.append(_reason("coverage", False,
                                       f"该型号设备未覆盖：{_join(component_display(k) for k in missing)}",
                                       f"Not covered by any unit: {_join(component_display(k) for k in missing)}"))
        possible += 20
        earned += 20 * avg_health / 100
        reasons.append(_reason("health", avg_health >= 80, f"平均健康分 {avg_health:.0f}", f"Average health {avg_health:.0f}"))
        possible += 20
        earned += 20 * online_rate
        reasons.append(_reason("uptime", online_rate >= 0.8, f"在线率 {online_rate * 100:.0f}%", f"Online rate {online_rate * 100:.0f}%"))
        possible += 10
        stability = max(0.0, 1.0 - (avg_alarms * 0.5 + avg_wos * 0.3))
        earned += 10 * stability
        reasons.append(_reason(
            "stability", stability >= 0.7,
            f"平均每台 {avg_alarms:.1f} 条活动报警、{avg_wos:.1f} 个未关闭工单",
            f"Avg {avg_alarms:.1f} active alarm(s) and {avg_wos:.1f} open work order(s) per unit",
        ))
        possible += 10
        earned += 10 * min(n, 5) / 5
        reasons.append(_reason("fleet", n >= 3, f"本平台已部署 {n} 台", f"{n} unit(s) deployed on this platform"))

        # Fleet-wide AI-certification and cost-health evidence: the best certification found for
        # each required, covered component across all units, and cost flags a majority of units share
        # (one unhealthy unit shouldn't sink a model that is fine on the rest of the fleet).
        best_certs: dict[str, dict] = {}
        rank = {"certified": 2, "conditional": 1, "failed": 0}
        for u in units:
            for key, c in (u.get("certifications") or {}).items():
                if key not in covered:
                    continue
                cur = best_certs.get(key)
                cand_score = rank.get(c.get("status"), -1) - (0.5 if c.get("expired") else 0)
                cur_score = rank.get(cur["status"], -1) - (0.5 if cur.get("expired") else 0) if cur else -99
                if cand_score > cur_score:
                    best_certs[key] = c
        cert_possible, cert_earned, cert_reasons = _cert_signal({"certifications": best_certs}, covered)
        flag_counts: dict[str, int] = {}
        for u in units:
            for f in (u.get("roi_flags") or []):
                flag_counts[f] = flag_counts.get(f, 0) + 1
        fleet_flags = [f for f, c in flag_counts.items() if c > n / 2]
        cost_possible, cost_earned, cost_reasons = _cost_signal({"roi_flags": fleet_flags})
        for sig_possible, sig_earned, sig_reasons in ((cert_possible, cert_earned, cert_reasons), (cost_possible, cost_earned, cost_reasons)):
            possible += sig_possible
            earned += sig_earned
            reasons.extend(sig_reasons)

        type_ok: Optional[bool] = None
        if types:
            possible += W_TYPE
            ok = type_ok = _type_matches(first, types)
            earned += W_TYPE if ok else 0
            reasons.append(_reason("type", ok, ("类型匹配：" if ok else "类型不匹配：") + _join(types),
                                   ("Type matches: " if ok else "Type mismatch: ") + _join(types)))
        if protos:
            possible += W_PROTOCOL
            ok = any(_protocol_matches(u, protos) for u in units)
            earned += W_PROTOCOL if ok else 0
            reasons.append(_reason("protocol", ok, ("协议匹配：" if ok else "协议不匹配：") + _join(protos),
                                   ("Protocol matches: " if ok else "Protocol mismatch: ") + _join(protos)))

        penalty = 0.0
        score = max(0.0, min(100.0, 100.0 * earned / possible - penalty))
        out.append({
            "model_name": name,
            "manufacturer": first.get("manufacturer"),
            "analyzer_type": first.get("analyzer_type"),
            "units": n,
            "avg_health": round(avg_health, 1),
            "online_rate": round(online_rate, 3),
            "avg_active_alarms": round(avg_alarms, 2),
            "avg_open_work_orders": round(avg_wos, 2),
            "protocols": proto_set,
            "components": sorted(component_display(k) for k in union),
            "device_codes": sorted(str(u.get("device_code")) for u in units),
            "score": round(score, 1),
            "fit": _fit(required, covered),
            "type_ok": type_ok,
            "missing_components": [component_display(k) for k in missing],
            "reasons": reasons,
        })
    out.sort(key=lambda r: (*_tier(r), -r["score"], r["model_name"]))
    return out[: max(0, limit)]


# --------------------------------------------------------------------------
# Cross-menu links & facets
# --------------------------------------------------------------------------


def build_links(profile: dict) -> list[dict]:
    """The device's footprint in every other menu, for the AI Center 360 panel.
    `menu` values are the front-end page ids (showPage)."""
    n_tags = len(_comp_names(profile))
    gws = list(profile.get("gateways") or [])
    return [
        {"menu": "dev", "label_zh": "设备管理", "label_en": "Devices", "count": None},
        {"menu": "comm", "label_zh": "通讯（TAG/网关）", "label_en": "Comm (TAGs / gateways)",
         "count": n_tags, "detail": _join(gws) or None},
        {"menu": "danal", "label_zh": "数据（近24h点数）", "label_en": "Data (24h points)",
         "count": int(profile.get("points_24h") or 0), "detail": None},
        {"menu": "dash", "label_zh": "活动报警", "label_en": "Active alarms",
         "count": int(profile.get("active_alarms") or 0), "detail": None},
        {"menu": "maint", "label_zh": "未关闭工单", "label_en": "Open work orders",
         "count": int(profile.get("open_work_orders") or 0), "detail": None},
        {"menu": "asset", "label_zh": "资产", "label_en": "Asset",
         "count": 1 if profile.get("asset_code") else 0, "detail": profile.get("asset_code")},
        {"menu": "lab", "label_zh": "实验室样本", "label_en": "Lab samples",
         "count": int(profile.get("lab_samples") or 0), "detail": None},
        {"menu": "cert", "label_zh": "化验认证", "label_en": "Lab certification",
         "count": _cert_link_count(profile), "detail": _cert_link_detail(profile)},
        {"menu": "roi", "label_zh": "ROI / 维护成本健康度", "label_en": "ROI / cost health",
         "count": len(profile.get("roi_flags") or []), "detail": _join(profile.get("roi_flags") or []) or None},
    ]


_CERT_STATUSES = ("certified", "conditional", "failed")


def _cert_link_count(profile: dict) -> int:
    return sum(1 for c in (profile.get("certifications") or {}).values() if c.get("status") in _CERT_STATUSES)


def _cert_link_detail(profile: dict) -> Optional[str]:
    pairs = [f"{c['component_name']}:{c['status']}" for c in (profile.get("certifications") or {}).values() if c.get("status") in _CERT_STATUSES]
    return _join(sorted(pairs)[:5]) or None


def build_facets(profiles: list[dict]) -> dict:
    """Distinct values present on the platform, to drive the selection form."""
    comps: dict[str, str] = {}
    for p in profiles:
        for n in _comp_names(p):
            k = canon_component(n)
            comps.setdefault(k, component_display(k) if k in COMPONENTS else n)
    return {
        "components": sorted(comps.values(), key=str.lower),
        "analyzer_types": sorted({str(p["analyzer_type"]) for p in profiles if p.get("analyzer_type")}),
        "protocols": sorted({str(p["protocol"]) for p in profiles if p.get("protocol")}),
        "statuses": sorted({str(p.get("status") or "offline") for p in profiles}),
        "models": sorted({str(p["model_name"]) for p in profiles if p.get("model_name")}),
    }


# --------------------------------------------------------------------------
# Text renderers — used to hand results to the chat LLM as live context
# --------------------------------------------------------------------------
_SELECTION_HINTS = (
    "选型", "推荐", "哪台", "哪个设备", "哪些设备", "选一台", "选择设备", "适合", "能测", "可以测", "可测",
    "recommend", "which device", "which analyzer", "select a device", "selection", "suitable",
)
_SEARCH_HINTS = (
    "搜索", "查找", "检索", "找出", "找一下", "找设备", "列出", "有哪些", "有没有",
    "search", "find", "look for", "list all", "show me",
)


def has_selection_intent(question: str) -> bool:
    q = _normalize(question)
    return any(h in q for h in _SELECTION_HINTS)


def has_search_intent(question: str) -> bool:
    q = _normalize(question)
    return any(h in q for h in _SEARCH_HINTS)


def render_search_text(parsed: dict, total: int, hits: list[dict], limit: int = 10) -> str:
    d = describe_parsed(parsed)
    conds = []
    if d["components"]:
        conds.append("组分=" + "/".join(d["components"]))
    if d["statuses"]:
        conds.append("状态=" + "/".join(d["statuses"]))
    if d["analyzer_types"]:
        conds.append("类型=" + "/".join(d["analyzer_types"]))
    if d["protocols"]:
        conds.append("协议=" + "/".join(d["protocols"]))
    if d["terms"]:
        conds.append("关键词=" + " ".join(d["terms"]))
    head = f"设备检索结果（条件：{'；'.join(conds) or '无'}）：共 {total} 台匹配"
    lines = [
        f"- {h['device_code']} {h['name']}：{h['status']}，健康分{h['health_score']:.0f}，"
        f"型号={h.get('model_name') or '未登记'}，活动报警{h['active_alarms']}，未关闭工单{h['open_work_orders']}"
        for h in hits[:limit]
    ]
    return head + ("\n" + "\n".join(lines) if lines else "")


def render_selection_text(req: dict, considered: int, devices: list[dict], models: list[dict]) -> str:
    d = describe_requirement(req)
    conds = []
    if d["components"]:
        conds.append("需测组分=" + "/".join(d["components"]))
    if d["analyzer_types"]:
        conds.append("类型=" + "/".join(d["analyzer_types"]))
    if d["protocols"]:
        conds.append("协议=" + "/".join(d["protocols"]))
    if d["online_only"]:
        conds.append("仅在线")
    if d["keywords"]:
        conds.append("关键词=" + " ".join(d["keywords"]))
    lines = [f"设备选型结果（条件：{'；'.join(conds) or '无特别要求'}；参与评估 {considered} 台）："]
    fit_zh = {"full": "完全满足", "partial": "部分满足", "none": "不满足", "n/a": ""}
    for i, r in enumerate(devices, 1):
        p = r["profile"]
        miss = f"，缺少{'/'.join(r['missing_components'])}" if r["missing_components"] else ""
        lines.append(
            f"{i}. {p.get('device_code')} {p.get('name')}：综合得分{r['score']}，{fit_zh[r['fit']]}{miss}，"
            f"状态={p.get('status')}，健康分{float(p.get('health_score') or 0):.0f}"
        )
    if not devices:
        lines.append("（没有满足硬性条件的设备）")
    if models:
        lines.append("型号推荐（基于本平台已部署设备的运行数据）：")
        for r in models[:3]:
            lines.append(
                f"- {r['model_name']}：得分{r['score']}，已部署{r['units']}台，平均健康分{r['avg_health']:.0f}，"
                f"在线率{r['online_rate'] * 100:.0f}%"
            )
    return "\n".join(lines)
