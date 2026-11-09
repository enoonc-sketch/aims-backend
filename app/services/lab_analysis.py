"""Pure logic for Lab correlation / comparison driven by Data Analysis data, and the
criteria-based "AI certification" verdict.

Where the online value comes from: NOT typed in. For every lab sample the online value is
taken from the platform's measurement data (what the Data Analysis page shows) inside a time
window around the sampling instant.

"Certification" here is an automated conformity check of the analyzer against the lab
reference using explicit, configurable criteria, with an audit trail. It is NOT a metrological
accreditation (e.g. CNAS / ISO 17025) and the default thresholds are assumptions — set them to
the acceptance limits of your own standard / SOP.
"""
from __future__ import annotations

import bisect
import math
from datetime import datetime, timezone
from typing import Any, Optional

DEFAULT_CRITERIA = {
    "min_pairs": 5,                 # matched lab/online pairs needed to decide anything
    "min_match_rate": 0.8,          # matched samples / samples with a lab value
    "min_r2": 0.95,
    "max_mape_pct": 5.0,            # mean absolute % deviation
    "max_abs_bias_pct": 3.0,        # mean signed % deviation (systematic offset)
    "bias_not_significant": True,   # paired t-test at 95 %
    "no_significant_drift": True,   # deviation should not trend with time
}
HARD = ("pairs", "match_rate", "r2", "mape")   # failing any => "failed"; failing only soft ones => "conditional"
DISCLAIMER = {
    "zh": "本结果是基于所列判据的自动合格判定与留痕，不是计量认可（CNAS/ISO 17025）或法定检定；默认阈值为假设值，请按你们的验收标准调整。",
    "en": "This is an automated conformity check against the listed criteria, with an audit trail. It is not a metrological accreditation (CNAS / ISO 17025) or legal verification; the default thresholds are assumptions — set them to your own acceptance limits.",
}
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
        21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def t_crit_95(df: int) -> float:
    """Two-sided 95 % Student-t critical value (table up to 30 df, then standard approximations)."""
    if df < 1:
        return float("inf")
    if df <= 30:
        return _T95[df]
    return 2.021 if df <= 40 else 2.000 if df <= 60 else 1.980 if df <= 120 else 1.960


def _epoch(dt: datetime) -> float:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def pair_with_measurements(
    samples: list[dict],
    times: list[float],
    values: list[float],
    *,
    window_minutes: float = 30.0,
    aggregate: str = "mean",
) -> tuple[list[dict], list[dict]]:
    """samples: [{id, sample_no, sampled_at(datetime), lab_value}]. `times` (epoch seconds, ascending)
    and `values` are the analyzer's measurements for the same device + component.
    aggregate: 'mean' / 'median' of the points within ±window, or 'nearest' point in the window."""
    if aggregate not in ("mean", "median", "nearest"):
        raise ValueError("aggregate must be mean / median / nearest")
    w = window_minutes * 60.0
    pairs: list[dict] = []
    unmatched: list[dict] = []
    for s in samples:
        base = {"sample_id": s.get("id"), "sample_no": s.get("sample_no"),
                "sampled_at": s["sampled_at"].isoformat() if isinstance(s.get("sampled_at"), datetime) else None}
        lab = s.get("lab_value")
        if lab is None or not isinstance(s.get("sampled_at"), datetime):
            unmatched.append({**base, "reason": "no_lab_value" if lab is None else "no_time"})
            continue
        t0 = _epoch(s["sampled_at"])
        lo, hi = bisect.bisect_left(times, t0 - w), bisect.bisect_right(times, t0 + w)
        if hi <= lo:
            unmatched.append({**base, "reason": "no_data_in_window"})
            continue
        win_t, win_v = times[lo:hi], values[lo:hi]
        nearest = min(range(len(win_t)), key=lambda i: abs(win_t[i] - t0))
        if aggregate == "nearest":
            online = win_v[nearest]
        elif aggregate == "median":
            sv = sorted(win_v)
            online = sv[len(sv) // 2] if len(sv) % 2 else (sv[len(sv) // 2 - 1] + sv[len(sv) // 2]) / 2
        else:
            online = sum(win_v) / len(win_v)
        lab = float(lab)
        diff = online - lab
        pairs.append({**base, "lab": lab, "online": round(online, 6), "n_points": len(win_v),
                      "nearest_delta_min": round(abs(win_t[nearest] - t0) / 60, 2), "diff": round(diff, 6),
                      "diff_pct": round(diff / lab * 100, 4) if lab else None})
    return pairs, unmatched


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def compare_stats(pairs: list[dict]) -> dict:
    """Regression + agreement statistics of online (y) against lab (x)."""
    n = len(pairs)
    out: dict[str, Any] = {"pairs": n}
    if n < 2:
        return out
    xs = [p["lab"] for p in pairs]
    ys = [p["online"] for p in pairs]
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    diffs = [y - x for x, y in zip(xs, ys)]
    bias = _mean(diffs)
    sd = math.sqrt(sum((d - bias) ** 2 for d in diffs) / (n - 1))
    pct = [p["diff_pct"] for p in pairs if p.get("diff_pct") is not None]
    out.update(
        slope=round(slope, 6), intercept=round(intercept, 6),
        r_squared=round((sxy * sxy) / (sxx * syy), 6) if sxx and syy else 0.0,
        bias=round(bias, 6), sd_diff=round(sd, 6),
        bias_pct=round(_mean(pct), 4) if pct else None,
        mae=round(_mean([abs(d) for d in diffs]), 6),
        mape_pct=round(_mean([abs(v) for v in pct]), 4) if pct else None,
        rmse=round(math.sqrt(_mean([d * d for d in diffs])), 6),
        loa_low=round(bias - 1.96 * sd, 6), loa_high=round(bias + 1.96 * sd, 6),
        max_abs_dev=round(max(abs(d) for d in diffs), 6),
    )
    if sd > 0:
        t = bias / (sd / math.sqrt(n))
    else:
        t = 0.0 if bias == 0 else float("inf")
    tc = t_crit_95(n - 1)
    out.update(t_stat=round(t, 4) if math.isfinite(t) else None, t_crit=tc, bias_significant=abs(t) > tc)
    # does the deviation trend with time? (needs a few points and distinct times)
    ts = [_epoch(datetime.fromisoformat(p["sampled_at"])) for p in pairs if p.get("sampled_at")]
    if len(ts) == n and n >= 4 and len(set(ts)) > 1:
        mt = _mean(ts)
        stt = sum((t_ - mt) ** 2 for t_ in ts)
        sdd = sum((d - bias) ** 2 for d in diffs)
        r = sum((t_ - mt) * (d - bias) for t_, d in zip(ts, diffs)) / math.sqrt(stt * sdd) if stt and sdd else 0.0
        tr = r * math.sqrt((n - 2) / (1 - r * r)) if abs(r) < 1 else float("inf")
        out.update(drift_r=round(r, 4), drift_significant=abs(tr) > t_crit_95(n - 2))
    else:
        out.update(drift_r=None, drift_significant=False)
    return out


def _chk(code: str, ok: bool, value: Any, threshold: Any, zh: str, en: str) -> dict:
    return {"code": code, "ok": bool(ok), "hard": code in HARD, "value": value, "threshold": threshold, "zh": zh, "en": en}


def certify(stats: dict, total_samples: int, criteria: Optional[dict] = None) -> dict:
    """Evaluate `stats` (from compare_stats) against the criteria.
    status: certified | conditional | failed | insufficient"""
    c = dict(DEFAULT_CRITERIA, **{k: v for k, v in (criteria or {}).items() if v is not None})
    c["min_pairs"] = max(3, int(c["min_pairs"]))
    n = int(stats.get("pairs", 0))
    checks: list[dict] = []
    checks.append(_chk("pairs", n >= c["min_pairs"], n, c["min_pairs"],
                       f"有效配对样本 {n} 个（至少 {c['min_pairs']} 个）", f"{n} matched pair(s) (need ≥ {c['min_pairs']})"))
    if n < c["min_pairs"]:
        return {"status": "insufficient", "score": 0.0, "checks": checks, "criteria": c, "disclaimer": DISCLAIMER}
    rate = n / total_samples if total_samples else 0.0
    checks.append(_chk("match_rate", rate >= c["min_match_rate"], round(rate, 3), c["min_match_rate"],
                       f"数据匹配率 {rate * 100:.0f}%（要求 ≥ {c['min_match_rate'] * 100:.0f}%）：在线数据覆盖了多少化验样本",
                       f"Match rate {rate * 100:.0f}% (need ≥ {c['min_match_rate'] * 100:.0f}%): how many lab samples have online data"))
    r2 = stats.get("r_squared", 0.0)
    checks.append(_chk("r2", r2 >= c["min_r2"], r2, c["min_r2"], f"R² = {r2:.4f}（要求 ≥ {c['min_r2']}）", f"R² = {r2:.4f} (need ≥ {c['min_r2']})"))
    mape = stats.get("mape_pct")
    checks.append(_chk("mape", mape is not None and mape <= c["max_mape_pct"], mape, c["max_mape_pct"],
                       f"平均绝对偏差 {mape:.2f}%（要求 ≤ {c['max_mape_pct']}%）" if mape is not None else "无法计算平均绝对偏差（化验值含 0）",
                       f"Mean absolute deviation {mape:.2f}% (need ≤ {c['max_mape_pct']}%)" if mape is not None else "Mean absolute deviation unavailable (lab value is 0)"))
    bp = stats.get("bias_pct")
    checks.append(_chk("bias_pct", bp is not None and abs(bp) <= c["max_abs_bias_pct"], bp, c["max_abs_bias_pct"],
                       f"系统偏差 {bp:+.2f}%（要求 |偏差| ≤ {c['max_abs_bias_pct']}%）" if bp is not None else "无法计算系统偏差",
                       f"Systematic bias {bp:+.2f}% (need |bias| ≤ {c['max_abs_bias_pct']}%)" if bp is not None else "Systematic bias unavailable"))
    if c["bias_not_significant"]:
        sig = stats.get("bias_significant", False)
        checks.append(_chk("bias_sig", not sig, stats.get("t_stat"), stats.get("t_crit"),
                           "配对 t 检验：偏差在 95% 水平上" + ("显著不为 0" if sig else "与 0 无显著差异"),
                           "Paired t-test: bias is " + ("significantly different from 0" if sig else "not significantly different from 0") + " at 95%"))
    if c["no_significant_drift"]:
        dsig = stats.get("drift_significant", False)
        checks.append(_chk("drift", not dsig, stats.get("drift_r"), None,
                           "偏差随时间" + ("出现显著漂移趋势" if dsig else "无显著漂移"),
                           "Deviation shows " + ("a significant drift over time" if dsig else "no significant drift over time")))
    hard_fail = [k for k in checks if k["hard"] and not k["ok"]]
    soft_fail = [k for k in checks if not k["hard"] and not k["ok"]]
    status = "failed" if hard_fail else "conditional" if soft_fail else "certified"
    return {"status": status, "score": round(sum(k["ok"] for k in checks) / len(checks) * 100, 1),
            "checks": checks, "criteria": c, "disclaimer": DISCLAIMER}
