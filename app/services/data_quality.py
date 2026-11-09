"""数据质量评估 (实时数据 -> 数据质量 -> 报警)。

之前 `measurements.quality` 只是一个从来不被计算的字符串: 导入路径一律写 "good",
报警和 AI 诊断也从不看它。这里给每条读数一个真实的质量等级:

    good       正常
    uncertain  存疑 —— 仍会参与报警判断(报警信息里会注明存疑原因), 但要让人知道
    bad        不可信 —— 不触发工艺报警、不进入 AI 诊断的证据; 连续出现则升级为「数据质量报警」

设计取舍(重要): 只有**确定不可信**的数据才会被标 bad 并压制工艺报警:
    - 数值不是有限数 (NaN / inf)
    - 时间戳在未来(超过容忍度)
    - 设备/网关自己上报了 bad (对应 OPC 的 Bad 质量码)
统计类判断 —— 尖峰(spike)、冻结(flatline)、超出 TAG 量程、实时数据时间戳过旧 —— 只标 uncertain,
**不**压制报警。原因: 真实的工艺突变从统计上看就是"尖峰", 如果把它标成 bad 并吞掉报警,
就是把最该报的报警漏了。

TAG 的 low_limit/high_limit 在这里被当作**仪表量程**使用(不是报警阈值; 报警阈值在 AlarmRule)。
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.device import Device, DeviceComponent
from app.models.measurement import Measurement

GOOD, UNCERTAIN, BAD = "good", "uncertain", "bad"
_RANK = {GOOD: 0, UNCERTAIN: 1, BAD: 2}

FUTURE_TOLERANCE = timedelta(minutes=5)
STALE_AFTER = timedelta(hours=24)      # 仅实时路径: 时间戳比"现在"旧这么多 -> uncertain
RANGE_TOLERANCE = 0.05                 # 超出 TAG 量程 5% 跨度以上 -> uncertain
FLATLINE_N = 10                        # 连续这么多个完全相同的非零值 -> 疑似冻结
SPIKE_MIN_TAIL = 10
SPIKE_K = 8.0
TAIL_KEEP = 30
DQ_ALARM_CONSECUTIVE_BAD = 3           # 末尾连续 bad 达到这个数 -> 数据质量报警

REASON_TEXT = {
    "not_finite": "数值不是有限数",
    "future_timestamp": "时间戳在未来",
    "client_bad": "设备/网关上报质量为 bad",
    "client_uncertain": "设备/网关上报质量存疑",
    "out_of_tag_range": "超出 TAG 量程",
    "spike": "孤立尖峰",
    "flatline": "数值疑似冻结",
    "stale_timestamp": "时间戳过旧",
}


def worse(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def describe(reasons: Iterable[str]) -> str:
    return "、".join(REASON_TEXT.get(r, r) for r in reasons)


@dataclass
class Assessment:
    quality: str = GOOD
    reasons: list[str] = field(default_factory=list)


class ComponentAssessor:
    """有状态地逐条评估同一 (设备, 组分) 的读数, 按时间顺序喂入。"""

    def __init__(
        self,
        *,
        low: Optional[float],
        high: Optional[float],
        tail: list[float],
        realtime: bool,
        now: datetime,
    ) -> None:
        self.low, self.high = low, high
        self.realtime = realtime
        self.now = now
        self.recent: deque[float] = deque(tail[-TAIL_KEEP:], maxlen=TAIL_KEEP)

    def _far(self, value: float, med: float, scale: float) -> bool:
        return abs(value - med) > SPIKE_K * scale

    def assess(self, value: float, ts: Optional[datetime], client_quality: Optional[str]) -> Assessment:
        reasons: list[str] = []
        quality = GOOD

        cq = (client_quality or GOOD).strip().lower()
        if cq == BAD:
            quality, reasons = BAD, reasons + ["client_bad"]
        elif cq == UNCERTAIN:
            quality, reasons = UNCERTAIN, reasons + ["client_uncertain"]

        if value is None or not math.isfinite(value):
            return Assessment(BAD, reasons + ["not_finite"])

        ts = _aware(ts)
        if ts is not None:
            if ts > self.now + FUTURE_TOLERANCE:
                quality = worse(quality, BAD)
                reasons.append("future_timestamp")
            elif self.realtime and ts < self.now - STALE_AFTER:
                quality = worse(quality, UNCERTAIN)
                reasons.append("stale_timestamp")

        if self.low is not None and self.high is not None and self.high > self.low:
            span = self.high - self.low
            margin = RANGE_TOLERANCE * span
            if value < self.low - margin or value > self.high + margin:
                quality = worse(quality, UNCERTAIN)
                reasons.append("out_of_tag_range")

        if quality != BAD:
            # 冻结: 最近 N-1 个 + 当前 全部相同且非零 (零常见于"该组分不存在", 不算冻结)
            if len(self.recent) >= FLATLINE_N - 1 and value != 0:
                window = list(self.recent)[-(FLATLINE_N - 1):] + [value]
                if max(window) == min(window):
                    quality = worse(quality, UNCERTAIN)
                    reasons.append("flatline")
            # 尖峰: 只标"孤立"的单点; 连续两点都远离基线说明是水平跃迁(可能是真实工艺变化), 不标
            if len(self.recent) >= SPIKE_MIN_TAIL:
                med = statistics.median(self.recent)
                mad = statistics.median(abs(x - med) for x in self.recent)
                scale = max(1.4826 * mad, 0.02 * abs(med), 1e-9)
                if self._far(value, med, scale) and not self._far(self.recent[-1], med, scale):
                    quality = worse(quality, UNCERTAIN)
                    reasons.append("spike")
            self.recent.append(value)

        return Assessment(quality, reasons)


@dataclass
class QualityReport:
    counts: Counter = field(default_factory=Counter)
    by_row: dict[int, Assessment] = field(default_factory=dict)   # id(row) -> Assessment
    trailing_bad: dict[str, int] = field(default_factory=dict)    # 组分 -> 本批末尾连续 bad 数
    all_bad: dict[str, bool] = field(default_factory=dict)        # 组分 -> 本批是否全部 bad
    reasons: dict[str, Counter] = field(default_factory=dict)     # 组分 -> 原因计数

    def summary(self) -> dict:
        return {k: int(self.counts.get(k, 0)) for k in (GOOD, UNCERTAIN, BAD)}


def _load_tail(db: Session, device_id: int, component: str, before: Optional[datetime]) -> list[float]:
    stmt = select(Measurement.value).where(
        Measurement.device_id == device_id,
        Measurement.component_name == component,
        Measurement.quality != BAD,
    )
    if before is not None:
        stmt = stmt.where(Measurement.time < before)
    stmt = stmt.order_by(Measurement.time.desc()).limit(TAIL_KEEP)
    return list(reversed(db.scalars(stmt).all()))


def prior_bad_run(db: Session, device_id: int, component: str, before: Optional[datetime], cap: int = 10) -> int:
    """在 `before` 之前, 该点位连续 bad 的条数 (从最近往回数)。"""
    stmt = select(Measurement.quality).where(
        Measurement.device_id == device_id, Measurement.component_name == component
    )
    if before is not None:
        stmt = stmt.where(Measurement.time < before)
    n = 0
    for q in db.scalars(stmt.order_by(Measurement.time.desc()).limit(cap)).all():
        if q != BAD:
            break
        n += 1
    return n


def assess_rows(db: Session, device: Device, rows: list[Measurement], *, realtime: bool) -> QualityReport:
    """给 `rows` (尚未写入数据库的 Measurement) 逐条定级, 就地修改 row.quality。

    必须在这些行被写库**之前**调用: 它要读取库里已有的历史作为基线, 不能把本批数据自己读进去。
    """
    report = QualityReport()
    if not rows:
        return report
    now = datetime.now(timezone.utc)

    tags = {
        t.component_name: t
        for t in db.scalars(select(DeviceComponent).where(DeviceComponent.device_id == device.id)).all()
    }

    by_comp: dict[str, list[Measurement]] = {}
    for r in rows:
        by_comp.setdefault(r.component_name, []).append(r)

    for comp, comp_rows in by_comp.items():
        comp_rows.sort(key=lambda r: _aware(r.time) or now)
        first_ts = _aware(comp_rows[0].time)
        tag = tags.get(comp)
        assessor = ComponentAssessor(
            low=float(tag.low_limit) if tag is not None and tag.low_limit is not None else None,
            high=float(tag.high_limit) if tag is not None and tag.high_limit is not None else None,
            tail=_load_tail(db, device.id, comp, first_ts),
            realtime=realtime,
            now=now,
        )
        run = 0
        n_bad = 0
        reason_counter: Counter = Counter()
        for r in comp_rows:
            a = assessor.assess(r.value, r.time, r.quality)
            r.quality = a.quality
            report.by_row[id(r)] = a
            report.counts[a.quality] += 1
            reason_counter.update(a.reasons)
            if a.quality == BAD:
                run += 1
                n_bad += 1
            else:
                run = 0
        report.trailing_bad[comp] = run
        report.all_bad[comp] = n_bad == len(comp_rows)
        report.reasons[comp] = reason_counter
    return report


# ---------------------------------------------------------------------------
# Acquisition-event rule check (industrial core, POST /industrial/acquisition-events).
# Independent of the reading assessor above: it validates ONE raw acquisition event and
# returns issue dicts that are stored as DataQualityEvent rows.
# ---------------------------------------------------------------------------
QUALITY_GOOD = "good"
QUALITY_CODES = {"good", "uncertain", "bad", "stale", "estimated", "manual"}

def validate_measurement(*, value: Any, quality: str, low: float | None = None, high: float | None = None,
                         source_timestamp: datetime | None = None, received_at: datetime | None = None) -> list[dict]:
    issues = []
    q = (quality or QUALITY_GOOD).lower()
    if q not in QUALITY_CODES:
        issues.append({"rule_code":"QUALITY_CODE_INVALID","severity":"error","details":{"quality":quality}})
    try:
        v = float(value)
        if low is not None and v < float(low):
            issues.append({"rule_code":"OUT_OF_RANGE_LOW","severity":"warning","details":{"value":v,"low":float(low)}})
        if high is not None and v > float(high):
            issues.append({"rule_code":"OUT_OF_RANGE_HIGH","severity":"warning","details":{"value":v,"high":float(high)}})
    except (TypeError, ValueError):
        issues.append({"rule_code":"VALUE_NOT_NUMERIC","severity":"error","details":{"value":str(value)}})
    if source_timestamp and received_at:
        delay = (received_at - source_timestamp).total_seconds()
        if delay > 300:
            issues.append({"rule_code":"STALE_SOURCE","severity":"warning","details":{"delay_seconds":delay}})
    return issues
