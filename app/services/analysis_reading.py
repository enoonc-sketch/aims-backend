"""Pure logic behind "AI reads the Data Analysis data".

Every number is computed here, deterministically, from the series handed in. An LLM
(if one is configured) only *phrases* the result afterwards — it never produces figures.
Each finding carries bilingual text (zh/en) plus the evidence it was derived from.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

THRESHOLDS = {
    "min_points": 5,
    "missing_warn_pct": 10.0,        # share of missing readings
    "flatline_ratio": 0.9,           # share of consecutive identical readings => stuck sensor
    "trend_min_r2": 0.5,             # linear-fit R² needed to call a trend
    "trend_min_change_pct": 2.0,     # fitted change over the window, % of mean
    "noise_warn_pct": 5.0,           # residual std around the fit, % of mean
    "outlier_z": 3.5,                # modified z-score (median / MAD)
    "sum_low": 99.0,                 # component-sum check window (%), as on the Data Analysis page
    "sum_high": 101.0,
    "strong_corr": 0.9,
}
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _floats(values: Iterable[Any]) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    for v in values:
        try:
            f = float(v)
            out.append(None if math.isnan(f) or math.isinf(f) else f)
        except (TypeError, ValueError):
            out.append(None)
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _std(xs: list[float]) -> float:
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def linear_fit(ys: list[float]) -> tuple[float, float, float]:
    """(slope per step, intercept, R²) of ys against 0..n-1."""
    n = len(ys)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0
    xs = list(range(n))
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx if sxx else 0.0
    r2 = (sxy * sxy) / (sxx * syy) if sxx and syy else 0.0
    return slope, my - slope * mx, r2


def pearson(a: list[float], b: list[float]) -> Optional[float]:
    if len(a) != len(b) or len(a) < 3:
        return None
    ma, mb = _mean(a), _mean(b)
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    if not saa or not sbb:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(saa * sbb)


def outlier_indices(xs: list[float], z: float = THRESHOLDS["outlier_z"]) -> list[int]:
    """Modified z-score (robust). A flat series (MAD == 0) has no outliers by this measure."""
    if len(xs) < 5:
        return []
    s = sorted(xs)
    med = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
    dev = sorted(abs(x - med) for x in xs)
    mad = dev[len(dev) // 2] if len(dev) % 2 else (dev[len(dev) // 2 - 1] + dev[len(dev) // 2]) / 2
    if mad == 0:
        return []
    return [i for i, x in enumerate(xs) if abs(0.6745 * (x - med) / mad) > z]


def flatline_ratio(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    return sum(1 for a, b in zip(xs, xs[1:]) if a == b) / (len(xs) - 1)


def _f(x: float, nd: int = 3) -> str:
    return f"{x:.{nd}f}".rstrip("0").rstrip(".") if abs(x) < 1e6 else f"{x:.3g}"


def _finding(sev: str, code: str, series: Optional[str], zh: str, en: str, **evidence: Any) -> dict:
    return {"severity": sev, "code": code, "series": series, "zh": zh, "en": en, "evidence": evidence}


def read_series(
    series: dict[str, list],
    *,
    check_sum: bool = False,
    sum_components: Optional[list[str]] = None,
    max_corr_pairs: int = 5,
    thresholds: Optional[dict] = None,
) -> dict:
    """Analyse named, time-ordered series. Returns per-series stats/trend, ranked findings,
    top correlations and an optional component-sum check."""
    th = dict(THRESHOLDS, **(thresholds or {}))
    raw = {name: _floats(vals) for name, vals in series.items()}
    stats_out: list[dict] = []
    findings: list[dict] = []
    clean: dict[str, list[float]] = {}

    for name, vals in raw.items():
        good = [v for v in vals if v is not None]
        total, n = len(vals), len(good)
        missing = total - n
        st: dict[str, Any] = {"name": name, "count": n, "missing": missing}
        if n == 0:
            findings.append(_finding("warning", "no_data", name, f"{name}：没有有效数据", f"{name}: no valid data"))
            stats_out.append(st)
            continue
        clean[name] = good
        mean, std = _mean(good), _std(good)
        st.update(min=min(good), max=max(good), mean=round(mean, 6), std=round(std, 6),
                  cv_pct=round(std / abs(mean) * 100, 3) if mean else None, first=good[0], last=good[-1])
        if n < th["min_points"]:
            findings.append(_finding("info", "few_points", name, f"{name}：仅 {n} 个有效点，趋势/异常判断不可靠",
                                     f"{name}: only {n} valid points — trend/outlier checks are unreliable", n=n))
            stats_out.append(st)
            continue

        slope, intercept, r2 = linear_fit(good)
        change = slope * (n - 1)
        change_pct = change / abs(mean) * 100 if mean else None
        resid = [y - (intercept + slope * i) for i, y in enumerate(good)]
        noise_pct = _std(resid) / abs(mean) * 100 if mean else None
        st.update(slope_per_step=round(slope, 8), r2=round(r2, 4), fitted_change=round(change, 6),
                  fitted_change_pct=round(change_pct, 3) if change_pct is not None else None,
                  noise_pct=round(noise_pct, 3) if noise_pct is not None else None)
        big = (abs(change_pct) >= th["trend_min_change_pct"]) if change_pct is not None else (std > 0 and abs(change) >= 2 * std)
        if r2 >= th["trend_min_r2"] and big:
            up = change > 0
            pct = f"{change_pct:+.2f}%" if change_pct is not None else _f(change)
            st["trend"] = "rising" if up else "falling"
            findings.append(_finding(
                "warning", "trend", name,
                f"{name}：窗口内呈{'上升' if up else '下降'}趋势，拟合变化约 {pct}（R²={r2:.2f}）",
                f"{name}: {'rising' if up else 'falling'} over the window, fitted change ≈ {pct} (R²={r2:.2f})",
                r2=round(r2, 4), change_pct=change_pct))
        else:
            st["trend"] = "stable"

        if noise_pct is not None and noise_pct >= th["noise_warn_pct"] and st["trend"] == "stable":
            findings.append(_finding("warning", "noise", name,
                                     f"{name}：围绕均值波动较大（残差标准差约占均值 {noise_pct:.1f}%）",
                                     f"{name}: noisy — residual std ≈ {noise_pct:.1f}% of the mean", noise_pct=noise_pct))
        fr = flatline_ratio(good)
        if fr >= th["flatline_ratio"] and n >= 10:
            findings.append(_finding("warning", "flatline", name,
                                     f"{name}：{fr * 100:.0f}% 的相邻读数完全相同，疑似传感器卡死/数据未更新",
                                     f"{name}: {fr * 100:.0f}% of consecutive readings are identical — possible stuck sensor or stale data",
                                     ratio=round(fr, 3)))
        out = outlier_indices(good, th["outlier_z"])
        if out:
            st["outliers"] = len(out)
            sev = "warning" if (len(out) >= 3 or len(out) / n >= 0.01) else "info"
            worst = max(out, key=lambda i: abs(good[i] - mean))
            findings.append(_finding(sev, "outliers", name,
                                     f"{name}：发现 {len(out)} 个离群点（最极端值 {_f(good[worst])}，位于第 {worst + 1} 个有效点）",
                                     f"{name}: {len(out)} outlier(s) (most extreme {_f(good[worst])} at valid point #{worst + 1})",
                                     count=len(out), index=worst))
        if total and missing / total * 100 >= th["missing_warn_pct"]:
            findings.append(_finding("warning", "missing", name,
                                     f"{name}：缺失 {missing}/{total} 个读数（{missing / total * 100:.0f}%）",
                                     f"{name}: {missing}/{total} readings missing ({missing / total * 100:.0f}%)",
                                     missing=missing, total=total))
        stats_out.append(st)

    # component-sum check (e.g. gas composition should add up to ~100 %)
    sum_check = None
    wanted = [c for c in (sum_components or list(raw.keys())) if c in raw]
    # a component with no readings at all can't take part in the sum; say so instead of failing every sample
    names = [c for c in wanted if any(v is not None for v in raw[c])]
    excluded = [c for c in wanted if c not in names]
    if check_sum and len(names) >= 2:
        length = min(len(raw[c]) for c in names)
        sums = []
        for i in range(length):
            row = [raw[c][i] for c in names]
            if all(v is not None for v in row):
                sums.append(sum(row))  # type: ignore[arg-type]
        if sums:
            inside = sum(1 for s in sums if th["sum_low"] <= s <= th["sum_high"])
            share = inside / len(sums)
            sum_check = {"components": names, "excluded_no_data": excluded, "samples": len(sums), "within_range_pct": round(share * 100, 2),
                         "min": round(min(sums), 4), "max": round(max(sums), 4), "mean": round(_mean(sums), 4),
                         "low": th["sum_low"], "high": th["sum_high"]}
            sev = "info" if share >= 0.99 else ("warning" if share >= 0.9 else "critical")
            findings.append(_finding(
                sev, "component_sum", None,
                f"成分总和检查（{len(names)} 个组分）：{share * 100:.1f}% 的采样点落在 {th['sum_low']:g}–{th['sum_high']:g}% 内（范围 {min(sums):.2f}–{max(sums):.2f}，均值 {_mean(sums):.2f}）"
                + (f"；无数据未计入：{', '.join(excluded)}" if excluded else ""),
                f"Component-sum check ({len(names)} components): {share * 100:.1f}% of samples within {th['sum_low']:g}–{th['sum_high']:g}% (range {min(sums):.2f}–{max(sums):.2f}, mean {_mean(sums):.2f})"
                + (f"; left out (no data): {', '.join(excluded)}" if excluded else ""),
                **sum_check))
        else:
            findings.append(_finding("info", "component_sum", None, "成分总和检查：没有各组分同时有值的采样点",
                                     "Component-sum check: no sample has every component present"))

    # strongest cross-correlations (equal-length, aligned series only)
    corr: list[dict] = []
    ks = [k for k in clean if len(raw[k]) == len(next(iter(raw.values())))]
    for i, a in enumerate(ks):
        for b in ks[i + 1:]:
            pa, pb = raw[a], raw[b]
            idx = [j for j in range(len(pa)) if pa[j] is not None and pb[j] is not None]
            r = pearson([pa[j] for j in idx], [pb[j] for j in idx])
            if r is not None:
                corr.append({"a": a, "b": b, "r": round(r, 4), "n": len(idx)})
    corr.sort(key=lambda c: -abs(c["r"]))
    corr = corr[:max_corr_pairs]
    for c in corr:
        if abs(c["r"]) >= th["strong_corr"]:
            findings.append(_finding("info", "correlation", None,
                                     f"{c['a']} 与 {c['b']} 高度{'正' if c['r'] > 0 else '负'}相关（r={c['r']:.2f}, n={c['n']}）",
                                     f"{c['a']} and {c['b']} are strongly {'positively' if c['r'] > 0 else 'negatively'} correlated (r={c['r']:.2f}, n={c['n']})",
                                     **c))
            break  # one headline correlation is enough; the full list is in `correlations`

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["series"] or ""))
    if not any(f["severity"] in ("warning", "critical") for f in findings) and clean:
        findings.append(_finding("info", "all_clear", None, "未发现明显异常：趋势平稳、无显著离群点/卡死/缺失",
                                 "No notable issues: stable trends, no significant outliers, stuck readings or gaps"))
    return {"series": stats_out, "findings": findings, "correlations": corr, "sum_check": sum_check, "thresholds": th}
