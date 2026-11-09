from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class MeasurementIn(BaseModel):
    time: Optional[datetime] = None
    device_id: int
    component_name: str
    value: float
    quality: str = "good"


class MeasurementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    time: datetime
    device_id: int
    component_name: str
    value: float
    quality: str


class MeasurementBatchIn(BaseModel):
    device_id: int
    readings: list[MeasurementIn]


class ImportResult(BaseModel):
    """Summary returned after a CSV/Excel file import (Data page 数据导入)."""

    import_id: int
    file_name: str
    device_id: int
    row_count: int  # number of source rows read from the file
    rows_inserted: int  # number of measurement points actually inserted
    columns_detected: list[str]  # component columns (wide) or [component_col, value_col] (long)
    time_column: str
    format: str  # "wide" | "long"
    status: str
    warnings: list[str] = []


class TrendPoint(BaseModel):
    time: datetime
    value: float


class TrendSeries(BaseModel):
    component_name: str
    points: list[TrendPoint]


class HistoryRow(BaseModel):
    """One row of Data > 历史数据: a measurement plus where it came from."""

    time: datetime
    device_id: int
    device_code: Optional[str] = None
    component_name: str
    value: float
    quality: str
    source: str  # "local" (手动导入/实时录入) | "cloud" (云数据同步)
    import_id: Optional[int] = None
    source_name: Optional[str] = None  # cloud: 外部数据库名称; local: 导入文件名


class HistoryPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[HistoryRow]


class ImportBatchOut(BaseModel):
    """Data > 导入记录: local uploads and cloud syncs in one list."""

    id: int
    file_name: str
    device_id: Optional[int] = None
    device_code: Optional[str] = None
    row_count: int
    column_count: int
    status: str
    source: str
    external_database_id: Optional[int] = None
    external_database_name: Optional[str] = None
    rows_stored: int = 0  # measurements actually stored under this batch
    created_at: Optional[datetime] = None
