"""Pure logic for the AI-generated ROI / total-cost-of-ownership analysis.

The platform knows an asset's purchase value, its maintenance records and costs, work
orders and alarms — it does NOT know the *benefit* an analyzer produces (avoided
downtime, product quality...). So:

* cost-side indicators are always computed from platform data;
* ROI % and payback are computed only when the caller supplies `annual_benefit`;
* anything that could not be computed is reported in `data_gaps` instead of being guessed.

The flag thresholds are internal heuristics (returned in `thresholds`, overridable per
call) — not an industry standard.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

DAYS_PER_MONTH = 30.4375
PREVENTIVE = {"calibration", "inspection", "maintenance", "preventive", "routine", "校准", "巡检", "保养", "预防性维护"}
CORRECTIVE = {"repair", "replacement", "fault", "corrective", "emergency", "breakdown", "维修", "更换", "故障", "抢修"}
DEFAULT_THRESHOLDS = {
    "maint_ratio_high_pct": 15.0,      # annualised maintenance cost / purchase value
    "corrective_share_high_pct": 60.0,  # corrective share of work orders (needs >= 3 orders)
    "nbv_low_pct": 20.0,               # net book value / purchase value
}


def classify_order(order_type: Any) -> str:
    t = str(order_type or "").strip().lower()
    if t in PREVENTIVE:
        return "preventive"
    if t in CORRECTIVE:
        return "corrective"
    return "other"


def _aware(dt: Any) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if isinstance(dt, date):
        return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    return None


def months_between(d0: Any, d1: Any) -> Optional[float]:
    a, b = _aware(d0), _aware(d1)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds() / 86400 / DAYS_PER_MONTH)


def _msg(code: str, sev: str, zh: str, en: str, **kw: Any) -> dict:
    return {"code": code, "severity": sev, "zh": zh, "en": en, **kw}


def compute_roi(
    asset: Optional[dict],
    records: list[dict],
    work_orders: list[dict],
    alarms_in_period: int,
    period_months: int,
    *,
    annual_benefit: Optional[float] = None,
    now: Optional[datetime] = None,
    thresholds: Optional[dict] = None,
) -> dict:
    """asset: {asset_code,name,purchase_value,depreciation_years,purchase_date}|None
    records: [{cost, performed_at}]   work_orders: [{order_type,status,created_at}]"""
    th = dict(DEFAULT_THRESHOLDS, **(thresholds or {}))
    now = _aware(now) or datetime.now(timezone.utc)
    period_months = max(1, int(period_months))
    start = now - timedelta(days=DAYS_PER_MONTH * period_months)

    recs = [r for r in records if (_aware(r.get("performed_at")) or now) >= start]
    wos = [w for w in work_orders if (_aware(w.get("created_at")) or now) >= start]
    with_cost = [r for r in recs if r.get("cost") is not None]
    maint_cost = round(sum(float(r["cost"]) for r in with_cost), 2)
    kinds = [classify_order(w.get("order_type")) for w in wos]
    corrective, preventive = kinds.count("corrective"), kinds.count("preventive")

    m: dict[str, Any] = {
        "period_months": period_months,
        "maintenance_cost": maint_cost,
        "maintenance_records": len(recs),
        "records_with_cost": len(with_cost),
        "work_orders": len(wos),
        "corrective_orders": corrective,
        "preventive_orders": preventive,
        "other_orders": kinds.count("other"),
        "corrective_share_pct": round(corrective / len(wos) * 100, 1) if wos else None,
        "alarms": int(alarms_in_period),
        "cost_per_work_order": round(maint_cost / len(wos), 2) if wos and maint_cost else None,
        "annual_maintenance_cost": round(maint_cost * 12 / period_months, 2),
    }
    gaps: list[dict] = []
    assumptions: list[dict] = []
    formulas: list[dict] = [
        _msg("f_ann", "info", "年化维护成本 = 期间维护成本 × 12 ÷ 期间月数", "Annualised maintenance cost = period maintenance cost × 12 ÷ period months"),
    ]

    pv = float(asset["purchase_value"]) if asset and asset.get("purchase_value") is not None else None
    if asset is None:
        gaps.append(_msg("no_asset", "warning", "该设备未关联资产，无法计算购置价值、折旧、净值等指标",
                         "No asset is linked to this device — purchase value, depreciation and net book value cannot be computed"))
    elif not pv:
        gaps.append(_msg("no_purchase_value", "warning", "资产未填写购置价值，无法计算折旧与占比类指标",
                         "The asset has no purchase value — depreciation and ratio metrics cannot be computed"))
    if not recs:
        gaps.append(_msg("no_records", "info", "期间内没有维护记录，维护成本按 0 计",
                         "No maintenance records in the period — maintenance cost counts as 0"))
    elif not with_cost:
        gaps.append(_msg("no_costs", "warning", f"期间内有 {len(recs)} 条维护记录，但都没有填写费用",
                         f"{len(recs)} maintenance record(s) in the period, none with a cost"))
    elif len(with_cost) < len(recs):
        gaps.append(_msg("some_costs_missing", "info", f"{len(recs) - len(with_cost)} 条维护记录没有费用，未计入",
                         f"{len(recs) - len(with_cost)} record(s) without a cost were not counted"))

    dep_period = nbv = None
    if pv:
        years = int(asset.get("depreciation_years") or 10)  # type: ignore[union-attr]
        monthly = pv / max(years * 12, 1)
        age = months_between(asset.get("purchase_date"), now)  # type: ignore[union-attr]
        if age is None:
            age = float(period_months)
            gaps.append(_msg("no_purchase_date", "info", "资产未填写购置日期，净值按“已使用期间月数”估算",
                             "The asset has no purchase date — net book value is estimated from the period length"))
            assumptions.append(_msg("age_assumed", "info", "净值假设设备已使用的时间等于所选期间", "Net book value assumes the asset's age equals the selected period"))
        dep_period = round(min(monthly * period_months, pv), 2)
        nbv = round(max(pv - monthly * age, 0.0), 2)
        m.update(purchase_value=round(pv, 2), depreciation_years=years, asset_age_months=round(age, 1),
                 monthly_depreciation=round(monthly, 2), depreciation_in_period=dep_period, net_book_value=nbv,
                 net_book_value_pct=round(nbv / pv * 100, 1), maintenance_cost_ratio_pct=round(m["annual_maintenance_cost"] / pv * 100, 2),
                 total_cost_period=round(dep_period + maint_cost, 2))
        formulas += [
            _msg("f_dep", "info", "月折旧 = 购置价值 ÷ (折旧年限 × 12)（直线法）", "Monthly depreciation = purchase value ÷ (depreciation years × 12) (straight line)"),
            _msg("f_ratio", "info", "维护成本占比 = 年化维护成本 ÷ 购置价值", "Maintenance cost ratio = annualised maintenance cost ÷ purchase value"),
            _msg("f_tco", "info", "期间总成本 = 期间折旧 + 期间维护成本", "Period total cost = period depreciation + period maintenance cost"),
        ]

    if annual_benefit is not None and annual_benefit >= 0:
        benefit_period = round(annual_benefit * period_months / 12, 2)
        m["annual_benefit"] = round(annual_benefit, 2)
        m["benefit_period"] = benefit_period
        if dep_period is not None:
            cost_period = dep_period + maint_cost
            m["net_benefit_period"] = round(benefit_period - cost_period, 2)
            m["roi_pct"] = round((benefit_period - cost_period) / cost_period * 100, 1) if cost_period else None
            net_monthly = annual_benefit / 12 - m["annual_maintenance_cost"] / 12
            m["payback_months"] = round(pv / net_monthly, 1) if pv and net_monthly > 0 else None
            formulas += [
                _msg("f_roi", "info", "ROI = (期间收益 − 期间总成本) ÷ 期间总成本", "ROI = (period benefit − period total cost) ÷ period total cost"),
                _msg("f_payback", "info", "回收期(月) = 购置价值 ÷ (月收益 − 月维护成本)", "Payback (months) = purchase value ÷ (monthly benefit − monthly maintenance cost)"),
            ]
            assumptions.append(_msg("benefit_input", "info", "收益为你输入的估计值，平台无法核实", "The benefit is your own estimate; the platform cannot verify it"))
        else:
            gaps.append(_msg("roi_needs_asset", "warning", "已提供年化收益，但缺少购置价值，无法计算 ROI 与回收期",
                             "An annual benefit was given but the purchase value is missing — ROI and payback cannot be computed"))
    else:
        gaps.append(_msg("no_benefit", "info", "未提供年化收益：以下只有成本侧指标，无法给出 ROI% 与回收期（平台不记录设备带来的收益）",
                         "No annual benefit provided — only cost-side indicators are shown; ROI % and payback need a benefit estimate (the platform does not record it)"))

    flags: list[dict] = []
    ratio = m.get("maintenance_cost_ratio_pct")
    if ratio is not None and ratio >= th["maint_ratio_high_pct"]:
        flags.append(_msg("high_maintenance_ratio", "warning", f"年化维护成本约占购置价值的 {ratio:.1f}%（阈值 {th['maint_ratio_high_pct']:g}%），建议评估继续维修与更换的成本对比",
                          f"Annualised maintenance cost is ≈ {ratio:.1f}% of purchase value (threshold {th['maint_ratio_high_pct']:g}%) — compare repair vs replace"))
    share = m["corrective_share_pct"]
    if share is not None and len(wos) >= 3 and share >= th["corrective_share_high_pct"]:
        flags.append(_msg("corrective_heavy", "warning", f"故障性工单占比 {share:.0f}%（{corrective}/{len(wos)}），预防性维护偏少",
                          f"Corrective orders are {share:.0f}% of work orders ({corrective}/{len(wos)}) — little preventive maintenance"))
    pct = m.get("net_book_value_pct")
    if pct is not None and pct <= th["nbv_low_pct"]:
        flags.append(_msg("near_end_of_life", "info", f"账面净值仅剩购置价值的 {pct:.0f}%，接近折旧期末，建议规划更新预算",
                          f"Net book value is only {pct:.0f}% of purchase value — near the end of its depreciation life; plan a replacement budget"))
    if m.get("roi_pct") is not None and m["roi_pct"] < 0:
        flags.append(_msg("negative_roi", "warning", f"按所填收益，期间 ROI 为 {m['roi_pct']:.1f}%（收益不足以覆盖期间总成本）",
                          f"With the benefit you entered, period ROI is {m['roi_pct']:.1f}% (benefit does not cover total cost)"))
    if m["work_orders"] and not preventive and m["work_orders"] >= 3:
        flags.append(_msg("no_preventive", "info", "期间没有任何预防性（校准/巡检/保养）工单", "No preventive (calibration / inspection / maintenance) work orders in the period"))

    assumptions.append(_msg("order_classes", "info",
                            "工单类型归类：校准/巡检/保养 = 预防性；维修/更换/故障 = 故障性；其他不计入占比分子",
                            "Order types: calibration / inspection / maintenance = preventive; repair / replacement / fault = corrective; others are not counted as corrective"))
    return {"metrics": m, "flags": flags, "data_gaps": gaps, "assumptions": assumptions, "formulas": formulas, "thresholds": th}
