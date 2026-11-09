"""Prompts and offline templates for the AI-generated SOP / ROI / analysis reading / lab
certification text.

Division of labour: figures are computed elsewhere (roi_analysis, analysis_reading,
lab_analysis) and are passed in as facts. The LLM — when one is configured — only phrases
them, and is told not to invent numbers or specifications. When no LLM is available the same
facts are rendered by the templates below, so every feature works offline.
"""
from __future__ import annotations

from typing import Any, Optional

import re

_UNIT = r"(?:%|ppm|ppb|psi|bar|kpa|mpa|℃|°c|°f|nm|ml|l/min|sccm|min|分钟|小时|天|个月|月|年|hours?|days?|months?)"
_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(\s?" + _UNIT + r")?", re.I)


def _norm_num(tok: str) -> str:
    v = float(tok.replace(",", ""))
    return str(int(v)) if v == int(v) else repr(round(v, 6))


_UNIT_CLASS = {"month": "month", "months": "month", "个月": "month", "月": "month", "hour": "hour", "hours": "hour", "小时": "hour",
               "day": "day", "days": "day", "天": "day", "min": "min", "分钟": "min", "年": "year"}


def _unit_class(unit: str) -> str:
    u = unit.strip().lower()
    return _UNIT_CLASS.get(u, u)


def ungrounded_numbers(text: str, facts: str) -> list[str]:
    """Numbers in LLM output that are not in the facts it was given.

    * a plain integer up to 20 (step numbers, [n] citations, item counts) is ignored;
    * a small integer WITH a unit or % ("±2%", "5 psi") must appear in the facts with that same
      unit — a bare "5" from a heading such as "## 5. Procedure" must not vouch for an invented
      "±5%" tolerance;
    * everything else (decimals, > 20) must appear somewhere in the facts, however formatted."""
    known: set[str] = set()
    known_with_unit: set[tuple[str, str]] = set()
    for m in _NUM.finditer(facts):
        n = _norm_num(m.group(1))
        known.add(n)
        if m.group(2):
            known_with_unit.add((n, _unit_class(m.group(2))))
    bad: list[str] = []
    for m in _NUM.finditer(text):
        tok, unit = m.group(1), m.group(2)
        n = _norm_num(tok)
        small = "." not in tok and float(tok.replace(",", "")) <= 20
        if small:
            if unit is None or (n, _unit_class(unit)) in known_with_unit:
                continue
        elif n in known:
            continue
        bad.append(m.group(0).strip())
    return bad


SOP_TYPES = {"calibration": "Calibration", "replacement": "Replacement", "maintenance": "Maintenance"}
_TYPE_ZH = {"Calibration": "校准", "Replacement": "更换", "Maintenance": "维护保养"}
STRICT_SYSTEM = {
    "zh": "你是工业在线分析仪运维助手。只能使用用户提供的资料与数字；资料里没有的规格、数值、步骤细节一律写“以厂家手册为准”，不要编造。用中文简洁回答。",
    "en": "You are an industrial process-analyzer O&M assistant. Use ONLY the material and figures provided; for any spec, value or step detail that is not in the material write 'per the manufacturer manual' — never invent. Answer concisely in English.",
}


def normalize_sop_type(value: str) -> Optional[str]:
    return SOP_TYPES.get((value or "").strip().lower())


def _t(lang: str, zh: str, en: str) -> str:
    return zh if lang == "zh" else en


# ---------------------------------------------------------------- SOP ----
_GENERIC_STEPS = {
    "Calibration": {
        "zh": ["确认分析仪处于可校准状态，记录当前读数与报警", "准备有证标准气体/标准样品，核对有效期与浓度", "按厂家手册对分析仪隔离并通入零点气，待读数稳定后做零点校准",
               "通入量程标准气，待读数稳定后做量程校准", "恢复采样并复核：用标准气再测一次，确认偏差在验收范围内", "记录校准前后读数、标准物质批号、操作人，并更新校准记录"],
        "en": ["Confirm the analyzer can be calibrated; record current readings and alarms", "Prepare certified calibration gas / standards and check expiry and concentration",
               "Isolate the analyzer per the manual, introduce zero gas and zero-calibrate once the reading is stable",
               "Introduce span gas and span-calibrate once stable", "Restore sampling and verify with the standard again; confirm deviation is within acceptance limits",
               "Record before/after readings, standard lot numbers and operator; update the calibration record"],
    },
    "Replacement": {
        "zh": ["确认更换范围与备件型号，核对备件库存与到货状态", "按规程停用并隔离分析仪，泄压、断电并挂牌上锁", "拆下旧部件，检查安装面/密封件，记录旧件状态",
               "安装新部件，按力矩和方向要求紧固，检查密封", "恢复供气/供电，做泄漏检查与自检", "重新校准并观察稳定性，记录更换部件、批号与日期"],
        "en": ["Confirm scope and spare part model; check stock and delivery", "Take the analyzer out of service per procedure: depressurise, isolate power, lock-out/tag-out",
               "Remove the old part; inspect mating surfaces and seals; record its condition", "Fit the new part to the specified torque/orientation and check the seals",
               "Restore gas/power; run a leak check and self-test", "Recalibrate, watch stability, and record the part, lot number and date"],
    },
    "Maintenance": {
        "zh": ["查看历史工单、报警与上次保养记录", "外观与环境检查：管线、接头、伴热、过滤器、样品处理系统", "检查载气/仪表气压力与流量，必要时更换过滤器/干燥剂",
               "检查关键参数（温度、压力、流量）是否在正常范围", "按需清洁或更换易耗件，并做功能自检", "记录检查结果与更换件，安排下次保养"],
        "en": ["Review past work orders, alarms and the last service record", "Inspect condition and environment: tubing, fittings, heat tracing, filters, sample conditioning",
               "Check carrier / instrument-gas pressure and flow; replace filters / dryers if needed", "Check key parameters (temperature, pressure, flow) are in range",
               "Clean or replace consumables as required; run a functional self-test", "Record findings and replaced parts; schedule the next service"],
    },
}


def sop_template(ctx: dict, lang: str = "zh") -> tuple[str, str]:
    """(title, markdown content) assembled from platform facts. Generic steps are only used
    when no vetted SOP exists for this model + type, and are flagged as such."""
    d, st = ctx["device"], ctx["sop_type"]
    title = _t(lang, f"{d['code']} {_TYPE_ZH[st]}作业指导书（AI 草稿）", f"{d['code']} {st} SOP (AI draft)")
    L: list[str] = [f"# {title}", "",
                    _t(lang, "> ⚠ **草稿：由 AI 依据平台数据生成，未经批准，执行前必须由具备资质的人员审核。**",
                       "> ⚠ **DRAFT — generated by AI from platform data. Not approved; must be reviewed by a qualified person before use.**"), "",
                    _t(lang, "## 1. 适用对象", "## 1. Scope"),
                    _t(lang, f"- 设备：{d['code']}（{d.get('name') or '-'}）\n- 型号：{d.get('model') or '未登记'}　厂商：{d.get('manufacturer') or '-'}　类型：{d.get('analyzer_type') or '-'}\n- 位置：{d.get('location') or '-'}",
                       f"- Device: {d['code']} ({d.get('name') or '-'})\n- Model: {d.get('model') or 'unregistered'}  Manufacturer: {d.get('manufacturer') or '-'}  Type: {d.get('analyzer_type') or '-'}\n- Location: {d.get('location') or '-'}")]
    if ctx.get("component"):
        L.append(_t(lang, f"- 关注组分：{ctx['component']}", f"- Component of interest: {ctx['component']}"))
    L += ["", _t(lang, "## 2. 作业前确认（来自平台当前数据）", "## 2. Pre-checks (from current platform data)")]
    pre = []
    for a in ctx.get("open_alarms", [])[:5]:
        pre.append(_t(lang, f"- 存在活动报警：[{a.get('severity')}] {a.get('component') or ''} {a.get('message')}", f"- Active alarm: [{a.get('severity')}] {a.get('component') or ''} {a.get('message')}"))
    for w in ctx.get("open_work_orders", [])[:5]:
        pre.append(_t(lang, f"- 未关闭工单 #{w['id']}：{w['title']}（{w.get('order_type')}，{w.get('priority')}）", f"- Open work order #{w['id']}: {w['title']} ({w.get('order_type')}, {w.get('priority')})"))
    dg = ctx.get("last_diagnosis")
    if dg:
        pre.append(_t(lang, f"- 最近一次 AI 诊断（{dg.get('component')}）：{dg.get('diagnosis')}", f"- Latest AI diagnosis ({dg.get('component')}): {dg.get('diagnosis')}"))
    corr = ctx.get("correlation")
    if corr:
        pre.append(_t(lang, f"- 最近一次化验比对：R²={corr.get('r_squared')}，平均偏差 {corr.get('mean_deviation')}", f"- Latest lab comparison: R²={corr.get('r_squared')}, mean deviation {corr.get('mean_deviation')}"))
    L += pre or [_t(lang, "- 平台上没有该设备的活动报警、未关闭工单或诊断记录", "- No active alarms, open work orders or diagnoses on record for this device")]
    L += ["", _t(lang, "## 3. 安全", "## 3. Safety"),
          _t(lang, "- 按现场规程办理作业许可，佩戴所需防护用品；涉及可燃/有毒气体时先确认气体检测与通风。\n- 具体隔离、泄压、上锁挂牌要求以厂家手册和现场规程为准。",
             "- Obtain the work permit per site rules and wear the required PPE; for flammable/toxic gases confirm gas detection and ventilation first.\n- Isolation, depressurisation and lock-out/tag-out details: per the manufacturer manual and site procedures.")]
    L += ["", _t(lang, "## 4. 备件与耗材", "## 4. Spare parts and consumables")]
    parts = ctx.get("spare_parts", [])
    if parts:
        for p in parts[:8]:
            low = " ⚠ 库存为 0" if p.get("stock_qty", 0) <= 0 and lang == "zh" else " ⚠ out of stock" if p.get("stock_qty", 0) <= 0 else ""
            L.append(f"- {p.get('part_code')} {p.get('name')}：{_t(lang, '库存', 'stock')} {p.get('stock_qty')}{low}")
    else:
        L.append(_t(lang, "- 平台没有登记该型号的备件，请按厂家手册准备。", "- No spare parts registered for this model; prepare per the manufacturer manual."))
    L += ["", _t(lang, "## 5. 作业步骤", "## 5. Procedure")]
    base = ctx.get("base_sop")
    if base:
        L += [_t(lang, f"*以下取自 SOP 库《{base['title']}》（版本 {base.get('version') or '-'}），为已有的规范内容：*", f"*Taken from the SOP library “{base['title']}” (version {base.get('version') or '-'}) — existing vetted content:*"), "", base["content"].strip()]
    else:
        L.append(_t(lang, "*SOP 库中没有该型号的此类规程，以下为通用步骤，务必对照厂家手册补全具体参数：*", "*The SOP library has no procedure for this model and type. The steps below are GENERIC — complete the specifics from the manufacturer manual:*"))
        for i, s in enumerate(_GENERIC_STEPS[st][lang if lang in ("zh", "en") else "en"], 1):
            L.append(f"{i}. {s}")
    L += ["", _t(lang, "## 6. 验收", "## 6. Acceptance"),
          _t(lang, "- 验收偏差范围：以厂家手册/你们的验收标准为准（本草稿不设定具体数值）。", "- Acceptance limits: per the manufacturer manual / your own acceptance standard (this draft does not set specific values).")]
    if ctx.get("kb"):
        L += ["", _t(lang, "## 7. 参考知识库资料", "## 7. Knowledge-base references")]
        for i, k in enumerate(ctx["kb"], 1):
            L.append(f"- [{i}] {k['title']}：{k['excerpt']}")
    L += ["", _t(lang, "## 8. 记录与签署", "## 8. Records and sign-off"),
          _t(lang, "- 记录：作业前后读数、使用的标准物质/备件批号、发现的问题。\n- 执行人：________　审核人：________　日期：________", "- Record: before/after readings, standard / part lot numbers, findings.\n- Performed by: ________  Reviewed by: ________  Date: ________")]
    return title, "\n".join(L)


def sop_prompt(ctx: dict, template_text: str, lang: str = "zh") -> str:
    kb = "\n".join(f"[{i}] {k['title']}: {k['excerpt']}" for i, k in enumerate(ctx.get("kb", []), 1)) or "(none)"
    return (
        f"Write a {ctx['sop_type']} standard operating procedure (SOP) in {'Chinese' if lang == 'zh' else 'English'} in Markdown "
        f"for the analyzer below. It is a DRAFT for human review: keep the draft banner.\n"
        f"Rules: use only the facts in the DRAFT and KNOWLEDGE sections; keep every ⚠, stock and alarm fact; do not invent specifications, "
        f"tolerances, torques or part numbers — write 'per the manufacturer manual' instead; cite knowledge as [n].\n\n"
        f"KNOWLEDGE:\n{kb}\n\nDRAFT (already assembled from platform data — improve wording, order and completeness, keep the facts):\n{template_text}"
    )


# ---------------------------------------------------------------- ROI ----
def _money(v: Any) -> str:
    return "-" if v is None else f"{float(v):,.0f}"


def roi_template(res: dict, lang: str = "zh") -> str:
    m = res["metrics"]
    P: list[str] = []
    if "purchase_value" in m:
        P.append(_t(lang, f"该设备购置价值 {_money(m['purchase_value'])}，按直线法折旧，账面净值约 {_money(m['net_book_value'])}（{m['net_book_value_pct']}%）。",
                    f"Purchase value {_money(m['purchase_value'])}; straight-line depreciated, net book value ≈ {_money(m['net_book_value'])} ({m['net_book_value_pct']}%)."))
    P.append(_t(lang, f"近 {m['period_months']} 个月共 {m['work_orders']} 张工单（故障性 {m['corrective_orders']}、预防性 {m['preventive_orders']}），{m['maintenance_records']} 条维护记录，"
                      f"记录的维护成本 {_money(m['maintenance_cost'])}，期间报警 {m['alarms']} 次。",
                f"Last {m['period_months']} months: {m['work_orders']} work orders ({m['corrective_orders']} corrective, {m['preventive_orders']} preventive), "
                f"{m['maintenance_records']} maintenance records, recorded maintenance cost {_money(m['maintenance_cost'])}, {m['alarms']} alarm(s)."))
    if "maintenance_cost_ratio_pct" in m:
        P.append(_t(lang, f"年化维护成本约 {_money(m['annual_maintenance_cost'])}，占购置价值 {m['maintenance_cost_ratio_pct']}%；期间总成本（折旧+维护）{_money(m['total_cost_period'])}。",
                    f"Annualised maintenance cost ≈ {_money(m['annual_maintenance_cost'])}, {m['maintenance_cost_ratio_pct']}% of purchase value; period total cost (depreciation + maintenance) {_money(m['total_cost_period'])}."))
    if m.get("roi_pct") is not None:
        pb = m.get("payback_months")
        P.append(_t(lang, f"按你提供的年化收益 {_money(m['annual_benefit'])}：期间 ROI {m['roi_pct']}%" + (f"，静态回收期约 {pb} 个月。" if pb else "，按当前收益与维护成本无法回收购置成本。"),
                    f"With your annual benefit of {_money(m['annual_benefit'])}: period ROI {m['roi_pct']}%" + (f", simple payback ≈ {pb} months." if pb else "; the purchase cost is not recovered at this benefit and maintenance cost.")))
    for f in res["flags"]:
        P.append("⚠ " + f[lang])
    for g in res["data_gaps"]:
        P.append("ℹ " + g[lang])
    return "\n\n".join(P)


def roi_prompt(res: dict, template_text: str, lang: str = "zh") -> str:
    return (f"Write a short (≤ 200 words) ROI / total-cost-of-ownership assessment for one analyzer in {'Chinese' if lang == 'zh' else 'English'}, "
            f"with a clear recommendation (keep / improve preventive maintenance / plan replacement) justified by the facts. "
            f"Use ONLY the figures below; do not add or change any number; mention data gaps.\n\nFACTS:\n{template_text}")


# ------------------------------------------------------ analysis reading ----
def reading_template(res: dict, lang: str = "zh") -> str:
    fs = res["findings"]
    head = _t(lang, f"共读取 {len(res['series'])} 条序列，发现 {sum(1 for f in fs if f['severity'] == 'critical')} 个严重、{sum(1 for f in fs if f['severity'] == 'warning')} 个警告。",
              f"Read {len(res['series'])} series: {sum(1 for f in fs if f['severity'] == 'critical')} critical, {sum(1 for f in fs if f['severity'] == 'warning')} warning finding(s).")
    mark = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
    return head + "\n\n" + "\n".join(f"{mark[f['severity']]} {f[lang]}" for f in fs[:10])


def reading_prompt(res: dict, template_text: str, lang: str = "zh") -> str:
    return (f"You are reading analyzer data for an operator. Summarise in ≤ 150 words in {'Chinese' if lang == 'zh' else 'English'}: the most important issue first, "
            f"a likely cause only if the facts support one, and the next check to make. Use ONLY these facts; add no numbers.\n\nFACTS:\n{template_text}")


# --------------------------------------------------------- certification ----
_STATUS_TXT = {"certified": ("通过（认证）", "Certified"), "conditional": ("有条件通过", "Conditional"),
               "failed": ("未通过", "Not certified"), "insufficient": ("数据不足，无法认证", "Insufficient data")}


def status_text(status: str, lang: str) -> str:
    return _STATUS_TXT[status][0 if lang == "zh" else 1]


def cert_template(dev: str, comp: str, stats: dict, cert: dict, window: float, unmatched: int, lang: str = "zh") -> str:
    head = _t(lang, f"{dev} · {comp}：{status_text(cert['status'], 'zh')}（判据通过率 {cert['score']:.0f}%）。", f"{dev} · {comp}: {status_text(cert['status'], 'en')} (criteria passed {cert['score']:.0f}%).")
    basis = _t(lang, f"在线值取自数据分析数据：采样时刻前后 ±{window:g} 分钟内的测量值；{stats.get('pairs', 0)} 个样本成功配对，{unmatched} 个没有在线数据。",
               f"Online values come from the analysis data: measurements within ±{window:g} min of the sampling time; {stats.get('pairs', 0)} sample(s) paired, {unmatched} had no online data.")
    lines = [head, basis, ""] + [("✓ " if k["ok"] else "✗ ") + k[lang] for k in cert["checks"]]
    if cert["status"] == "conditional":
        lines += ["", _t(lang, "精度指标合格，但存在系统偏差/漂移，建议校准或排查后复核。", "Accuracy criteria pass, but there is a systematic bias or drift — recalibrate or investigate, then re-check.")]
    elif cert["status"] == "failed":
        lines += ["", _t(lang, "关键精度/覆盖指标未达标，不建议以该在线值替代化验值；请检查校准、采样同步与样品处理。", "Key accuracy / coverage criteria are not met; do not use the online value in place of the lab value — check calibration, sampling synchronisation and sample handling.")]
    elif cert["status"] == "insufficient":
        lines += ["", _t(lang, "请补充化验样本或确认样本时间与在线数据时间范围重叠。", "Add lab samples or make sure the sample times overlap the online data.")]
    lines += ["", cert["disclaimer"][lang]]
    return "\n".join(lines)


def cert_prompt(template_text: str, lang: str = "zh") -> str:
    return (f"Explain this lab-vs-online comparison result to a plant chemist in ≤ 120 words in {'Chinese' if lang == 'zh' else 'English'}: what the verdict means, "
            f"the main reason, and one concrete next step. Use ONLY these facts; add no numbers; keep the disclaimer meaning.\n\nFACTS:\n{template_text}")
