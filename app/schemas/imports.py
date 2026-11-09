from typing import Optional

from pydantic import BaseModel


class TableImportResult(BaseModel):
    """Summary returned after a CSV/Excel table import on any module
    (Devices, Gateways, Assets, Work Orders, Lab Samples, Knowledge docs)."""

    file_name: str
    row_count: int  # data rows read from the file
    columns_detected: dict[str, str]  # our field name -> the column header we matched it to
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    status: str = "done"  # "done" | "failed"
    warnings: list[str] = []
