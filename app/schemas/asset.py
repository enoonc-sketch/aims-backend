from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class AssetBase(BaseModel):
    asset_code: str
    device_id: Optional[int] = None
    name: str
    category: Optional[str] = None
    purchase_date: Optional[date] = None
    purchase_value: Optional[float] = None
    depreciation_years: int = 10
    status: str = "in_use"              # in_use/idle/scrapped


class AssetCreate(AssetBase):
    pass


class AssetUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    current_value: Optional[float] = None


class AssetOut(AssetBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    current_value: Optional[float] = None
    created_at: datetime


class AssetROIResult(BaseModel):
    asset_id: int
    period_months: int
    purchase_value: float
    current_value: float
    straight_line_monthly_depreciation: float
    accumulated_depreciation: float
    net_book_value: float
    depreciation_pct: float


class AssetDocumentCreate(BaseModel):
    doc_name: str
    doc_type: Optional[str] = None      # manual/invoice/certificate
    file_path: Optional[str] = None
    uploaded_by: Optional[int] = None


class AssetDocumentOut(AssetDocumentCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    asset_id: int
    created_at: datetime
