"""External data-source models — scraping a third-party instrument vendor's
cloud platform (login once, then a dozen-odd "databases" on the left-hand
menu, each downloadable as CSV/Excel/JSON) and reconciling the result
against continuous Measurement coverage (4 tables).

    厂商云平台(ExternalSource, 一套账号密码登录一次)
        -> 站内数据库(ExternalDatabase, 十几个，每个可单独手动/定时刷新)
            -> 每次抓取记录(ExternalSyncLog)
            -> 数据连续性巡检结果(DataContinuityGap) -> 报警 -> 维护工单

Downloaded rows are imported into the existing `measurements` table via
app/services/external_import.py (which reuses app/services/data_chain.py),
so once the data lands here it flows through the same TAG/报警/维护 chain
as any other import — this module's job is only getting the file off the
vendor's website and mapping its columns.
"""
from __future__ import annotations

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class ExternalSource(Base):
    """一个仪器厂商云平台站点的登录配置。十几个"数据库"共用同一套账号密码，
    登录一次后分别跳到各自的列表页下载 —— 这里只存"如何登录"，具体每个数据库
    "去哪个页面点哪个按钮"记在 ExternalDatabase 上。

    CSS 选择器（*_selector 字段）必须对照目标网站的真实登录页填写，站点结构
    不同这些值也不同；创建后可用 POST /external/sources/{id}/test-login 验证。
    """

    __tablename__ = "external_sources"

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False)
    base_url = Column(String(500), nullable=False)
    login_url = Column(String(500), nullable=False)
    username = Column(String(200), nullable=False)
    password_encrypted = Column(Text, nullable=False)

    username_selector = Column(String(300), nullable=False, default="input[name='username']")
    password_selector = Column(String(300), nullable=False, default="input[name='password']")
    submit_selector = Column(String(300), nullable=False, default="button[type='submit']")
    # 登录成功后页面上一定会出现的元素，用来判断是否登录成功（例如导航栏里的用户名、
    # 或者跳转后的首页某个 id）。留空则只能靠"登录页元素消失"做弱判断。
    login_success_selector = Column(String(300))

    is_enabled = Column(Boolean, nullable=False, default=True)
    status = Column(String(20), nullable=False, default="untested")  # untested/ok/login_failed/error
    last_login_at = Column(DateTime(timezone=True))
    last_error = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    databases = relationship(
        "ExternalDatabase", back_populates="source", cascade="all, delete-orphan"
    )


class ExternalDatabase(Base):
    """厂商站点左侧菜单里的一个具体数据库/数据表——对应一次
    "登录后跳到这个地址 -> (可选)设置查询条件 -> 点下载按钮 -> 拿到文件"。"""

    __tablename__ = "external_databases"

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("external_sources.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(150), nullable=False)  # 数据库名称，如"1号锅炉在线数据"
    list_url = Column(String(500), nullable=False)  # 登录后进入这个数据库的页面地址
    download_selector = Column(String(300), nullable=False, default="text=下载")  # 下载按钮的选择器

    # 有的站点下载前还要选时间范围、点查询按钮，可选，按顺序依次 click/fill
    pre_download_steps = Column(JSON)  # 例如 [{"action":"fill","selector":"#start","value":"2026-09-01"}, {"action":"click","selector":"#query"}]
    data_format = Column(String(10), nullable=False, default="csv")  # csv/xlsx/json

    # 下载文件的列名 -> 本系统字段的映射（导入 measurements 表用）
    device_code_column = Column(String(100))
    component_column = Column(String(100))
    time_column = Column(String(100))
    value_column = Column(String(100))
    # 如果这一个数据库整份文件只对应一台设备/一个组分，可以直接指定，不必按列识别
    default_device_code = Column(String(50))
    default_component_name = Column(String(100))

    auto_refresh_enabled = Column(Boolean, nullable=False, default=False)
    refresh_interval_minutes = Column(Integer)  # 空 = 只能手动点"刷新"
    expected_interval_minutes = Column(Integer, nullable=False, default=60)  # 期望采样间隔，用于连续性巡检

    is_enabled = Column(Boolean, nullable=False, default=True)
    last_synced_at = Column(DateTime(timezone=True))
    last_status = Column(String(20), nullable=False, default="never_run")  # never_run/running/success/failed
    last_error = Column(Text)
    last_rows_imported = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    source = relationship("ExternalSource", back_populates="databases")

    __table_args__ = (Index("idx_external_databases_source", "source_id"),)


class ExternalSyncLog(Base):
    """每一次抓取/导入的执行记录 —— 用来看数据下载的连续性、可维护性，
    以及失败排查（哪一步失败、报了什么错）。"""

    __tablename__ = "external_sync_logs"

    id = Column(Integer, primary_key=True)
    database_id = Column(Integer, ForeignKey("external_databases.id", ondelete="CASCADE"), nullable=False)
    triggered_by = Column(String(20), nullable=False, default="manual")  # manual/scheduled
    status = Column(String(20), nullable=False, default="running")  # running/success/failed
    file_name = Column(String(255))
    rows_downloaded = Column(Integer, nullable=False, default=0)
    rows_imported = Column(Integer, nullable=False, default=0)
    gaps_found = Column(Integer, nullable=False, default=0)
    error_message = Column(Text)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True))

    __table_args__ = (Index("idx_external_sync_logs_database", "database_id", "started_at"),)


class DataContinuityGap(Base):
    """数据连续性巡检发现的断点：某设备某组分在一段时间内没有新数据落库。
    超过阈值的断点会自动生成一条 Alarm（进而按现有报警->维护链条生成工单），
    这样"网站没数据了"能直接体现在实验室/维护模块里，而不是只停在这张表里。"""

    __tablename__ = "data_continuity_gaps"

    id = Column(Integer, primary_key=True)
    database_id = Column(Integer, ForeignKey("external_databases.id", ondelete="CASCADE"))
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    component_name = Column(String(100), nullable=False)
    gap_start = Column(DateTime(timezone=True), nullable=False)
    gap_end = Column(DateTime(timezone=True), nullable=False)
    gap_minutes = Column(Float, nullable=False)
    severity = Column(String(20), nullable=False, default="warning")  # info/warning/critical
    alarm_id = Column(Integer, ForeignKey("alarms.id", ondelete="SET NULL"))
    resolved = Column(Boolean, nullable=False, default=False)
    detected_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_data_continuity_gaps_device", "device_id", "component_name"),
        Index("idx_data_continuity_gaps_database", "database_id"),
    )
