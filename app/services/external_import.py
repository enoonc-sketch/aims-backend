"""Turns a downloaded external-database file (CSV/Excel/JSON) into rows in
the existing `measurements` table, going through the same
设备->TAG->报警->维护 pipeline every other import endpoint uses
(app/services/data_chain.py) — this module's only job is mapping the vendor
site's export columns onto (device_code, component_name, time, value).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.external_source import ExternalDatabase
from app.models.measurement import Measurement, MeasurementImport
from app.services import data_chain, table_import

_DEVICE_CODE_CANDIDATES = ("device_code", "设备编号", "设备号", "device", "设备")
_COMPONENT_CANDIDATES = ("component_name", "component", "组分", "参数", "measurement")
_TIME_CANDIDATES = ("time", "timestamp", "时间", "采集时间", "date")
_VALUE_CANDIDATES = ("value", "数值", "值", "reading")


@dataclass
class ImportStats:
    rows_downloaded: int = 0
    rows_imported: int = 0
    device_ids: set[int] = field(default_factory=set)
    import_id: Optional[int] = None  # MeasurementImport batch this sync wrote (数据管理里可追溯)
    warnings: list[str] = field(default_factory=list)


def _read_any(raw: bytes, filename: str, data_format: str):
    import pandas as pd

    fmt = (data_format or "").lower()
    if fmt == "json":
        import json

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Could not parse JSON file: {e}") from e
        if isinstance(data, dict):
            for key in ("data", "rows", "records", "items"):
                if key in data and isinstance(data[key], list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise ValueError(
                'Expected a JSON array of records (optionally wrapped in {"data": [...]})'
            )
        return pd.DataFrame(data)
    return table_import.read_table(raw, filename)


def import_database_file(db: Session, database: ExternalDatabase, file_path: Path) -> ImportStats:
    stats = ImportStats()

    raw = file_path.read_bytes()
    if not raw:
        stats.warnings.append("下载得到的文件为空")
        return stats

    df = _read_any(raw, file_path.name, database.data_format)
    stats.rows_downloaded = len(df)
    if df.empty:
        stats.warnings.append("文件中没有数据行")
        return stats

    columns = list(df.columns)
    device_col = (
        database.device_code_column
        if database.device_code_column in columns
        else table_import.pick_column(columns, _DEVICE_CODE_CANDIDATES)
    )
    comp_col = (
        database.component_column
        if database.component_column in columns
        else table_import.pick_column(columns, _COMPONENT_CANDIDATES)
    )
    time_col = (
        database.time_column
        if database.time_column in columns
        else table_import.pick_column(columns, _TIME_CANDIDATES)
    )
    value_col = (
        database.value_column
        if database.value_column in columns
        else table_import.pick_column(columns, _VALUE_CANDIDATES)
    )

    if time_col is None or value_col is None:
        raise ValueError(
            "无法识别时间列/数值列，请在数据库配置里显式指定 time_column / value_column"
            f"（文件列名：{columns}）"
        )
    if device_col is None and not database.default_device_code:
        raise ValueError(
            "无法识别设备编号列，且未配置 default_device_code —— "
            "如果这个数据库整份文件只对应一台设备，请在配置里填 default_device_code"
        )
    if comp_col is None and not database.default_component_name:
        raise ValueError(
            "无法识别组分/参数列，且未配置 default_component_name —— "
            "如果这个数据库整份文件只对应一个组分，请在配置里填 default_component_name"
        )

    # 云数据 -> 数据管理: 每次同步登记一个导入批次(source=cloud)，写入的测量点带 import_id，
    # 这样数据管理页的「历史数据 / 导入记录」能区分并回溯云端来源。
    batch = MeasurementImport(
        file_name=file_path.name,
        row_count=len(df),
        column_count=len(columns),
        status="processing",
        source="cloud",
        external_database_id=database.id,
    )
    db.add(batch)
    db.flush()
    stats.import_id = batch.id

    device_cache: dict[str, Optional[Device]] = {}
    # 先收集, 循环结束后按设备统一送过 data_chain.process_readings (质量评估 -> 报警 -> 诊断 -> 工单),
    # 而不是像以前那样每行自己 add + 自己判断阈值、且一律写 quality="good"。
    pending: dict[int, list[Measurement]] = {}
    devices_by_id: dict[int, Device] = {}
    pending_keys: set[tuple] = set()
    bad_device = 0
    bad_time = 0
    bad_value = 0

    for _, row in df.iterrows():
        device_code = table_import.clean_str(row[device_col]) if device_col else database.default_device_code
        component_name = (
            table_import.clean_str(row[comp_col]) if comp_col else database.default_component_name
        )
        ts = table_import.clean_datetime(row[time_col])
        value = table_import.clean_number(row[value_col])

        if ts is None:
            bad_time += 1
            continue
        if value is None:
            bad_value += 1
            continue
        if not device_code or not component_name:
            bad_device += 1
            continue

        if device_code not in device_cache:
            device_cache[device_code] = db.scalar(
                select(Device).where(Device.device_code == device_code)
            )
        device = device_cache[device_code]
        if device is None:
            bad_device += 1
            continue

        stats.device_ids.add(device.id)

        key = (device.id, component_name, ts)
        if key in pending_keys:
            continue
        exists = db.scalar(
            select(Measurement).where(
                Measurement.time == ts,
                Measurement.device_id == device.id,
                Measurement.component_name == component_name,
            )
        )
        if exists is not None:
            continue

        pending_keys.add(key)
        devices_by_id[device.id] = device
        pending.setdefault(device.id, []).append(
            Measurement(
                time=ts, device_id=device.id, component_name=component_name, value=value, quality="good",
                import_id=batch.id,
            )
        )
        stats.rows_imported += 1

    for device_id, rows in pending.items():
        # 云端下载的是一批历史/近期文件, 不是网关实时推送: realtime=False (不做"时间戳过旧"判断)
        data_chain.process_readings(db, devices_by_id[device_id], rows, realtime=False)

    if bad_device:
        stats.warnings.append(f"{bad_device} 行的设备编号无法识别或在系统中不存在，已跳过（需先在设备管理里创建对应设备）")
    if bad_time:
        stats.warnings.append(f"{bad_time} 行时间戳无法解析，已跳过")
    if bad_value:
        stats.warnings.append(f"{bad_value} 行数值无法解析，已跳过")

    if len(stats.device_ids) == 1:
        batch.device_id = next(iter(stats.device_ids))
    batch.status = "done"
    db.flush()
    return stats
