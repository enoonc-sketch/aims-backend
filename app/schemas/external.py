from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------- Source --------
class ExternalSourceCreate(BaseModel):
    name: str
    base_url: str
    login_url: str
    username: str
    password: str = Field(..., description="明文传入，服务器端加密后存储，接口返回中不会再出现")
    username_selector: str = "input[name='username']"
    password_selector: str = "input[name='password']"
    submit_selector: str = "button[type='submit']"
    login_success_selector: Optional[str] = None
    is_enabled: bool = True


class ExternalSourceUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = Field(None, description="留空表示不修改密码")
    username_selector: Optional[str] = None
    password_selector: Optional[str] = None
    submit_selector: Optional[str] = None
    login_success_selector: Optional[str] = None
    is_enabled: Optional[bool] = None


class ExternalSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    base_url: str
    login_url: str
    username: str
    username_selector: str
    password_selector: str
    submit_selector: str
    login_success_selector: Optional[str] = None
    is_enabled: bool
    status: str
    last_login_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    # password_encrypted deliberately excluded


# -------------------------------------------------------- Database --------
class ExternalDatabaseCreate(BaseModel):
    source_id: int
    name: str
    list_url: str
    download_selector: str = "text=下载"
    pre_download_steps: Optional[list[dict[str, Any]]] = None
    data_format: str = "csv"
    device_code_column: Optional[str] = None
    component_column: Optional[str] = None
    time_column: Optional[str] = None
    value_column: Optional[str] = None
    default_device_code: Optional[str] = None
    default_component_name: Optional[str] = None
    auto_refresh_enabled: bool = False
    refresh_interval_minutes: Optional[int] = None
    expected_interval_minutes: int = 60
    is_enabled: bool = True


class ExternalDatabaseUpdate(BaseModel):
    name: Optional[str] = None
    list_url: Optional[str] = None
    download_selector: Optional[str] = None
    pre_download_steps: Optional[list[dict[str, Any]]] = None
    data_format: Optional[str] = None
    device_code_column: Optional[str] = None
    component_column: Optional[str] = None
    time_column: Optional[str] = None
    value_column: Optional[str] = None
    default_device_code: Optional[str] = None
    default_component_name: Optional[str] = None
    auto_refresh_enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = None
    expected_interval_minutes: Optional[int] = None
    is_enabled: Optional[bool] = None


class ExternalDatabaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_id: int
    name: str
    list_url: str
    download_selector: str
    pre_download_steps: Optional[list[dict[str, Any]]] = None
    data_format: str
    device_code_column: Optional[str] = None
    component_column: Optional[str] = None
    time_column: Optional[str] = None
    value_column: Optional[str] = None
    default_device_code: Optional[str] = None
    default_component_name: Optional[str] = None
    auto_refresh_enabled: bool
    refresh_interval_minutes: Optional[int] = None
    expected_interval_minutes: int
    is_enabled: bool
    last_synced_at: Optional[datetime] = None
    last_status: str
    last_error: Optional[str] = None
    last_rows_imported: int
    created_at: Optional[datetime] = None


# ------------------------------------------------------------- Sync -------
class ExternalSyncLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    database_id: int
    triggered_by: str
    status: str
    file_name: Optional[str] = None
    rows_downloaded: int
    rows_imported: int
    gaps_found: int
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class ExternalSyncResult(BaseModel):
    """Returned synchronously by the manual /refresh endpoint."""

    log: ExternalSyncLogOut
    rows_downloaded: int
    rows_imported: int
    gaps_found: int
    status: str
    error_message: Optional[str] = None


# --------------------------------------------------------------- Gap ------
class DataContinuityGapOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    database_id: Optional[int] = None
    device_id: int
    component_name: str
    gap_start: datetime
    gap_end: datetime
    gap_minutes: float
    severity: str
    alarm_id: Optional[int] = None
    resolved: bool
    detected_at: Optional[datetime] = None
