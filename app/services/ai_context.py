"""Cross-module live-data context for AI Center chat — "深度绑定": lets chat
read real platform data (设备/通讯/数据分析/维护/资产/实验室/报警) instead of
only the knowledge_chunks documents that plain RAG covers.

`gather_live_context()` turns a free-text question into a compact snapshot of
the actual current database: which sections it includes depends on keywords
in the question (bilingual EN/CN), and it narrows to one device's detail +
recent readings when the question names an existing device_code/name. This
runs before the LLM call, the same "retrieve, then generate" pattern
app/services/rag.py already uses for documents — just against structured
tables instead of chunked text — so answers like "现在有哪些设备离线"、
"这台设备最近读数怎么样"、"未关闭的工单有哪些" are grounded in real data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.alarm import Alarm
from app.models.device import Device, DeviceComponent, Gateway
from app.models.maintenance import Asset, LabCorrelationResult, MaintenanceSOP, WorkOrder
from app.models.measurement import Measurement

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "devices": ("设备", "device", "分析仪", "analyzer"),
    "communication": ("通讯", "通信", "网关", "gateway", "communication"),
    "measurements": ("数据", "读数", "趋势", "曲线", "measurement", "trend", "分析", "analysis"),
    "maintenance": ("维护", "保养", "工单", "maintenance", "work order", "维修"),
    "assets": ("资产", "asset", "折旧", "depreciation"),
    "lab": ("实验室", "化验", "lab", "correlation", "比对"),
    "alarms": ("报警", "告警", "alarm", "故障", "fault"),
}

_MAX_LIST = 20

logger = logging.getLogger(__name__)


def _matches(question: str, section: str) -> bool:
    q = question.lower()
    return any(kw.lower() in q for kw in _KEYWORDS.get(section, ()))


def _find_mentioned_device(db: Session, question: str) -> Optional[Device]:
    """Best-effort: does the question contain an existing device_code or name?"""
    q = question.lower()
    devices = db.scalars(select(Device)).all()
    for d in devices:
        if d.device_code and d.device_code.lower() in q:
            return d
    for d in devices:
        if d.name and len(d.name) > 1 and d.name.lower() in q:
            return d
    return None


def _overview(db: Session) -> str:
    total = db.scalar(select(func.count()).select_from(Device)) or 0
    online = db.scalar(select(func.count()).select_from(Device).where(Device.status == "online")) or 0
    fault = db.scalar(select(func.count()).select_from(Device).where(Device.status == "fault")) or 0
    active_alarms = db.scalar(select(func.count()).select_from(Alarm).where(Alarm.status == "active")) or 0
    open_wo = (
        db.scalar(select(func.count()).select_from(WorkOrder).where(WorkOrder.status.in_(["open", "in_progress"])))
        or 0
    )
    return (
        f"平台概况：设备总数 {total}，在线 {online}，故障 {fault}；"
        f"活动报警 {active_alarms} 条；未关闭工单 {open_wo} 个。"
    )


def _device_section(db: Session, device: Optional[Device]) -> str:
    if device is not None:
        lines = [
            f"设备详情 — {device.device_code}（{device.name}）：状态={device.status}，"
            f"健康分={float(device.health_score):.1f}，位置={device.location or '未知'}，"
            f"通讯协议={device.protocol}，IP={device.ip_address or '无'}。"
        ]
        tags = db.scalars(select(DeviceComponent).where(DeviceComponent.device_id == device.id)).all()
        if tags:
            tag_lines = []
            for t in tags[:_MAX_LIST]:
                gw = f"，网关={t.gateway.gateway_code}" if t.gateway_id and t.gateway else ""
                tag_lines.append(f"  - {t.component_name}（单位={t.unit}{gw}）")
            lines.append("关联TAG：\n" + "\n".join(tag_lines))
        asset = db.scalar(select(Asset).where(Asset.device_id == device.id))
        if asset:
            lines.append(f"关联资产：{asset.asset_code} {asset.name}，状态={asset.status}")
        if device.model_id:
            sops = db.scalars(
                select(MaintenanceSOP).where(MaintenanceSOP.device_model_id == device.model_id).limit(5)
            ).all()
            if sops:
                lines.append("相关维护SOP：\n" + "\n".join(f"  - {s.title}" for s in sops))
        return "\n".join(lines)

    devices = db.scalars(select(Device).order_by(Device.id).limit(_MAX_LIST)).all()
    if not devices:
        return "设备管理：当前没有任何设备记录。"
    lines = [f"- {d.device_code} {d.name}：{d.status}，健康分{float(d.health_score):.0f}" for d in devices]
    return f"设备列表（最多{_MAX_LIST}条）：\n" + "\n".join(lines)


def _communication_section(db: Session) -> str:
    gateways = db.scalars(select(Gateway).order_by(Gateway.id).limit(_MAX_LIST)).all()
    if not gateways:
        return "通讯管理：当前没有任何网关记录。"
    lines = [f"- {g.gateway_code} {g.name}：{g.status}，协议={g.protocol}" for g in gateways]
    return f"网关列表（最多{_MAX_LIST}条）：\n" + "\n".join(lines)


def _measurement_section(db: Session, device: Optional[Device]) -> Optional[str]:
    if device is None:
        return None
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = db.scalars(
        select(Measurement).where(Measurement.device_id == device.id, Measurement.time >= since)
    ).all()
    if not rows:
        return f"设备 {device.device_code} 最近24小时没有测量数据。"
    by_comp: dict[str, list[float]] = {}
    for r in rows:
        by_comp.setdefault(r.component_name, []).append(r.value)
    lines = []
    for comp, vals in list(by_comp.items())[:15]:
        lines.append(
            f"- {comp}: 最新={vals[-1]:.4g}, 最小={min(vals):.4g}, 最大={max(vals):.4g}, "
            f"平均={sum(vals) / len(vals):.4g}, 样本数={len(vals)}"
        )
    return f"设备 {device.device_code} 最近24小时各组分统计：\n" + "\n".join(lines)


def _maintenance_section(db: Session, device: Optional[Device]) -> str:
    stmt = select(WorkOrder).where(WorkOrder.status.in_(["open", "in_progress"]))
    if device is not None:
        stmt = stmt.where(WorkOrder.device_id == device.id)
    rows = db.scalars(stmt.order_by(WorkOrder.created_at.desc()).limit(15)).all()
    if not rows:
        scope = f"（设备 {device.device_code}）" if device else ""
        return f"维护管理：当前没有未关闭的工单{scope}。"
    lines = [f"- #{w.id} {w.title}（类型={w.order_type}，优先级={w.priority}，状态={w.status}）" for w in rows]
    return "未关闭工单：\n" + "\n".join(lines)


def _assets_section(db: Session, device: Optional[Device]) -> str:
    stmt = select(Asset)
    if device is not None:
        stmt = stmt.where(Asset.device_id == device.id)
    rows = db.scalars(stmt.order_by(Asset.id).limit(_MAX_LIST)).all()
    if not rows:
        return "资产管理：没有匹配的资产记录。"
    total_value = sum(float(a.current_value if a.current_value is not None else (a.purchase_value or 0)) for a in rows)
    lines = [
        f"- {a.asset_code} {a.name}：状态={a.status}，现值≈{float(a.current_value or 0):.2f}" for a in rows
    ]
    return f"资产（共{len(rows)}项，现值合计≈{total_value:.2f}）：\n" + "\n".join(lines)


def _lab_section(db: Session, device: Optional[Device]) -> str:
    stmt = select(LabCorrelationResult)
    if device is not None:
        stmt = stmt.where(LabCorrelationResult.device_id == device.id)
    rows = db.scalars(stmt.order_by(LabCorrelationResult.computed_at.desc()).limit(10)).all()
    if not rows:
        return "实验室：没有在线-化验比对/相关性结果。"
    lines = [
        f"- {r.component_name}: R²={float(r.r_squared or 0):.3f}, 样本数={r.sample_count}, "
        f"平均偏差={float(r.mean_deviation or 0):.4g}"
        for r in rows
    ]
    return "实验室在线-化验比对结果：\n" + "\n".join(lines)


def _alarms_section(db: Session, device: Optional[Device]) -> str:
    stmt = select(Alarm).where(Alarm.status == "active")
    if device is not None:
        stmt = stmt.where(Alarm.device_id == device.id)
    rows = db.scalars(stmt.order_by(Alarm.triggered_at.desc()).limit(15)).all()
    if not rows:
        return "当前没有活动报警。"
    lines = [f"- [{a.severity}] {a.message}" for a in rows]
    return "活动报警：\n" + "\n".join(lines)


def _selection_section(db: Session, question: str) -> Optional[str]:
    """选型: rank devices + models for a requirement phrased in the question."""
    from app.services import device_search
    from app.services import device_selection as ds

    req, considered, ranked, models = device_search.select_devices_raw(db, question, limit=5)
    if not ranked and not models:
        return None
    return ds.render_selection_text(req, considered, ranked, models)


def _search_section(db: Session, question: str) -> Optional[str]:
    """搜索: devices matching the status / component / type / protocol / keyword
    conditions found in the question. Omitted when nothing matches, so a query the
    parser only half-understood never tells the LLM 'no such devices'."""
    from app.services import device_search
    from app.services import device_selection as ds

    parsed = ds.parse_query(question)
    total, hits = ds.search(device_search.load_profiles(db), parsed, limit=10)
    if not total:
        return None
    return ds.render_search_text(parsed, total, hits)


def gather_live_context(db: Session, question: str, device_id: Optional[int] = None) -> str:
    """Compact text block of live platform data relevant to `question`, meant
    to be prepended to an LLM prompt alongside (or instead of) knowledge-base
    RAG excerpts. Always includes a one-line plant overview; adds a section
    per module the question's keywords touch, scoped to a specific device
    when one is given (`device_id`, e.g. the device picked in AI Center) or when
    the question names one (by device_code or name).

    Also bound to the device search / 选型 engine (app/services/device_search.py):
    questions like "推荐能测H2S的在线设备" or "现在有哪些设备离线" get ranked
    candidates / matching devices from the same engine AI Center's picker uses."""
    from app.services import device_selection as ds

    selected = db.get(Device, device_id) if device_id else None
    device = selected if selected is not None else _find_mentioned_device(db, question)
    sections = [_overview(db)]
    added: set[str] = set()

    def add(name: str, text: Optional[str]) -> None:
        if text and name not in added:
            added.add(name)
            sections.append(text)

    matched_any = False
    if _matches(question, "devices"):
        add("devices", _device_section(db, device))
        matched_any = True
    if _matches(question, "communication"):
        add("communication", _communication_section(db))
        matched_any = True
    if _matches(question, "measurements"):
        add("measurements", _measurement_section(db, device))
        matched_any = True
    if _matches(question, "maintenance"):
        add("maintenance", _maintenance_section(db, device))
        matched_any = True
    if _matches(question, "assets"):
        add("assets", _assets_section(db, device))
        matched_any = True
    if _matches(question, "lab"):
        add("lab", _lab_section(db, device))
        matched_any = True
    if _matches(question, "alarms"):
        add("alarms", _alarms_section(db, device))
        matched_any = True

    # A device was selected (AI Center picker) or named, but no module keyword
    # matched — still worth surfacing its detail + recent readings, since that's
    # almost certainly what's being asked about.
    if device is not None and not matched_any:
        add("devices", _device_section(db, device))
        add("measurements", _measurement_section(db, device))
    # An explicitly selected device means "this device" whatever the wording
    # ("它现在怎么样"), so always include its alarms and open work orders too.
    if selected is not None:
        add("devices", _device_section(db, selected))
        add("measurements", _measurement_section(db, selected))
        add("alarms", _alarms_section(db, selected))
        add("maintenance", _maintenance_section(db, selected))

    # Device search / 选型 — never let a failure here break the chat itself.
    try:
        parsed = ds.parse_query(question)
        wants_selection = ds.has_selection_intent(question) and (
            ds.has_facets(parsed) or _matches(question, "devices") or "型号" in question or "model" in question.lower()
        )
        if wants_selection:
            add("selection", _selection_section(db, question))
        elif device is None and (ds.has_search_intent(question) or ds.has_facets(parsed)):
            add("search", _search_section(db, question))
    except Exception:  # noqa: BLE001
        logger.exception("device search/selection context failed")

    return "\n\n".join(sections)
