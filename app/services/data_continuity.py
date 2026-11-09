"""Detects gaps in Measurement coverage for a (device, component) pair —
"the vendor site stopped producing new data for this component for longer
than expected" — records them in DataContinuityGap, and raises an Alarm
(which flows into the existing 报警 -> 维护 chain,
app/services/alarms_hooks.py) once a gap is large enough to be
operationally meaningful, rather than ordinary sampling jitter.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.alarm import Alarm
from app.models.device import Device
from app.models.external_source import DataContinuityGap, ExternalDatabase
from app.models.measurement import Measurement
from app.services.alarms_hooks import maybe_create_work_order_for_alarm

# A gap must be at least this many multiples of the expected sampling
# interval before it counts as a real break rather than ordinary jitter.
_SEVERITY_THRESHOLDS = (  # (multiplier of expected interval, severity), checked largest-first
    (10.0, "critical"),
    (4.0, "warning"),
    (2.0, "info"),
)

# Bounds how far back each sync re-scans, so a database with years of history
# doesn't get rescanned end-to-end on every refresh.
DEFAULT_LOOKBACK_HOURS = 24 * 30


def check_continuity_for_database(
    db: Session,
    database: ExternalDatabase,
    device_ids: Iterable[int],
    *,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> int:
    """Scan recent Measurement history for every (device, component) pair
    touched by the most recent sync of `database`, and record any new gap
    larger than `expected_interval_minutes * 2`. Returns the number of new
    gaps found (existing gap records for the same span are not duplicated)."""
    expected = max(int(database.expected_interval_minutes or 60), 1)
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    found = 0
    for device_id in set(device_ids):
        components = db.scalars(
            select(Measurement.component_name)
            .where(Measurement.device_id == device_id, Measurement.time >= since)
            .distinct()
        ).all()
        for component_name in components:
            times = db.scalars(
                select(Measurement.time)
                .where(
                    Measurement.device_id == device_id,
                    Measurement.component_name == component_name,
                    Measurement.time >= since,
                )
                .order_by(Measurement.time.asc())
            ).all()

            for prev, curr in zip(times, times[1:]):
                gap_minutes = (curr - prev).total_seconds() / 60.0
                if gap_minutes < expected * 2.0:
                    continue

                already = db.scalar(
                    select(DataContinuityGap).where(
                        DataContinuityGap.device_id == device_id,
                        DataContinuityGap.component_name == component_name,
                        DataContinuityGap.gap_start == prev,
                        DataContinuityGap.gap_end == curr,
                    )
                )
                if already is not None:
                    continue

                severity = "info"
                for mult, sev in _SEVERITY_THRESHOLDS:
                    if gap_minutes >= expected * mult:
                        severity = sev
                        break

                record = DataContinuityGap(
                    database_id=database.id,
                    device_id=device_id,
                    component_name=component_name,
                    gap_start=prev,
                    gap_end=curr,
                    gap_minutes=gap_minutes,
                    severity=severity,
                )
                db.add(record)
                db.flush()
                found += 1

                if severity in ("warning", "critical"):
                    device = db.get(Device, device_id)
                    device_label = device.device_code if device else str(device_id)
                    alarm = Alarm(
                        device_id=device_id,
                        component_name=component_name,
                        severity=severity,
                        message=(
                            f"{device_label} {component_name} 数据断点 {gap_minutes:.0f} 分钟"
                            f"（{prev} ~ {curr}），来源数据库「{database.name}」"
                        ),
                        status="active",
                    )
                    db.add(alarm)
                    db.flush()
                    record.alarm_id = alarm.id
                    maybe_create_work_order_for_alarm(db, alarm)

    return found
