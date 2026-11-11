"""Template Center API schemas（计划书 12.4/12.5/12.7 的接口载荷）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    template_uid: str
    name: str
    category: str
    source: str
    description: str | None = None
    author: str | None = None
    tags: list | None = None
    is_public: bool
    is_default: bool
    is_favorite: bool
    scripts_enabled: bool
    current_version: int
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_used_at: datetime | None = None


class TemplateDetailOut(TemplateOut):
    content: dict[str, Any]
    versions: list[int]
    readme: str | None = None


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    category: str = Field(min_length=1, max_length=50)
    description: str | None = None
    author: str | None = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list)
    is_public: bool = True
    is_default: bool = False
    content: dict[str, Any]


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=150)
    category: str | None = Field(default=None, max_length=50)
    description: str | None = None
    author: str | None = Field(default=None, max_length=100)
    tags: list[str] | None = None
    is_public: bool | None = None
    is_default: bool | None = None
    # 提供了 content 就追加新版本（12.7：旧版本永不覆盖）
    content: dict[str, Any] | None = None
    changelog: str | None = Field(default=None, max_length=300)


class TemplateApply(BaseModel):
    device_id: int | None = None
    device_code: str | None = None
    count: int | None = Field(default=None, ge=1, le=200)
    start_index: int | None = Field(default=None, ge=1)


class ImportPreviewOut(BaseModel):
    ok: bool
    filename: str
    manifest: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    conflicts: list[dict[str, Any]] | None = None
    missing_dependencies: list[str] | None = None
    scripts: dict[str, Any] | None = None
    sha256: str | None = None
    size: int | None = None
    error: str | None = None
