"""Unit tests for the pure AI-Studio logic: analysis_reading, roi_analysis, lab_analysis,
ai_generate. No database / FastAPI needed (`pytest --noconftest` works)."""
import math
from datetime import datetime, timedelta, timezone

import pytest

from app.services import ai_generate as gen
from app.services import analysis_reading as ar
from app.services import lab_analysis as la
from app.services import roi_analysis as roi

UTC = timezone.utc
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def codes(res):
    return [f["code"] for f in res["findings"]]


# ============================================================ analysis reading ====
class TestAnalysisReading:
    def test_stats_are_exact(self):
        r = ar.read_series({"N2": [1, 2, 3, 4, 5, 6]})
        s = r["series"][0]
        assert (s["count"], s["min"], s["max"], s["mean"]) == (6, 1, 6, 3.5)
        assert s["std"] == pytest.approx(math.sqrt(35 / 12), abs=1e-5)
        assert s["first"] == 1 and s["last"] == 6

    def test_linear_fit_matches_hand_calculation(self):
        slope, intercept, r2 = ar.linear_fit([2, 4, 6, 8])
        assert (slope, intercept) == (2.0, 2.0) and r2 == pytest.approx(1.0)
        assert ar.linear_fit([5, 5, 5])[2] == 0.0

    def test_rising_trend_detected_with_percentage(self):
        r = ar.read_series({"CH4": [100 + i for i in range(30)]})
        f = next(f for f in r["findings"] if f["code"] == "trend")
        assert r["series"][0]["trend"] == "rising" and f["severity"] == "warning"
        assert "上升" in f["zh"] and "rising" in f["en"] and "%" in f["en"]

    def test_stable_series_is_all_clear(self):
        r = ar.read_series({"CH4": [50.0, 50.1, 49.9, 50.0, 50.05, 49.95, 50.0, 50.1]})
        assert r["series"][0]["trend"] == "stable" and codes(r) == ["all_clear"]

    def test_outlier_found_and_flat_series_has_none(self):
        vals = [10.0, 10.1, 9.9, 10.0, 10.2, 9.8, 10.1, 10.0, 55.0, 9.9]
        r = ar.read_series({"X": vals})
        f = next(f for f in r["findings"] if f["code"] == "outliers")
        assert f["evidence"]["index"] == 8 and r["series"][0]["outliers"] == 1
        assert ar.outlier_indices([5.0] * 10) == []

    def test_flatline_detected(self):
        r = ar.read_series({"X": [7.0] * 20})
        assert "flatline" in codes(r)

    def test_missing_values_reported(self):
        r = ar.read_series({"X": [1, None, 3, None, 5, 6, 7, 8]})
        f = next(f for f in r["findings"] if f["code"] == "missing")
        assert f["evidence"] == {"missing": 2, "total": 8}
        assert r["series"][0]["count"] == 6

    def test_too_few_points_is_not_over_interpreted(self):
        r = ar.read_series({"X": [1, 2, 3]})
        assert "few_points" in codes(r) and "trend" not in codes(r)

    def test_empty_and_garbage_series(self):
        r = ar.read_series({"X": [], "Y": ["a", None, float("nan")]})
        assert codes(r).count("no_data") == 2

    def test_component_sum_check(self):
        good = ar.read_series({"A": [60.0] * 10, "B": [40.0] * 10}, check_sum=True)
        assert good["sum_check"]["within_range_pct"] == 100.0 and good["sum_check"]["mean"] == 100.0
        assert next(f for f in good["findings"] if f["code"] == "component_sum")["severity"] == "info"
        bad = ar.read_series({"A": [60.0] * 10, "B": [30.0] * 10}, check_sum=True)
        assert next(f for f in bad["findings"] if f["code"] == "component_sum")["severity"] == "critical"
        assert bad["sum_check"]["within_range_pct"] == 0.0

    def test_sum_check_leaves_out_components_that_have_no_data_and_says_so(self):
        # Regression: one empty TAG used to make the whole check report "no sample has every component".
        r = ar.read_series({"A": [60.0] * 10, "B": [40.0] * 10, "C": []}, check_sum=True)
        assert r["sum_check"]["excluded_no_data"] == ["C"] and r["sum_check"]["components"] == ["A", "B"] and r["sum_check"]["within_range_pct"] == 100.0
        f = next(f for f in r["findings"] if f["code"] == "component_sum")
        assert "C" in f["zh"] and "C" in f["en"] and "2 components" in f["en"]
        assert ar.read_series({"A": [60.0] * 5, "C": []}, check_sum=True)["sum_check"] is None   # < 2 usable components

    def test_sum_check_skips_samples_with_a_missing_component(self):
        r = ar.read_series({"A": [60.0, None, 60.0], "B": [40.0, 40.0, 40.0]}, check_sum=True)
        assert r["sum_check"]["samples"] == 2

    def test_sum_check_off_by_default(self):
        assert ar.read_series({"A": [60.0] * 6, "B": [40.0] * 6})["sum_check"] is None

    def test_correlation(self):
        a = [float(i) for i in range(12)]
        r = ar.read_series({"A": a, "B": [2 * x + 1 for x in a], "C": [(-1) ** i * 1.0 for i in range(12)]})
        top = r["correlations"][0]
        assert {top["a"], top["b"]} == {"A", "B"} and top["r"] == pytest.approx(1.0)
        assert "correlation" in codes(r)
        assert ar.pearson([1, 2, 3], [5, 5, 5]) is None

    def test_findings_sorted_by_severity(self):
        r = ar.read_series({"A": [60.0] * 10, "B": [30.0] * 10, "C": [7.0] * 20}, check_sum=True, sum_components=["A", "B"])
        sev = [f["severity"] for f in r["findings"]]
        assert sev == sorted(sev, key=lambda s: ar.SEVERITY_ORDER[s])

    def test_every_finding_is_bilingual(self):
        r = ar.read_series({"A": [100 + i for i in range(30)], "B": [7.0] * 20, "C": [1, None, 3, None, 5, 6, 7, 8]}, check_sum=True)
        assert r["findings"] and all(f["zh"] and f["en"] and f["severity"] in ar.SEVERITY_ORDER for f in r["findings"])

    def test_thresholds_can_be_overridden_and_are_returned(self):
        r = ar.read_series({"X": [100 + 0.1 * i for i in range(30)]}, thresholds={"trend_min_change_pct": 0.5})
        assert "trend" in codes(r) and r["thresholds"]["trend_min_change_pct"] == 0.5


# ==================================================================== ROI ====
ASSET = {"asset_code": "AST-1", "name": "GC", "purchase_value": 120000, "depreciation_years": 10, "purchase_date": datetime(2024, 9, 21, tzinfo=UTC)}


def rec(cost, days_ago):
    return {"cost": cost, "performed_at": NOW - timedelta(days=days_ago)}


def wo(kind, days_ago=10):
    return {"order_type": kind, "status": "closed", "created_at": NOW - timedelta(days=days_ago)}


class TestROI:
    def test_cost_side_numbers_are_exact(self):
        r = roi.compute_roi(ASSET, [rec(3000, 30), rec(1000, 100), rec(9999, 800)], [wo("repair"), wo("calibration")], 4, 12, now=NOW)
        m = r["metrics"]
        assert m["maintenance_cost"] == 4000.0          # the 800-day-old record is outside the window
        assert m["maintenance_records"] == 2 and m["work_orders"] == 2
        assert m["corrective_orders"] == 1 and m["preventive_orders"] == 1 and m["corrective_share_pct"] == 50.0
        assert m["monthly_depreciation"] == 1000.0 and m["depreciation_in_period"] == 12000.0
        assert m["asset_age_months"] == pytest.approx(24.0, abs=0.1)
        assert m["net_book_value"] == pytest.approx(120000 - 1000 * m["asset_age_months"], abs=1)
        assert m["annual_maintenance_cost"] == 4000.0 and m["maintenance_cost_ratio_pct"] == pytest.approx(3.33, abs=0.01)
        assert m["total_cost_period"] == 16000.0 and m["alarms"] == 4 and m["cost_per_work_order"] == 2000.0

    def test_roi_needs_a_benefit_and_says_so(self):
        r = roi.compute_roi(ASSET, [rec(4000, 30)], [], 0, 12, now=NOW)
        assert "roi_pct" not in r["metrics"] and "payback_months" not in r["metrics"]
        assert "no_benefit" in [g["code"] for g in r["data_gaps"]]

    def test_roi_and_payback_with_benefit(self):
        r = roi.compute_roi(ASSET, [rec(4000, 30)], [], 0, 12, annual_benefit=40000, now=NOW)
        m = r["metrics"]
        assert m["benefit_period"] == 40000.0
        assert m["net_benefit_period"] == 40000 - (12000 + 4000)
        assert m["roi_pct"] == pytest.approx(24000 / 16000 * 100, abs=0.1)
        assert m["payback_months"] == pytest.approx(120000 / (40000 / 12 - 4000 / 12), abs=0.1)
        assert "no_benefit" not in [g["code"] for g in r["data_gaps"]]

    def test_no_payback_when_benefit_does_not_cover_maintenance(self):
        m = roi.compute_roi(ASSET, [rec(50000, 30)], [], 0, 12, annual_benefit=10000, now=NOW)["metrics"]
        assert m["payback_months"] is None and m["roi_pct"] < 0

    def test_negative_roi_flag(self):
        r = roi.compute_roi(ASSET, [], [], 0, 12, annual_benefit=1000, now=NOW)
        assert "negative_roi" in [f["code"] for f in r["flags"]]

    def test_no_asset_reports_gap_instead_of_inventing_numbers(self):
        r = roi.compute_roi(None, [rec(500, 5)], [wo("repair")], 1, 6, annual_benefit=9000, now=NOW)
        m = r["metrics"]
        assert "purchase_value" not in m and "net_book_value" not in m and "roi_pct" not in m
        assert {"no_asset", "roi_needs_asset"} <= {g["code"] for g in r["data_gaps"]}
        assert m["maintenance_cost"] == 500.0

    def test_missing_purchase_date_uses_documented_assumption(self):
        a = dict(ASSET, purchase_date=None)
        r = roi.compute_roi(a, [], [], 0, 12, now=NOW)
        assert "no_purchase_date" in [g["code"] for g in r["data_gaps"]] and "age_assumed" in [x["code"] for x in r["assumptions"]]
        assert r["metrics"]["asset_age_months"] == 12.0

    def test_records_without_cost_are_reported(self):
        r = roi.compute_roi(ASSET, [{"cost": None, "performed_at": NOW - timedelta(days=3)}], [], 0, 12, now=NOW)
        assert "no_costs" in [g["code"] for g in r["data_gaps"]] and r["metrics"]["maintenance_cost"] == 0.0

    def test_flags(self):
        heavy = roi.compute_roi(ASSET, [rec(30000, 20)], [wo("repair"), wo("repair"), wo("replacement"), wo("calibration")], 0, 12, now=NOW)
        assert {"high_maintenance_ratio", "corrective_heavy"} <= {f["code"] for f in heavy["flags"]}
        nearly = roi.compute_roi(dict(ASSET, purchase_date=datetime(2017, 1, 1, tzinfo=UTC)), [], [], 0, 12, now=NOW)   # ~9.7 of 10 years used
        assert "near_end_of_life" in [f["code"] for f in nearly["flags"]] and 0 < nearly["metrics"]["net_book_value"] < 6000
        done = roi.compute_roi(dict(ASSET, purchase_date=datetime(2010, 1, 1, tzinfo=UTC)), [], [], 0, 12, now=NOW)     # fully depreciated
        assert done["metrics"]["net_book_value"] == 0.0 and done["metrics"]["net_book_value_pct"] == 0.0

    def test_classification_and_thresholds_override(self):
        assert [roi.classify_order(x) for x in ("Calibration", "REPAIR", "校准", "weird", None)] == ["preventive", "corrective", "preventive", "other", "other"]
        r = roi.compute_roi(ASSET, [rec(4000, 20)], [], 0, 12, now=NOW, thresholds={"maint_ratio_high_pct": 1.0})
        assert "high_maintenance_ratio" in [f["code"] for f in r["flags"]] and r["thresholds"]["maint_ratio_high_pct"] == 1.0

    def test_period_is_clamped_and_naive_datetimes_ok(self):
        r = roi.compute_roi(ASSET, [{"cost": 10, "performed_at": datetime(2026, 9, 1)}], [], 0, 0, now=NOW)
        assert r["metrics"]["period_months"] == 1 and r["metrics"]["maintenance_cost"] == 10.0

    def test_all_messages_bilingual(self):
        r = roi.compute_roi(None, [], [wo("repair")] * 3, 0, 12, now=NOW)
        for k in ("flags", "data_gaps", "assumptions", "formulas"):
            assert all(x["zh"] and x["en"] for x in r[k])


# ==================================================================== Lab ====
def samples(n=8, start=NOW - timedelta(days=8), lab=lambda i: 10.0 + i):
    return [{"id": i + 1, "sample_no": f"S{i}", "sampled_at": start + timedelta(days=i), "lab_value": lab(i)} for i in range(n)]


def series_for(smp, fn, offsets=(-10, 0, 10)):
    ts, vs = [], []
    for i, s in enumerate(smp):
        for o in offsets:
            ts.append((s["sampled_at"] + timedelta(minutes=o)).timestamp())
            vs.append(fn(i, s["lab_value"], o))
    order = sorted(range(len(ts)), key=ts.__getitem__)
    return [ts[i] for i in order], [vs[i] for i in order]


class TestLabPairing:
    def test_online_value_comes_from_measurements_in_window(self):
        smp = samples(3)
        t, v = series_for(smp, lambda i, lab, o: lab + o / 10)   # -1, 0, +1 around lab
        pairs, un = la.pair_with_measurements(smp, t, v, window_minutes=15)
        assert un == [] and len(pairs) == 3
        assert all(p["n_points"] == 3 and p["online"] == pytest.approx(p["lab"], abs=1e-6) for p in pairs)   # mean of -1,0,+1
        assert all(p["nearest_delta_min"] == 0.0 for p in pairs)

    def test_window_limits_points_used(self):
        smp = samples(1)
        t, v = series_for(smp, lambda i, lab, o: lab + o)         # -10, 0, +10
        assert la.pair_with_measurements(smp, t, v, window_minutes=5)[0][0]["n_points"] == 1
        assert la.pair_with_measurements(smp, t, v, window_minutes=15)[0][0]["n_points"] == 3

    def test_aggregates(self):
        smp = samples(1)
        t, v = series_for(smp, lambda i, lab, o: {-10: 1.0, 0: 5.0, 10: 12.0}[o])
        get = lambda a: la.pair_with_measurements(smp, t, v, window_minutes=15, aggregate=a)[0][0]["online"]  # noqa: E731
        assert get("mean") == pytest.approx(6.0) and get("median") == 5.0 and get("nearest") == 5.0
        with pytest.raises(ValueError):
            la.pair_with_measurements(smp, t, v, aggregate="mode")

    def test_unmatched_reasons(self):
        smp = samples(3) + [{"id": 9, "sample_no": "X", "sampled_at": NOW, "lab_value": None}]
        t, v = series_for(smp[:1], lambda i, lab, o: lab)         # data only around the first sample
        pairs, un = la.pair_with_measurements(smp, t, v, window_minutes=15)
        assert len(pairs) == 1
        assert sorted(u["reason"] for u in un) == ["no_data_in_window", "no_data_in_window", "no_lab_value"]

    def test_naive_sample_time_treated_as_utc(self):
        s = [{"id": 1, "sampled_at": datetime(2026, 9, 1, 8, 0), "lab_value": 5.0}]
        t = [datetime(2026, 9, 1, 8, 5, tzinfo=UTC).timestamp()]
        assert len(la.pair_with_measurements(s, t, [5.1], window_minutes=10)[0]) == 1


class TestCompareStats:
    def pairs(self, fn, n=8):
        smp = samples(n)
        t, v = series_for(smp, lambda i, lab, o: fn(i, lab), offsets=(0,))
        return la.pair_with_measurements(smp, t, v, window_minutes=5)[0]

    def test_perfect_agreement(self):
        s = la.compare_stats(self.pairs(lambda i, lab: lab))
        assert s["r_squared"] == pytest.approx(1.0) and s["slope"] == pytest.approx(1.0) and s["intercept"] == pytest.approx(0, abs=1e-9)
        assert s["bias"] == 0 and s["mape_pct"] == 0 and s["rmse"] == 0 and s["bias_significant"] is False

    def test_known_offset(self):
        s = la.compare_stats(self.pairs(lambda i, lab: lab + 0.5))
        assert s["bias"] == pytest.approx(0.5) and s["sd_diff"] == pytest.approx(0, abs=1e-9)
        assert s["rmse"] == pytest.approx(0.5) and s["mae"] == pytest.approx(0.5) and s["bias_significant"] is True
        assert s["intercept"] == pytest.approx(0.5) and s["slope"] == pytest.approx(1.0)
        assert s["mape_pct"] == pytest.approx(sum(0.5 / (10 + i) * 100 for i in range(8)) / 8, abs=1e-3)

    def test_regression_matches_hand_calculation(self):
        s = la.compare_stats(self.pairs(lambda i, lab: 2 * lab + 1))
        assert s["slope"] == pytest.approx(2.0) and s["intercept"] == pytest.approx(1.0) and s["r_squared"] == pytest.approx(1.0)

    def test_limits_of_agreement(self):
        noise = [0.3, -0.3, 0.1, -0.1, 0.2, -0.2, 0.0, 0.0]
        s = la.compare_stats(self.pairs(lambda i, lab: lab + noise[i]))
        assert s["loa_low"] == pytest.approx(s["bias"] - 1.96 * s["sd_diff"]) and s["loa_high"] == pytest.approx(s["bias"] + 1.96 * s["sd_diff"])
        assert s["max_abs_dev"] == pytest.approx(0.3)

    def test_drift_detected_when_deviation_grows_with_time(self):
        assert la.compare_stats(self.pairs(lambda i, lab: lab + 0.4 * i, n=10))["drift_significant"] is True
        assert la.compare_stats(self.pairs(lambda i, lab: lab + (0.1 if i % 2 else -0.1), n=10))["drift_significant"] is False

    def test_lab_value_zero_does_not_crash(self):
        smp = [{"id": i, "sampled_at": NOW - timedelta(days=i), "lab_value": v} for i, v in enumerate([0.0, 1.0, 2.0, 3.0, 4.0])]
        t, v = series_for(smp, lambda i, lab, o: lab + 0.1, offsets=(0,))
        s = la.compare_stats(la.pair_with_measurements(smp, t, v)[0])
        assert s["pairs"] == 5 and s["bias_pct"] is not None

    def test_too_few_pairs(self):
        assert la.compare_stats([]) == {"pairs": 0}
        assert la.compare_stats(self.pairs(lambda i, lab: lab, n=1)) == {"pairs": 1}

    def test_t_critical_values(self):
        assert la.t_crit_95(1) == 12.706 and la.t_crit_95(7) == 2.365 and la.t_crit_95(30) == 2.042
        assert la.t_crit_95(50) == 2.0 and la.t_crit_95(1000) == 1.96 and la.t_crit_95(0) == float("inf")


class TestCertification:
    def run(self, fn, n=10, total=None, criteria=None):
        smp = samples(n)
        t, v = series_for(smp, lambda i, lab, o: fn(i, lab), offsets=(0,))
        pairs, _ = la.pair_with_measurements(smp, t, v, window_minutes=5)
        return la.certify(la.compare_stats(pairs), total or n, criteria)

    def by(self, cert):
        return {c["code"]: c for c in cert["checks"]}

    def test_certified(self):
        noise = [0.02, -0.02, 0.01, -0.01, 0.02, -0.02, 0.0, 0.01, -0.01, 0.0]
        c = self.run(lambda i, lab: lab + noise[i])
        assert c["status"] == "certified" and c["score"] == 100.0 and all(k["ok"] for k in c["checks"])

    def test_failed_on_accuracy(self):
        c = self.run(lambda i, lab: lab * (1.3 if i % 2 else 0.7))
        assert c["status"] == "failed" and not self.by(c)["mape"]["ok"]

    def test_conditional_when_only_bias_fails(self):
        c = self.run(lambda i, lab: lab * 1.04, criteria={"max_mape_pct": 5, "max_abs_bias_pct": 3})
        b = self.by(c)
        assert b["r2"]["ok"] and b["mape"]["ok"] and not b["bias_pct"]["ok"] and not b["bias_sig"]["ok"]
        assert c["status"] == "conditional"

    def test_conditional_on_drift(self):
        c = self.run(lambda i, lab: lab + 0.04 * i, n=12, criteria={"max_abs_bias_pct": 50, "bias_not_significant": False})
        assert not self.by(c)["drift"]["ok"] and c["status"] == "conditional"

    def test_insufficient_pairs(self):
        c = self.run(lambda i, lab: lab, n=3)
        assert c["status"] == "insufficient" and c["score"] == 0.0 and len(c["checks"]) == 1

    def test_low_match_rate_fails(self):
        assert self.by(self.run(lambda i, lab: lab, n=10, total=20))["match_rate"]["ok"] is False
        assert self.run(lambda i, lab: lab, n=10, total=20)["status"] == "failed"

    def test_criteria_override_and_floor(self):
        c = self.run(lambda i, lab: lab * (1.3 if i % 2 else 0.7), criteria={"max_mape_pct": 50, "min_r2": 0.5})
        assert self.by(c)["mape"]["ok"]
        assert la.certify({"pairs": 2}, 2, {"min_pairs": 1})["criteria"]["min_pairs"] == 3   # can't be argued down to nothing

    def test_disclaimer_always_present_and_bilingual(self):
        for c in (self.run(lambda i, lab: lab), self.run(lambda i, lab: lab, n=3)):
            assert "CNAS" in c["disclaimer"]["zh"] and "accreditation" in c["disclaimer"]["en"]
            assert all(k["zh"] and k["en"] for k in c["checks"])


# ================================================================ generation ====
CTX = {
    "device": {"code": "GC-01", "name": "Line 1", "model": "GC-2000", "manufacturer": "AnalyzerCo", "analyzer_type": "GC", "location": "Unit 1"},
    "sop_type": "Calibration", "component": "Nitrogen", "base_sop": None,
    "open_alarms": [{"severity": "warning", "component": "Nitrogen", "message": "drift high"}],
    "open_work_orders": [{"id": 7, "title": "Check cell", "order_type": "inspection", "priority": "high"}],
    "last_diagnosis": {"component": "Nitrogen", "diagnosis": "Drift suspected"},
    "spare_parts": [{"part_code": "P-1", "name": "Filter", "stock_qty": 0}, {"part_code": "P-2", "name": "Seal", "stock_qty": 5}],
    "kb": [{"title": "Cal guide", "excerpt": "Use certified gas.", "score": 0.9}],
    "correlation": {"r_squared": 0.98, "mean_deviation": 0.02},
}


class TestGeneration:
    def test_type_normalisation(self):
        assert gen.normalize_sop_type(" calibration ") == "Calibration" and gen.normalize_sop_type("REPLACEMENT") == "Replacement"
        assert gen.normalize_sop_type("bogus") is None and gen.normalize_sop_type("") is None

    @pytest.mark.parametrize("lang", ["zh", "en"])
    @pytest.mark.parametrize("st", ["Calibration", "Replacement", "Maintenance"])
    def test_sop_template_is_complete_and_flagged_as_draft(self, lang, st):
        title, text = gen.sop_template(dict(CTX, sop_type=st), lang)
        assert title in text and "GC-01" in text and ("草稿" in text or "DRAFT" in text)
        assert "drift high" in text and "#7" in text and "Drift suspected" in text and "P-1" in text and "[1]" in text
        assert ("库存为 0" in text) if lang == "zh" else ("out of stock" in text)
        assert "GENERIC" in text or "通用步骤" in text        # no vetted SOP -> says the steps are generic

    def test_existing_sop_is_used_verbatim_not_replaced_by_generic_steps(self):
        base = {"title": "GC-2000 cal", "content": "Step A\nStep B", "version": "2.1"}
        _, text = gen.sop_template(dict(CTX, base_sop=base), "en")
        assert "Step A\nStep B" in text and "GC-2000 cal" in text and "GENERIC" not in text

    def test_sop_does_not_invent_specifications(self):
        _, text = gen.sop_template(CTX, "en")
        assert "per the manufacturer manual" in text and "does not set specific values" in text

    def test_sop_template_with_no_platform_data(self):
        c = dict(CTX, open_alarms=[], open_work_orders=[], last_diagnosis=None, spare_parts=[], kb=[], correlation=None, component=None)
        _, text = gen.sop_template(c, "en")
        assert "No active alarms" in text and "No spare parts registered" in text and "Knowledge-base references" not in text

    def test_prompts_forbid_inventing(self):
        _, text = gen.sop_template(CTX, "en")
        assert "do not invent" in gen.sop_prompt(CTX, text, "en") and "Use ONLY" in gen.roi_prompt({}, "x", "en")
        assert "Use ONLY" in gen.reading_prompt({}, "x") and "Use ONLY" in gen.cert_prompt("x")
        assert "Chinese" in gen.sop_prompt(CTX, text, "zh") and "never invent" in gen.STRICT_SYSTEM["en"]

    def test_roi_template_contains_the_computed_numbers_and_gaps(self):
        res = roi.compute_roi(ASSET, [rec(4000, 30)], [wo("repair")], 2, 12, annual_benefit=40000, now=NOW)
        en = gen.roi_template(res, "en")
        assert "120,000" in en and "4,000" in en and str(res["metrics"]["roi_pct"]) in en and "payback" in en
        gap = gen.roi_template(roi.compute_roi(None, [], [], 0, 12, now=NOW), "zh")
        facts = gap.split("ℹ")[0]
        assert "未关联资产" in gap and "无法" in gap
        assert "ROI" not in facts and "回收期" not in facts     # no ROI/payback figures when the inputs are missing

    def test_reading_template(self):
        res = ar.read_series({"A": [100 + i for i in range(30)]})
        assert "🟠" in gen.reading_template(res, "zh") and "rising" in gen.reading_template(res, "en")

    def test_cert_template(self):
        smp = samples(10)
        t, v = series_for(smp, lambda i, lab, o: lab, offsets=(0,))
        pairs, un = la.pair_with_measurements(smp, t, v, window_minutes=5)
        st = la.compare_stats(pairs)
        cert = la.certify(st, 10)
        zh, en = gen.cert_template("GC-01", "Nitrogen", st, cert, 30, len(un), "zh"), gen.cert_template("GC-01", "Nitrogen", st, cert, 30, len(un), "en")
        assert "通过（认证）" in zh and "Certified" in en and "±30" in zh and "✓" in en and "CNAS" in zh
        bad = la.certify({"pairs": 2}, 5)
        assert "数据不足" in gen.cert_template("d", "c", {"pairs": 2}, bad, 30, 3, "zh") and gen.status_text("failed", "en") == "Not certified"


class TestNumberGuard:
    FACTS = "Purchase value 120,000. Maintenance cost 4,000 over 12 months. ROI 150.0%. Health 96.5. Torque n/a. Step 1."

    def test_numbers_present_in_facts_pass_whatever_their_formatting(self):
        assert gen.ungrounded_numbers("Cost was 4000, about 120000 in total; ROI 150%, health 96.5.", self.FACTS) == []
        assert gen.ungrounded_numbers("购置价值 120,000，ROI 为 150.0%", self.FACTS) == []

    def test_invented_figures_are_caught(self):
        assert gen.ungrounded_numbers("ROI is 175%", self.FACTS) == ["175%"]
        assert gen.ungrounded_numbers("Payback in 30 months", self.FACTS) == ["30 months"]
        assert gen.ungrounded_numbers("Health improved to 97.2", self.FACTS) == ["97.2"]

    def test_small_plain_integers_are_allowed_but_not_with_a_unit(self):
        assert gen.ungrounded_numbers("1. Do this 2. Do that, see [3]", self.FACTS) == []
        assert gen.ungrounded_numbers("Accuracy within ±2%", self.FACTS) == ["2%"]        # invented tolerance
        assert gen.ungrounded_numbers("Torque to 15 psi", self.FACTS) == ["15 psi"]

    def test_unit_bearing_number_that_is_in_the_facts_is_fine(self):
        assert gen.ungrounded_numbers("Recalibrate every 12 months", self.FACTS) == []

    def test_large_invented_number(self):
        assert gen.ungrounded_numbers("Replace after 5000 hours", self.FACTS) == ["5000 hours"]

    def test_sop_values_from_the_vetted_base_sop_are_allowed(self):
        _, text = gen.sop_template(dict(CTX, base_sop={"title": "t", "content": "Span gas 50 ppm, tolerance ±2%", "version": "1"}), "en")
        assert gen.ungrounded_numbers("Use 50 ppm span gas; tolerance ±2%", text) == []
        assert gen.ungrounded_numbers("Use 80 ppm span gas", text) == ["80 ppm"]

    def test_heading_numbers_do_not_vouch_for_an_invented_small_tolerance(self):
        # Regression: "## 5. Procedure" put a bare 5 in the facts, so an invented "±5%" slipped through.
        _, text = gen.sop_template(CTX, "en")
        assert "## 5." in text and "5%" not in text
        assert gen.ungrounded_numbers("Calibrate to within ±5%", text) == ["5%"]
        assert gen.ungrounded_numbers("Torque to 3 psi", text) == ["3 psi"]
        assert gen.ungrounded_numbers("Follow steps 1-5 in section 5.", text) == []          # plain small integers stay fine

    def test_small_unit_number_grounded_by_same_unit_only(self):
        facts = "Recalibrate every 6 months; tolerance 2%."
        assert gen.ungrounded_numbers("Every 6 months, within 2%.", facts) == []
        assert gen.ungrounded_numbers("每 6 个月校准一次", facts) == []                        # unit synonyms across languages
        assert gen.ungrounded_numbers("Every 6 days", facts) == ["6 days"]                   # same number, different unit
        assert gen.ungrounded_numbers("within 6%", facts) == ["6%"]
