"""Template Center models — 模板中心与自定义模板系统（计划书第十二章）。

设计要点：
- 模板正文（template.json）整包以 JSON 存储，天然跨电脑迁移；.aimstpl 文件只是它的 ZIP 容器。
- 每次修改生成一条 TemplateVersion 历史 —— 旧版本永不覆盖，支持回退（12.7）。
- system 模板与用户模板同表存放，用 source 区分；system 模板禁止删除/改名（12.9-8）。
- 导入/使用全程留痕（TemplateImportLog / TemplateUsageLog），并写审计日志（12.9-9）。
"""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func

from app.db.base import Base


class TemplateRegistry(Base):
    """模板注册表：模板的"身份"信息；正文在 TemplateVersion.content。"""

    __tablename__ = "template_registry"

    id = Column(Integer, primary_key=True)
    # template_uid：跨电脑迁移时的稳定标识（导入去重就靠它）；系统模板固定，用户模板随机生成
    template_uid = Column(String(64), unique=True, nullable=False)
    name = Column(String(150), nullable=False)
    category = Column(String(50), nullable=False, index=True)   # 计划书 12.2 的 20 个分类
    # system=官方内置 / custom=用户在本机创建 / imported=从 .aimstpl 导入
    source = Column(String(20), nullable=False, default="custom", index=True)
    description = Column(Text)
    author = Column(String(100))
    tags = Column(JSON, default=list)          # 标签数组
    is_public = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    is_favorite = Column(Boolean, nullable=False, default=False)
    scripts_enabled = Column(Boolean, nullable=False, default=False)  # 12.9-2：脚本默认禁用，且全系统无执行点
    current_version = Column(Integer, nullable=False, default=1)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True))


    __table_args__ = (
        Index("uq_template_registry_name_source", "name", "source", unique=True),
    )


class TemplateVersion(Base):
    """模板版本历史：每次保存/导入/回退都追加一行，旧版本永不覆盖（12.7）。"""

    __tablename__ = "template_versions"

    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, ForeignKey("template_registry.id", ondelete="CASCADE"), nullable=False)
    version = Column(Integer, nullable=False)
    content = Column(JSON, nullable=False)          # template.json 正文
    manifest = Column(JSON)                         # 导入时的 manifest 快照（可空 = 本机创建）
    changelog = Column(String(300))
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_template_versions_tid_ver"),
    )



class TemplateImportLog(Base):
    """导入日志：成功/失败都记（12.5 第三步 & 12.9-9）。"""

    __tablename__ = "template_import_logs"

    id = Column(Integer, primary_key=True)
    filename = Column(String(255), nullable=False)
    template_name = Column(String(150))
    template_id = Column(Integer, ForeignKey("template_registry.id", ondelete="SET NULL"))
    status = Column(String(20), nullable=False)     # success / failed / preview
    reason = Column(Text)                           # 失败时的明确中文原因
    file_size = Column(Integer)
    file_sha256 = Column(String(64))
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TemplateUsageLog(Base):
    """使用记录：应用/回退/导出等动作留痕（12.11 + 12.9-9）。"""

    __tablename__ = "template_usage_logs"

    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, ForeignKey("template_registry.id", ondelete="CASCADE"), nullable=False)
    version = Column(Integer)
    action = Column(String(30), nullable=False)     # apply / restore / export / duplicate
    # 应用结果摘要：创建了哪些业务对象（如 "devices: GC-1001, GC-1002"）
    target_summary = Column(Text)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TemplateDependency(Base):
    """模板依赖关系（12.10）：第一版只做"导入时校验是否存在"，不做自动安装。"""

    __tablename__ = "template_dependencies"

    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, ForeignKey("template_registry.id", ondelete="CASCADE"), nullable=False)
    dep_type = Column(String(30), nullable=False)   # template / device_model / knowledge_base ...
    dep_key = Column(String(150), nullable=False)
    dep_version = Column(String(30))

    __table_args__ = (
        Index("uq_template_deps", "template_id", "dep_type", "dep_key", unique=True),
    )
