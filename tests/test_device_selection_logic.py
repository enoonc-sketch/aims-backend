"""Unit tests for app/services/device_selection.py — the pure search / 选型 core.

No database or FastAPI needed, so these also run with `pytest --noconftest`.
"""
import pytest

from app.services import device_selection as ds


def comp(name, gw="GW-001"):
    return {"name": name, "unit": "%", "gateway": gw}


def prof(id_, code, **kw):
    base = {
        "id": id_,
        "device_code": code,
        "name": f"Process Gas Analyzer {code[-3:]}",
        "status": "online",
        "health_score": 90.0,
        "protocol": "Modbus TCP",
        "location": "Unit 1 - Rack A",
        "ip_address": f"10.0.1.{10 + id_}",
        "model_name": "GC-2000",
        "manufacturer": "AnalyzerCo",
        "analyzer_type": "GC",
        "components": [comp("Nitrogen"), comp("Methane"), comp("Carbon Dioxide")],
        "components_with_data": ["Nitrogen", "Methane", "Carbon Dioxide"],
        "gateways": ["GW-001"],
        "asset_code": None,
        "asset_name": None,
        "active_alarms": 0,
        "open_work_orders": 0,
        "points_24h": 48,
        "lab_samples": 0,
    }
    base.update(kw)
    return base


@pytest.fixture()
def fleet():
    return [
        prof(1, "Analyzer-001", health_score=96.5),
        prof(
            2, "Analyzer-002", status="warning", health_score=82.0, protocol="OPC UA",
            model_name="FTIR-500", analyzer_type="FTIR", location="Unit 2 - Rack B",
            components=[comp("Nitrogen"), comp("Hydrogen Sulfide"), comp("Carbon Dioxide")],
            components_with_data=["Nitrogen", "Hydrogen Sulfide", "Carbon Dioxide"],
            active_alarms=1, open_work_orders=1, asset_code="AST-002", asset_name="FTIR unit",
        ),
        prof(
            3, "Analyzer-003", status="offline", health_score=60.0,
            components=[comp("H2S", gw=None), comp("CO2", gw=None)],
            components_with_data=[], points_24h=0, gateways=[],
        ),
        prof(4, "Analyzer-004", status="fault", health_score=30.0, model_name=None,
             manufacturer=None, analyzer_type=None, components=[], components_with_data=[],
             points_24h=0, gateways=[], protocol="MQTT", location=None),
    ]


# ------------------------------------------------------------- parsing ----
class TestParseQuery:
    def test_chinese_natural_language(self):
        p = ds.parse_query("在线的能测硫化氢的色谱")
        assert p["components"] == ["hydrogen sulfide"]
        assert p["statuses"] == ["online"]
        assert p["analyzer_types"] == ["GC"]
        assert p["terms"] == []

    def test_english_natural_language(self):
        p = ds.parse_query("online GC that measures H2S and CO2")
        assert set(p["components"]) == {"hydrogen sulfide", "carbon dioxide"}
        assert p["statuses"] == ["online"]
        assert p["analyzer_types"] == ["GC"]
        assert p["terms"] == []

    def test_longest_alias_wins(self):
        # 硫化氢 must not leave a stray 氢 -> hydrogen; CO2 must not also yield CO.
        assert ds.parse_query("硫化氢")["components"] == ["hydrogen sulfide"]
        assert ds.parse_query("co2")["components"] == ["carbon dioxide"]
        assert set(ds.parse_query("co and h2s")["components"]) == {"carbon monoxide", "hydrogen sulfide"}

    @pytest.mark.parametrize("code", ["GC-H2", "IR-1500", "analyzer-002", "10.0.1.12", "gc-n2"])
    def test_device_codes_are_not_parsed_as_facets(self, code):
        p = ds.parse_query(code)
        assert not ds.has_facets(p)
        assert p["terms"] == [code.lower()]

    def test_short_aliases_do_not_fire_inside_words(self):
        assert not ds.has_facets(ds.parse_query("communication"))

    def test_fullwidth_is_normalised(self):
        p = ds.parse_query("Ｈ２Ｓ 在线")
        assert p["components"] == ["hydrogen sulfide"] and p["statuses"] == ["online"]

    def test_multiword_component_with_comma(self):
        assert "1,3-butadiene" in ds.parse_query("1,3-butadiene methane")["components"]

    def test_protocol_and_leftover_term(self):
        p = ds.parse_query("modbus tcp 一号车间")
        assert p["protocols"] == ["Modbus TCP"] and p["terms"] == ["一号车间"]

    def test_stopwords_removed(self):
        assert ds.parse_query("帮我找一下离线设备") == {
            "terms": [], "components": [], "statuses": ["offline"], "analyzer_types": [], "protocols": [],
        }

    def test_empty(self):
        assert not ds.has_facets(ds.parse_query("")) and ds.parse_query(None)["terms"] == []


def test_canon_component_aliases():
    assert ds.canon_component("CO2") == ds.canon_component("Carbon Dioxide") == "carbon dioxide"
    assert ds.canon_component("H2S (ppm)") == "hydrogen sulfide"
    assert ds.canon_component("C6+") == ds.canon_component("Hexane+") == "hexane+"
    assert ds.canon_component("Propadiene") == "propadiene"  # unknown tags still match themselves


def test_merge_filters_canonicalises_and_dedupes():
    p = ds.merge_filters(ds.parse_query("h2s"), status="在线", analyzer_type="gc",
                         component="硫化氢;CO2", protocol="opc ua")
    assert p["components"] == ["hydrogen sulfide", "carbon dioxide"]
    assert p["statuses"] == ["online"]
    assert p["analyzer_types"] == ["GC"]
    assert p["protocols"] == ["OPC UA"]


# -------------------------------------------------------------- search ----
class TestSearch:
    def codes(self, hits):
        return [h["device_code"] for h in hits]

    def test_exact_code_ranks_first(self, fleet):
        total, hits = ds.search(fleet, ds.parse_query("Analyzer-002"))
        assert self.codes(hits) == ["Analyzer-002"] and total == 1
        assert "device_code" in hits[0]["matched_in"]

    def test_partial_number_finds_one_device(self, fleet):
        _, hits = ds.search(fleet, ds.parse_query("003"))
        assert self.codes(hits) == ["Analyzer-003"]

    def test_status_facet_chinese(self, fleet):
        _, hits = ds.search(fleet, ds.parse_query("离线设备"))
        assert self.codes(hits) == ["Analyzer-003"]

    def test_component_alias_matches_differently_named_tags(self, fleet):
        # 002 has "Hydrogen Sulfide", 003 has "H2S" — both are hydrogen sulfide.
        _, hits = ds.search(fleet, ds.parse_query("硫化氢"))
        assert self.codes(hits) == ["Analyzer-002", "Analyzer-003"]

    def test_facets_combine(self, fleet):
        _, hits = ds.search(fleet, ds.parse_query("在线 GC 甲烷"))
        assert self.codes(hits) == ["Analyzer-001"]

    def test_type_facet_matches_model_prefix_too(self):
        p = prof(9, "X-1", analyzer_type=None, model_name="FTIR-900")
        _, hits = ds.search([p], ds.parse_query("ftir"))
        assert len(hits) == 1

    def test_all_terms_must_match(self, fleet):
        assert ds.search(fleet, ds.parse_query("analyzer nonexistent-thing"))[0] == 0

    def test_matches_location_model_asset_gateway(self, fleet):
        assert self.codes(ds.search(fleet, ds.parse_query("rack b"))[1]) == ["Analyzer-002"]
        assert self.codes(ds.search(fleet, ds.parse_query("ftir-500"))[1]) == ["Analyzer-002"]
        assert self.codes(ds.search(fleet, ds.parse_query("ast-002"))[1]) == ["Analyzer-002"]
        assert self.codes(ds.search(fleet, ds.parse_query("gw-001"))[1]) == ["Analyzer-001", "Analyzer-002"]

    def test_single_letter_term_matches_word_starts_only(self, fleet):
        # Regression: 'b' used to match the 'b' inside "Modbus", so "rack b" returned
        # the Rack A device as well.
        assert self.codes(ds.search(fleet, ds.parse_query("rack b"))[1]) == ["Analyzer-002"]
        assert ds.search(fleet, ds.parse_query("b"))[0] == 1  # only "Rack B" starts a word with b
        assert ds.search(fleet, ds.parse_query("mo"))[0] >= 1  # 2-letter prefix of "Modbus" still works

    def test_empty_query_lists_everything_by_code(self, fleet):
        total, hits = ds.search(fleet, ds.parse_query(""))
        assert total == 4 and self.codes(hits) == [f"Analyzer-00{i}" for i in range(1, 5)]

    def test_limit_keeps_total(self, fleet):
        total, hits = ds.search(fleet, ds.parse_query(""), limit=2)
        assert total == 4 and len(hits) == 2

    def test_hit_shape(self, fleet):
        h = ds.search(fleet, ds.parse_query("Analyzer-001"))[1][0]
        assert h["components"] == ["Nitrogen", "Methane", "Carbon Dioxide"]
        assert h["model_name"] == "GC-2000" and h["health_score"] == 96.5


# ----------------------------------------------------------- selection ----
class TestSelection:
    def test_full_coverage_outranks_healthier_partial(self, fleet):
        # 001 is the healthiest/online but cannot measure H2S; 002 can.
        req = ds.build_requirement("需要测 H2S 和 CO2")
        _, ranked = ds.rank_devices(fleet, req, limit=10)
        order = [r["profile"]["device_code"] for r in ranked]
        assert order.index("Analyzer-002") < order.index("Analyzer-001")
        by = {r["profile"]["device_code"]: r for r in ranked}
        assert by["Analyzer-002"]["fit"] == "full" and by["Analyzer-002"]["missing_components"] == []
        assert by["Analyzer-001"]["fit"] == "partial" and by["Analyzer-001"]["missing_components"] == ["Hydrogen Sulfide"]
        assert by["Analyzer-004"]["fit"] == "none"

    def test_online_only_is_a_hard_filter(self, fleet):
        req = ds.build_requirement("H2S", online_only=True)
        considered, ranked = ds.rank_devices(fleet, req, limit=10)
        assert considered == 1 and [r["profile"]["device_code"] for r in ranked] == ["Analyzer-001"]

    def test_online_word_in_query_implies_online_only(self):
        assert ds.build_requirement("在线色谱")["online_only"] is True

    def test_min_health_filters(self, fleet):
        req = ds.build_requirement(min_health=85)
        _, ranked = ds.rank_devices(fleet, req, limit=10)
        assert [r["profile"]["device_code"] for r in ranked] == ["Analyzer-001"]

    def test_alarms_and_work_orders_lower_the_score(self):
        clean = prof(1, "A-1")
        loaded = prof(2, "A-2", active_alarms=2, open_work_orders=2)
        req = ds.build_requirement()
        a, b = ds.score_device(clean, req), ds.score_device(loaded, req)
        assert b["score"] < a["score"]
        assert {r["code"] for r in b["reasons"] if not r["ok"]} >= {"alarms", "work_orders"}

    def test_penalty_is_capped(self):
        req = ds.build_requirement()
        worst = ds.score_device(prof(1, "A-1", active_alarms=99, open_work_orders=99), req)
        base = ds.score_device(prof(2, "A-2"), req)
        assert base["score"] - worst["score"] == pytest.approx(25.0, abs=0.11)

    def test_scores_stay_within_0_100(self, fleet):
        for req in (ds.build_requirement(), ds.build_requirement("在线 GC H2S 一号车间", location="unit")):
            for p in fleet:
                r = ds.score_device(p, req)
                if r:
                    assert 0 <= r["score"] <= 100

    def test_perfect_match_scores_high(self):
        p = prof(1, "A-1", health_score=100)
        r = ds.score_device(p, ds.build_requirement("在线 GC 甲烷 modbus"))
        assert r["score"] >= 95 and r["fit"] == "full"

    def test_data_readiness_reason(self, fleet):
        req = ds.build_requirement("H2S")
        by = {r["profile"]["device_code"]: r for r in ds.rank_devices(fleet, req, limit=10)[1]}
        data = lambda code: next(x for x in by[code]["reasons"] if x["code"] == "data")  # noqa: E731
        assert data("Analyzer-002")["ok"] is True
        assert data("Analyzer-003")["ok"] is False  # has the tag, but no recent data

    def test_comms_reason(self, fleet):
        r = ds.score_device(fleet[2], ds.build_requirement())
        comms = next(x for x in r["reasons"] if x["code"] == "comms")
        assert comms["ok"] is False and "2" in comms["zh"]

    def test_type_and_protocol_are_soft_preferences(self, fleet):
        req = ds.build_requirement(analyzer_type="FTIR", protocol="OPC UA")
        _, ranked = ds.rank_devices(fleet, req, limit=10)
        assert ranked[0]["profile"]["device_code"] == "Analyzer-002"
        codes = {x["code"]: x["ok"] for x in ranked[0]["reasons"]}
        assert codes["type"] and codes["protocol"]
        # a mismatching device is still listed, just flagged
        other = next(r for r in ranked if r["profile"]["device_code"] == "Analyzer-001")
        assert {x["code"]: x["ok"] for x in other["reasons"]}["type"] is False

    def test_requested_type_outranks_a_healthier_wrong_type(self, fleet):
        # Regression: a healthy online GC used to outrank the FTIR that was asked for,
        # because a type mismatch only cost a few points.
        req = ds.build_requirement(analyzer_type="FTIR")
        _, ranked = ds.rank_devices(fleet, req, limit=10)
        assert ranked[0]["profile"]["device_code"] == "Analyzer-002"
        assert ranked[0]["profile"]["health_score"] < next(
            r for r in ranked if r["profile"]["device_code"] == "Analyzer-001"
        )["profile"]["health_score"]
        assert ranked[0]["type_ok"] is True
        assert next(r for r in ranked if r["profile"]["device_code"] == "Analyzer-001")["type_ok"] is False

    def test_component_coverage_still_outranks_type(self, fleet):
        # Can it measure the thing? beats is-it-the-right-kind-of-analyzer.
        req = ds.build_requirement("H2S", analyzer_type="GC")
        _, ranked = ds.rank_devices(fleet, req, limit=10)
        by = {r["profile"]["device_code"]: r for r in ranked}
        assert by["Analyzer-002"]["fit"] == "full" and by["Analyzer-002"]["type_ok"] is False
        assert by["Analyzer-001"]["fit"] == "none"  # a GC, but it has no H2S tag at all
        order = [r["profile"]["device_code"] for r in ranked]
        assert order.index("Analyzer-002") < order.index("Analyzer-001")

    def test_model_ranking_also_prefers_requested_type(self, fleet):
        models = ds.rank_models(fleet, ds.build_requirement(analyzer_type="FTIR"))
        assert models[0]["model_name"] == "FTIR-500"

    def test_every_reason_is_bilingual(self, fleet):
        for r in ds.rank_devices(fleet, ds.build_requirement("在线 GC H2S 一号车间", location="unit"), limit=10)[1]:
            for reason in r["reasons"]:
                assert reason["zh"] and reason["en"] and isinstance(reason["ok"], bool)

    def test_requirement_merges_query_and_explicit_fields(self):
        req = ds.build_requirement("测 H2S", components=["CO2"], analyzer_type="gc", protocol="opc ua")
        assert set(req["components"]) == {"hydrogen sulfide", "carbon dioxide"}
        assert req["analyzer_types"] == ["GC"] and req["protocols"] == ["OPC UA"]


class TestModelRanking:
    def test_groups_by_model_and_skips_unregistered(self, fleet):
        models = ds.rank_models(fleet, ds.build_requirement())
        assert {m["model_name"] for m in models} == {"GC-2000", "FTIR-500"}  # 004 has no model

    def test_fleet_evidence(self, fleet):
        gc = next(m for m in ds.rank_models(fleet, ds.build_requirement()) if m["model_name"] == "GC-2000")
        assert gc["units"] == 2  # 001 + 003
        assert gc["avg_health"] == pytest.approx((96.5 + 60.0) / 2, abs=0.1)
        assert gc["online_rate"] == 0.5
        assert gc["device_codes"] == ["Analyzer-001", "Analyzer-003"]

    def test_coverage_uses_union_of_fleet_components(self, fleet):
        req = ds.build_requirement("H2S CO2")
        gc = next(m for m in ds.rank_models(fleet, req) if m["model_name"] == "GC-2000")
        assert gc["fit"] == "full"  # 003's H2S/CO2 tags count towards the model

    def test_ranking_prefers_full_coverage(self, fleet):
        ranked = ds.rank_models(fleet, ds.build_requirement("H2S"))
        assert all(r["fit"] == "full" for r in ranked)  # both models have an H2S unit
        only_gc = ds.rank_models([fleet[0]], ds.build_requirement("H2S"))
        assert only_gc[0]["fit"] == "none" and only_gc[0]["missing_components"] == ["Hydrogen Sulfide"]

    def test_basis_disclaims_vendor_specs(self):
        assert "规格书" in ds.MODEL_BASIS["zh"] and "spec" in ds.MODEL_BASIS["en"]


# ------------------------------------------------- links / facets / text ----
def test_links_cover_every_menu(fleet):
    links = {l["menu"]: l for l in ds.build_links(fleet[1])}
    assert set(links) == {"dev", "comm", "danal", "dash", "maint", "asset", "lab", "cert", "roi"}
    assert links["maint"]["count"] == 1 and links["dash"]["count"] == 1
    assert links["asset"]["count"] == 1 and links["asset"]["detail"] == "AST-002"
    assert links["comm"]["count"] == 3 and links["comm"]["detail"] == "GW-001"
    assert links["danal"]["count"] == 48
    assert links["cert"]["count"] == 0 and links["cert"]["detail"] is None   # no certifications in the base fixture
    assert links["roi"]["count"] == 0 and links["roi"]["detail"] is None    # no roi_flags in the base fixture


def test_links_show_certification_and_cost_health():
    p = prof(9, "X-1", certifications={
        "hydrogen sulfide": {"component_name": "H2S", "status": "certified", "score": 100, "expired": False},
        "nitrogen": {"component_name": "N2", "status": "insufficient", "score": 0, "expired": False},   # not counted
    }, roi_flags=["high_maintenance_ratio", "near_end_of_life"])
    links = {l["menu"]: l for l in ds.build_links(p)}
    assert links["cert"]["count"] == 1 and links["cert"]["detail"] == "H2S:certified"   # insufficient excluded
    assert links["roi"]["count"] == 2 and links["roi"]["detail"] == "high_maintenance_ratio, near_end_of_life"


def test_facets(fleet):
    f = ds.build_facets(fleet)
    assert "Hydrogen Sulfide" in f["components"] and "H2S" not in f["components"]  # aliases merged
    assert f["analyzer_types"] == ["FTIR", "GC"]
    assert f["models"] == ["FTIR-500", "GC-2000"]
    assert set(f["statuses"]) == {"online", "warning", "offline", "fault"}


def test_intent_detection():
    assert ds.has_selection_intent("推荐一台能测H2S的设备")
    assert ds.has_selection_intent("Which device should I use for CO2?")
    assert not ds.has_selection_intent("今天天气怎么样")
    assert ds.has_search_intent("搜索离线设备") and ds.has_search_intent("find offline analyzers")


def test_render_texts(fleet):
    parsed = ds.parse_query("在线 GC")
    total, hits = ds.search(fleet, parsed)
    text = ds.render_search_text(parsed, total, hits)
    assert "状态=online" in text and "类型=GC" in text and "Analyzer-001" in text

    req = ds.build_requirement("H2S CO2")
    considered, ranked = ds.rank_devices(fleet, req, limit=3)
    text = ds.render_selection_text(req, considered, ranked, ds.rank_models(fleet, req))
    assert "设备选型结果" in text and "完全满足" in text and "型号推荐" in text
    assert ds.render_selection_text(req, 0, [], []).endswith("（没有满足硬性条件的设备）")


# --------------------------------------------- certification & cost-health signals ----
def cert(status, expired=False, score=90.0):
    return {"status": status, "score": score, "expired": expired}


class TestCertificationSignal:
    def test_never_certified_is_not_penalised(self, fleet):
        req = ds.build_requirement("H2S")   # 002 and 003 both cover it, neither has been certified
        _, ranked = ds.rank_devices(fleet, req, limit=10)
        by = {r["profile"]["device_code"]: r for r in ranked}
        assert all(x["code"] != "certification" for x in by["Analyzer-002"]["reasons"])
        assert by["Analyzer-002"]["score"] == pytest.approx(ds.score_device(fleet[1], req)["score"])  # unaffected

    def test_certified_outranks_uncertified_at_equal_coverage(self, fleet):
        certified = dict(fleet[1], certifications={"hydrogen sulfide": cert("certified")})
        req = ds.build_requirement("H2S")
        a = ds.score_device(certified, req)
        b = ds.score_device(fleet[1], req)  # same device, no certification record
        assert a["score"] > b["score"]
        r = next(x for x in a["reasons"] if x["code"] == "certification")
        assert r["ok"] is True and "certified" in r["en"] and "Hydrogen Sulfide" in r["en"]

    def test_failed_certification_is_a_penalty(self, fleet):
        failed = dict(fleet[1], certifications={"hydrogen sulfide": cert("failed")})
        req = ds.build_requirement("H2S")
        a = ds.score_device(failed, req)
        b = ds.score_device(fleet[1], req)
        assert a["score"] < b["score"]
        assert next(x for x in a["reasons"] if x["code"] == "certification")["ok"] is False

    def test_expired_certification_counts_less_than_current(self, fleet):
        fresh = dict(fleet[1], certifications={"hydrogen sulfide": cert("certified", expired=False)})
        stale = dict(fleet[1], certifications={"hydrogen sulfide": cert("certified", expired=True)})
        req = ds.build_requirement("H2S")
        a, b = ds.score_device(fresh, req), ds.score_device(stale, req)
        assert a["score"] > b["score"]
        assert "expired" in next(x for x in b["reasons"] if x["code"] == "certification")["en"]

    def test_conditional_sits_between_certified_and_failed(self, fleet):
        req = ds.build_requirement("H2S")
        mk = lambda st: ds.score_device(dict(fleet[1], certifications={"hydrogen sulfide": cert(st)}), req)["score"]  # noqa: E731
        assert mk("failed") < mk("conditional") < mk("certified")

    def test_insufficient_status_is_ignored_like_no_record(self, fleet):
        req = ds.build_requirement("H2S")
        a = ds.score_device(dict(fleet[1], certifications={"hydrogen sulfide": cert("insufficient")}), req)
        b = ds.score_device(fleet[1], req)
        assert a["score"] == b["score"] and all(x["code"] != "certification" for x in a["reasons"])

    def test_only_covered_and_required_components_are_considered(self, fleet):
        # a certification on a component the requirement never asked about must not affect scoring
        req = ds.build_requirement("H2S")
        with_extra = dict(fleet[1], certifications={"hydrogen sulfide": cert("certified"), "methane": cert("failed")})
        without_extra = dict(fleet[1], certifications={"hydrogen sulfide": cert("certified")})
        assert ds.score_device(with_extra, req)["score"] == ds.score_device(without_extra, req)["score"]

    def test_multiple_required_components_average(self, fleet):
        p = dict(fleet[1], certifications={"hydrogen sulfide": cert("certified"), "carbon dioxide": cert("failed")})
        req = ds.build_requirement("H2S CO2")
        r = next(x for x in ds.score_device(p, req)["reasons"] if x["code"] == "certification")
        assert r["ok"] is False  # average of full + zero credit is well below the "ok" threshold

    def test_no_signal_without_a_component_requirement(self, fleet):
        p = dict(fleet[1], certifications={"hydrogen sulfide": cert("certified")})
        assert all(x["code"] != "certification" for x in ds.score_device(p, ds.build_requirement())["reasons"])


class TestCostHealthSignal:
    def test_no_asset_no_flags_no_penalty(self, fleet):
        r = ds.score_device(fleet[0], ds.build_requirement())
        assert all(x["code"] not in ("cost_health", "near_end_of_life") for x in r["reasons"])

    def test_flagged_devices_score_lower(self, fleet):
        clean = ds.score_device(fleet[0], ds.build_requirement())
        flagged = ds.score_device(dict(fleet[0], roi_flags=["high_maintenance_ratio", "corrective_heavy"]), ds.build_requirement())
        assert flagged["score"] < clean["score"]
        r = next(x for x in flagged["reasons"] if x["code"] == "cost_health")
        assert r["ok"] is False and "high maintenance-cost ratio" in r["en"] and "mostly corrective" in r["en"]

    def test_penalty_is_capped(self, fleet):
        many = ds.score_device(dict(fleet[0], roi_flags=["high_maintenance_ratio", "corrective_heavy", "negative_roi"]), ds.build_requirement())
        two = ds.score_device(dict(fleet[0], roi_flags=["high_maintenance_ratio", "corrective_heavy"]), ds.build_requirement())
        assert many["score"] == two["score"]  # third flag pushes past the W_COST cap, no further drop

    def test_near_end_of_life_is_informational_only(self, fleet):
        base = ds.score_device(fleet[0], ds.build_requirement())
        eol = ds.score_device(dict(fleet[0], roi_flags=["near_end_of_life"]), ds.build_requirement())
        assert eol["score"] == base["score"]
        r = next(x for x in eol["reasons"] if x["code"] == "near_end_of_life")
        assert r["ok"] is True and "depreciation" in r["en"]

    def test_unknown_flag_codes_are_ignored_gracefully(self, fleet):
        r = ds.score_device(dict(fleet[0], roi_flags=["some_future_flag_code"]), ds.build_requirement())
        assert all(x["code"] not in ("cost_health",) for x in r["reasons"])


class TestModelRankingSignals:
    def test_certification_on_any_unit_credits_the_model(self, fleet):
        u1 = dict(fleet[1], certifications={"hydrogen sulfide": cert("certified")})
        u2 = dict(fleet[2])  # Analyzer-003, uncertified
        models = ds.rank_models([u1, u2], ds.build_requirement("H2S"))
        ft = next(m for m in models if m["model_name"] == "FTIR-500")
        assert any(x["code"] == "certification" and x["ok"] for x in ft["reasons"])

    def test_best_evidence_wins_over_stale_or_failed_on_other_units(self, fleet):
        u1 = dict(fleet[1], certifications={"hydrogen sulfide": cert("failed")}, device_code="U1")
        u2 = dict(fleet[1], certifications={"hydrogen sulfide": cert("certified")}, device_code="U2", model_name="FTIR-500")
        models = ds.rank_models([u1, u2], ds.build_requirement("H2S"))
        r = next(x for x in models[0]["reasons"] if x["code"] == "certification")
        assert r["ok"] is True  # the certified unit's evidence is used, not the failed one's

    def test_cost_flag_needs_a_majority_of_units(self, fleet):
        units = [dict(fleet[0], device_code=f"U{i}", roi_flags=(["high_maintenance_ratio"] if i < 2 else [])) for i in range(5)]
        models = ds.rank_models(units, ds.build_requirement())
        assert all(x["code"] != "cost_health" for x in models[0]["reasons"])  # 2 of 5 is not a majority
        units2 = [dict(fleet[0], device_code=f"U{i}", roi_flags=(["high_maintenance_ratio"] if i < 3 else [])) for i in range(5)]
        models2 = ds.rank_models(units2, ds.build_requirement())
        assert any(x["code"] == "cost_health" and not x["ok"] for x in models2[0]["reasons"])  # 3 of 5 is
