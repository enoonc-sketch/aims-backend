"""托盘用的报警摘要：未处理数、其中严重数、最新几条、历史最大 id。

max_id 取全部报警（含已确认 / 已解决）的最大 id，托盘靠它判断“有没有新报警”——
这样把报警处理掉不会让它倒退，也不会把旧报警当新报警再弹一次。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.alarm import Alarm

MAX_MESSAGE_CHARS = 120


def alarm_summary(db: Session, limit: int = 3) -> dict:
    active_q = Alarm.status == "active"
    active = db.scalar(select(func.count()).select_from(Alarm).where(active_q)) or 0
    critical = db.scalar(select(func.count()).select_from(Alarm).where(active_q, Alarm.severity == "critical")) or 0
    max_id = db.scalar(select(func.max(Alarm.id))) or 0
    rows = db.execute(select(Alarm.id, Alarm.severity, Alarm.message).where(active_q)
                      .order_by(Alarm.id.desc()).limit(limit)).all()
    return {
        "active": int(active), "critical": int(critical), "max_id": int(max_id),
        "latest": [{"id": r.id, "severity": r.severity, "message": (r.message or "")[:MAX_MESSAGE_CHARS]} for r in rows],
    }
