"""模板中心业务逻辑 — 创建/导入/应用/版本管理（计划书 12.5–12.8）。

应用引擎（12.6）：模板必须真正落到业务数据，不是"存个文件"。
- 设备类（仪表/分析仪/色谱/台账）→ 创建 Device + DeviceComponent
- TAG 点位 → 创建 DeviceTag
- 通信配置 → 创建 Gateway（+ 连接）
- 报警配置 → 创建 AlarmRule（走正常权限，不绕过任何安全限制）
- 资产管理 → 创建 Asset
- 维护管理 → 创建 MaintenanceSOP
- RAG 知识库 → 创建 KnowledgeDocument
- 其余分类（图表/工作流/数据库查询等）→ 明确返回"该分类暂不支持自动应用"，
  绝不假成功（计划书执行要求 4：不允许用假成功状态代替真实功能）。

应用产生的都是**新增**业务数据；模板删除不会触碰它们（12.9-8）。
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func as sa_func, select
from sqlalchemy.orm import Session

from app.deploy import APP_VERSION
from app.models.alarm import AlarmRule
from app.models.device import Device, DeviceComponent, DeviceModel, Gateway
from app.models.industrial import DeviceTag
from app.models.knowledge import KnowledgeDocument
from app.models.maintenance import Asset, MaintenanceSOP
from app.services.audit import record_audit as _record_audit
from app.models.template import (
    TemplateDependency,
    TemplateImportLog,
    TemplateRegistry,
    TemplateUsageLog,
    TemplateVersion,
)
from app.services.template_package import TemplatePackage, TemplatePackageError

logger = logging.getLogger("aims.templates")

# 计划书 12.2 的统一分类（顺序即前端左侧分类栏顺序）
CATEGORIES: list[str] = [
    "仪表管理", "在线分析仪", "气相色谱仪", "设备台账", "TAG 点位", "通信配置",
    "报警配置", "维护管理", "资产管理", "实验室数据", "数据导入", "数据导出",
    "报表", "图表", "AI 分析", "RAG 知识库", "设备诊断", "数据库查询",
    "工作流程", "系统配置",
]

# 每个分类的应用方式：apply-handler 或 None（暂不支持自动应用，见模块 docstring）
_APPLICABLE = {
    "仪表管理": "devices", "在线分析仪": "devices", "气相色谱仪": "devices",
    "设备台账": "devices", "TAG 点位": "tags", "通信配置": "gateways",
    "报警配置": "alarm_rules", "资产管理": "assets", "维护管理": "sop",
    "RAG 知识库": "knowledge",
}


class TemplateError(Exception):
    """业务级模板错误，message 直接展示给用户。"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_uid() -> str:
    return "tpl-" + secrets.token_hex(8)


def _require_str(payload: dict, key: str, where: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TemplateError(f"{where} 缺少必需字段：{key}")
    return value.strip()


# --------------------------------------------------------------------------- #
# 内置官方模板（12.2-A）
# --------------------------------------------------------------------------- #
BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    {
        "uid": "aims.builtin.gauge-management", "name": "仪表管理模板", "category": "仪表管理",
        "description": "标准仪表台账结构：含常规在线仪表的组件、量程与报rtu上下限示例。",
        "content": {
            "devices": [{
                "device_code": "GAUGE-{n}", "name": "在线仪表 {n}", "protocol": "HART",
                "location": "现场仪表间",
                "components": [
                    {"component_name": "主测量值", "unit": "%", "low_limit": 0, "high_limit": 100},
                    {"component_name": "温度补偿", "unit": "℃", "low_limit": -20, "high_limit": 80},
                ],
            }],
            "count": 3,
        },
    },
    {
        "uid": "aims.builtin.analyzer-online", "name": "在线分析仪管理模板", "category": "在线分析仪",
        "description": "在线分析仪器标准配置：Modbus TCP 协议、成分类测点。",
        "content": {
            "devices": [{
                "device_code": "PGA-{n}", "name": "在线分析仪 {n}", "protocol": "Modbus TCP",
                "ip_address": "192.168.1.{n}",
                "components": [
                    {"component_name": "O2", "unit": "%", "low_limit": 0, "high_limit": 25},
                    {"component_name": "CO", "unit": "ppm", "low_limit": 0, "high_limit": 200},
                    {"component_name": "NOx", "unit": "ppm", "low_limit": 0, "high_limit": 150},
                ],
            }],
            "count": 2,
        },
    },
    {
        "uid": "aims.builtin.gc-management", "name": "气相色谱仪管理模板", "category": "气相色谱仪",
        "description": "气相色谱仪（GC）标准测点：载气、柱箱、检测器与成分类通道。",
        "content": {
            "devices": [{
                "device_code": "GC-{n}", "name": "气相色谱仪 {n}", "protocol": "OPC UA",
                "components": [
                    {"component_name": "柱箱温度", "unit": "℃", "low_limit": 20, "high_limit": 250},
                    {"component_name": "检测器温度", "unit": "℃", "low_limit": 100, "high_limit": 300},
                    {"component_name": "载气压力", "unit": "kPa", "low_limit": 200, "high_limit": 400},
                    {"component_name": "CH4", "unit": "%", "low_limit": 0, "high_limit": 100},
                ],
            }],
            "count": 2,
        },
    },
    {
        "uid": "aims.builtin.device-ledger", "name": "设备台账模板", "category": "设备台账",
        "description": "通用设备台账批量建档结构。",
        "content": {
            "devices": [{
                "device_code": "DEV-{n}", "name": "设备 {n}", "protocol": "Modbus TCP",
                "location": "一车间",
                "components": [{"component_name": "运行状态", "unit": "-"}],
            }],
            "count": 5,
        },
    },
    {
        "uid": "aims.builtin.tag-points", "name": "TAG 点位模板", "category": "TAG 点位",
        "description": "为已有设备批量创建标准 TAG 点位（液位/温度/压力/流量）。",
        "content": {
            "tags": [
                {"tag_code": "LT-{n}", "name": "液位 {n}", "data_type": "float", "unit": "m"},
                {"tag_code": "TT-{n}", "name": "温度 {n}", "data_type": "float", "unit": "℃"},
                {"tag_code": "PT-{n}", "name": "压力 {n}", "data_type": "float", "unit": "MPa"},
                {"tag_code": "FT-{n}", "name": "流量 {n}", "data_type": "float", "unit": "m3/h"},
            ],
        },
    },
    {
        "uid": "aims.builtin.comm-config", "name": "通信配置模板", "category": "通信配置",
        "description": "标准 MQTT 网关 + Modbus 连接配置。",
        "content": {
            "gateways": [{
                "gateway_code": "GW-{n}", "name": "采集网关 {n}", "protocol": "MQTT",
                "ip_address": "192.168.1.10", "port": 1883,
            }],
            "count": 1,
        },
    },
    {
        "uid": "aims.builtin.alarm-rules", "name": "报警配置模板", "category": "报警配置",
        "description": "常用阈值报警规则集：超上限 warning / 严重超标 critical。",
        "content": {
            "alarm_rules": [
                {"rule_type": "threshold", "severity": "warning", "match_component": "*",
                 "high_limit_delta": 10},
                {"rule_type": "threshold", "severity": "critical", "match_component": "*",
                 "high_limit_delta": 30},
            ],
        },
    },
    {
        "uid": "aims.builtin.maintenance-sop", "name": "维护管理模板", "category": "维护管理",
        "description": "仪器例行维护 SOP 集（日检/周检/校准）。",
        "content": {
            "sops": [
                {"sop_type": "daily", "title": "仪器日检 SOP",
                 "content": "1. 检查样气压力；2. 查看报警列表；3. 记录运行状态。"},
                {"sop_type": "weekly", "title": "仪器周检 SOP",
                 "content": "1. 零点/量程核查；2. 清洁取样探头；3. 检查载气余量。"},
                {"sop_type": "calibration", "title": "标气校准 SOP",
                 "content": "1. 通入零气；2. 通入量程标气；3. 记录校准曲线偏差。"},
            ],
        },
    },
    {
        "uid": "aims.builtin.asset-ledger", "name": "资产管理模板", "category": "资产管理",
        "description": "标准资产台账结构（折旧年限 10 年）。",
        "content": {
            "assets": [{
                "asset_code": "AST-{n}", "name": "分析仪器资产 {n}", "category": "仪器仪表",
                "depreciation_years": 10, "status": "in_use",
            }],
            "count": 3,
        },
    },
    {
        "uid": "aims.builtin.lab-data", "name": "实验室数据模板", "category": "实验室数据",
        "description": "化验数据与在线数据对照的采样记录结构（JSON 结构定义，供化验室参照）。",
        "content": {"lab_sample_fields": [
            {"field": "sample_code", "label": "样品编号", "required": True},
            {"field": "sampled_at", "label": "采样时间", "required": True},
            {"field": "component_name", "label": "组分", "required": True},
            {"field": "lab_value", "label": "化验值", "required": True},
            {"field": "unit", "label": "单位", "required": False},
        ]},
    },
    {
        "uid": "aims.builtin.data-import", "name": "数据导入模板", "category": "数据导入",
        "description": "历史数据 Excel 批量导入的列映射约定（时间/设备/组分/数值）。",
        "content": {"column_mapping": {
            "时间": "timestamp", "设备编号": "device_code", "组分": "component_name",
            "数值": "value", "单位": "unit",
        }},
    },
    {
        "uid": "aims.builtin.data-export", "name": "数据导出模板", "category": "数据导出",
        "description": "标准日报/月报导出字段集。",
        "content": {"export_fields": ["timestamp", "device_code", "component_name", "value", "unit"]},
    },
    {
        "uid": "aims.builtin.report-daily", "name": "报表模板", "category": "报表",
        "description": "设备运行日报结构（JSON 报表定义）。",
        "content": {"report": {
            "title": "设备运行日报", "period": "daily",
            "sections": ["汇总统计", "报警明细", "数据完整性"],
            "fields": ["device_code", "avg_value", "max_value", "min_value", "alarm_count"],
        }},
    },
    {
        "uid": "aims.builtin.chart-trend", "name": "图表模板", "category": "图表",
        "description": "趋势图配置（时间序列，多组分叠加）。",
        "content": {"chart": {
            "type": "line", "time_range_hours": 24,
            "series": [{"source": "measurements", "group_by": "component_name"}],
        }},
    },
    {
        "uid": "aims.builtin.ai-analysis", "name": "AI 分析模板", "category": "AI 分析",
        "description": "设备异常分析任务的提示词与输入/输出约定（供 AI 中心调用）。",
        "content": {"ai_analysis": {
            "task": "设备异常分析",
            "prompt": "基于以下设备近期测量数据与报警记录，分析可能的故障原因并给出维护建议。",
            "inputs": ["device_code", "recent_measurements", "recent_alarms"],
            "output_format": {"结论": "text", "可能原因": "list", "建议措施": "list"},
        }},
    },
    {
        "uid": "aims.builtin.rag-knowledge", "name": "RAG 知识库模板", "category": "RAG 知识库",
        "description": "仪器维护知识库文档标准结构，应用后创建一篇可检索的知识文档。",
        "content": {"knowledge": {
            "title": "在线分析仪维护知识（模板生成）",
            "doc_type": "manual",
            "content": "在线分析仪日常维护要点：\n1. 保持取样系统气密性；\n2. 定期核查零点与量程；\n3. 探头堵塞时使用配套工具清理；\n4. 长期停机前用零气吹扫管路。",
        }},
    },
    {
        "uid": "aims.builtin.diagnosis", "name": "设备诊断模板", "category": "设备诊断",
        "description": "常见故障现象 → 诊断思路对照表。",
        "content": {"diagnosis": [
            {"symptom": "测量值长时间不变", "checks": ["取样管路堵塞", "传感器漂移", "通信中断"]},
            {"symptom": "测量值剧烈波动", "checks": ["样气压力不稳", "电磁干扰", "接地不良"]},
        ]},
    },
    {
        "uid": "aims.builtin.db-query", "name": "数据库查询模板", "category": "数据库查询",
        "description": "常用查询口径定义（只保存查询意图，不做任意 SQL 执行）。",
        "content": {"saved_query": {
            "name": "近24小时报警统计",
            "resource": "alarms", "filters": {"since_hours": 24}, "group_by": "severity",
        }},
    },
    {
        "uid": "aims.builtin.workflow", "name": "工作流程模板", "category": "工作流程",
        "description": "报警处置工作流（发现→确认→处理→验证→关闭）。",
        "content": {"workflow": {
            "name": "报警处置流程",
            "steps": ["报警确认", "原因定位", "现场处理", "复测验证", "关闭归档"],
        }},
    },
    {
        "uid": "aims.builtin.system-config", "name": "系统配置模板", "category": "系统配置",
        "description": "新装站点推荐系统参数（JSON，仅供参考与一键对照）。",
        "content": {"system_config": {
            "ACCESS_TOKEN_EXPIRE_MINUTES": 60, "MAX_UPLOAD_BYTES_MB": 64,
            "AI_LOCAL_LLM_ENABLED": True,
        }},
    },
]


def seed_builtin_templates(db: Session) -> int:
    """把官方内置模板落库（幂等：按 template_uid 判断，绝不覆盖已有内容）。"""
    created = 0
    for spec in BUILTIN_TEMPLATES:
        exists = db.scalar(select(TemplateRegistry).where(TemplateRegistry.template_uid == spec["uid"]))
        if exists:
            continue
        row = TemplateRegistry(
            template_uid=spec["uid"], name=spec["name"], category=spec["category"],
            source="system", description=spec.get("description"), author="AIMS 官方",
            tags=["内置"], is_public=True, current_version=1,
        )
        db.add(row)
        db.flush()
        db.add(TemplateVersion(template_id=row.id, version=1, content=spec["content"],
                               manifest={"uid": spec["uid"], "name": spec["name"],
                                         "category": spec["category"], "version": "1.0.0",
                                         "min_aims_version": APP_VERSION, "author": "AIMS 官方"}))
        created += 1
    if created:
        db.commit()
        logger.info("内置模板种子完成：新增 %d 个", created)
    return created


# --------------------------------------------------------------------------- #
# 版本管理（12.7）
# --------------------------------------------------------------------------- #
def _next_version(db: Session, template_id: int) -> int:
    top = db.scalar(select(sa_func.max(TemplateVersion.version)).where(TemplateVersion.template_id == template_id))
    return int(top or 0) + 1


def add_version(db: Session, tpl: TemplateRegistry, content: dict, *, manifest: dict | None = None,
                changelog: str | None = None, user_id: int | None = None) -> int:
    """追加一个新版本并把模板指向它。旧版本永不覆盖。"""
    version = _next_version(db, tpl.id)
    db.add(TemplateVersion(template_id=tpl.id, version=version, content=content,
                           manifest=manifest, changelog=changelog, created_by=user_id))
    tpl.current_version = version
    tpl.updated_at = now()
    return version


def get_version_content(db: Session, tpl: TemplateRegistry, version: int | None = None) -> dict:
    v = version if version is not None else tpl.current_version
    row = db.scalar(select(TemplateVersion).where(
        TemplateVersion.template_id == tpl.id, TemplateVersion.version == v))
    if row is None:
        raise TemplateError(f"版本 v{v} 不存在（模板 {tpl.name}）。")
    return dict(row.content or {})


def diff_versions(a: dict, b: dict, *, prefix: str = "") -> list[dict]:
    """字段级 JSON diff（版本比较用）。返回 [{path, old, new}]。"""
    out: list[dict] = []
    for key in sorted(set(a) | set(b)):
        path = f"{prefix}.{key}" if prefix else key
        va, vb = a.get(key, "<无>"), b.get(key, "<无>")
        if isinstance(va, dict) and isinstance(vb, dict):
            out.extend(diff_versions(va, vb, prefix=path))
        elif va != vb:
            out.append({"path": path, "old": va, "new": vb})
    return out


# --------------------------------------------------------------------------- #
# 导入（12.5）
# --------------------------------------------------------------------------- #
def preview_import(db: Session, pkg: TemplatePackage) -> dict:
    """导入第三/四步：安全检查已完成（package 层），这里做业务冲突与依赖检查。"""
    manifest = pkg.manifest
    existing = db.scalar(select(TemplateRegistry).where(TemplateRegistry.template_uid == manifest["uid"]))
    conflicts = []
    if existing:
        conflicts.append({
            "type": "uid_exists",
            "detail": f"已存在同 UID 模板「{existing.name}」（v{existing.current_version}），"
                      f"导入将追加为新版本 v{existing.current_version + 1}，旧版本保留。",
        })
    same_name = db.scalar(select(TemplateRegistry).where(
        TemplateRegistry.name == manifest["name"], TemplateRegistry.source == "imported"))
    if same_name and not existing:
        conflicts.append({"type": "name_exists",
                          "detail": f"已存在同名导入模板「{same_name.name}」，导入后两者并存。"})
    missing_deps: list[str] = []
    for dep in manifest.get("dependencies") or []:
        dep_key = str(dep.get("key") or dep if isinstance(dep, str) else dep.get("key") or "")
        if not dep_key:
            continue
        row = db.scalar(select(TemplateRegistry).where(TemplateRegistry.template_uid == dep_key))
        if not row:
            missing_deps.append(dep_key)
    return {
        "manifest": manifest,
        "summary": _content_summary(pkg.template),
        "conflicts": conflicts,
        "missing_dependencies": missing_deps,
        "compatible": True,
        "scripts": {"count": len(pkg.scripts), "enabled": False,
                    "note": "脚本文件已随包保存但默认禁用，系统内无任何执行入口（12.9-2）"},
        "sha256": pkg.sha256,
        "size": pkg.size,
    }


def _content_summary(template: dict) -> dict:
    return {
        "devices": len(template.get("devices") or []),
        "tags": len(template.get("tags") or []),
        "gateways": len(template.get("gateways") or []),
        "alarm_rules": len(template.get("alarm_rules") or []),
        "assets": len(template.get("assets") or []),
        "sops": len(template.get("sops") or []),
        "knowledge": 1 if template.get("knowledge") else 0,
        "其他键": sorted(k for k in template if k not in
                        {"devices", "tags", "gateways", "alarm_rules", "assets", "sops", "knowledge"}),
    }


def confirm_import(db: Session, pkg: TemplatePackage, *, filename: str, user: Any) -> TemplateRegistry:
    """导入第五步：真正入库。同 UID 已存在 → 追加版本；否则新建 imported 模板。"""
    manifest = pkg.manifest
    tpl = db.scalar(select(TemplateRegistry).where(TemplateRegistry.template_uid == manifest["uid"]))
    try:
        if tpl:
            add_version(db, tpl, pkg.template, manifest=manifest,
                        changelog=f"导入覆盖：包版本 {manifest.get('version')}", user_id=user.id)
            tpl.description = manifest.get("description") or tpl.description
            tpl.author = manifest.get("author") or tpl.author
        else:
            tpl = TemplateRegistry(
                template_uid=manifest["uid"], name=str(manifest["name"])[:150],
                category=str(manifest["category"])[:50], source="imported",
                description=manifest.get("description"), author=manifest.get("author"),
                tags=manifest.get("tags") or [], is_public=bool(manifest.get("is_public", True)),
                created_by=user.id, current_version=1,
            )
            db.add(tpl)
            db.flush()
            db.add(TemplateVersion(template_id=tpl.id, version=1, content=pkg.template,
                                   manifest=manifest, created_by=user.id))
        # 依赖登记（12.10）
        for dep in manifest.get("dependencies") or []:
            if isinstance(dep, str):
                dep = {"type": "template", "key": dep}
            if isinstance(dep, dict) and dep.get("key"):
                db.add(TemplateDependency(template_id=tpl.id, dep_type=str(dep.get("type", "template"))[:30],
                                          dep_key=str(dep["key"])[:150], dep_version=dep.get("version")))
        db.add(TemplateImportLog(filename=filename[:255], template_name=tpl.name, template_id=tpl.id,
                                 status="success", file_size=pkg.size, file_sha256=pkg.sha256,
                                 created_by=user.id))
        record_audit(db, user, "TEMPLATE_IMPORT", "templates", tpl.id, f"导入模板「{tpl.name}」")
        db.commit()
        db.refresh(tpl)
        return tpl
    except Exception as exc:
        db.rollback()
        db.add(TemplateImportLog(filename=filename[:255], template_name=str(manifest.get("name") or ""),
                                 status="failed", reason=str(exc)[:900],
                                 file_size=pkg.size, file_sha256=pkg.sha256, created_by=user.id))
        db.commit()
        raise


def record_import_failure(db: Session, *, filename: str, reason: str, size: int, user: Any) -> None:
    """解析阶段就失败：也必须留痕（12.9-9），失败原因给到用户（12.5）。"""
    try:
        db.add(TemplateImportLog(filename=filename[:255], status="failed", reason=reason[:900],
                                 file_size=size, created_by=getattr(user, "id", None)))
        db.commit()
    except Exception:  # noqa: BLE001 — 留痕失败不能掩盖原始错误
        db.rollback()


# --------------------------------------------------------------------------- #
# 应用（12.6）
# --------------------------------------------------------------------------- #
def _expand(template: dict, key: str, count: int) -> list[dict]:
    items = template.get(key) or []
    return [dict(x) for x in items if isinstance(x, dict)] * max(1, count)


def _substitute(text: str, n: int) -> str:
    return text.replace("{n}", str(n))


def apply_template(db: Session, tpl: TemplateRegistry, params: dict, *, user: Any) -> dict:
    """应用模板 → 生成真实业务数据。返回创建摘要；不支持自动应用的分类明确拒绝。"""
    if tpl.category not in _APPLICABLE:
        raise TemplateError(
            f"「{tpl.category}」类模板暂不支持自动应用。该模板可作为配置/结构定义导出使用；"
            f"如需扩展应用能力，请在需求评审后按计划书 12.6 增加对应处理器。")
    content = get_version_content(db, tpl)
    kind = _APPLICABLE[tpl.category]
    summary: dict[str, Any] = {"category": tpl.category, "created": {}}

    if kind == "devices":
        created = _apply_devices(db, content, params)
        summary["created"]["devices"] = [d.device_code for d in created["devices"]]
        summary["created"]["components"] = created["components"]
    elif kind == "tags":
        summary["created"]["device_tags"] = _apply_tags(db, content, params)
    elif kind == "gateways":
        summary["created"]["gateways"] = _apply_gateways(db, content)
    elif kind == "alarm_rules":
        summary["created"]["alarm_rules"] = _apply_alarm_rules(db, content, params)
    elif kind == "assets":
        summary["created"]["assets"] = _apply_assets(db, content)
    elif kind == "sop":
        summary["created"]["sops"] = _apply_sops(db, content)
    elif kind == "knowledge":
        summary["created"]["knowledge_documents"] = _apply_knowledge(db, content, tpl, user)

    tpl.last_used_at = now()
    db.add(TemplateUsageLog(template_id=tpl.id, version=tpl.current_version, action="apply",
                            target_summary=str(summary["created"])[:900], created_by=user.id))
    record_audit(db, user, "TEMPLATE_APPLY", "templates", tpl.id,
                 f"应用模板「{tpl.name}」：{summary['created']}")
    db.commit()
    return summary


def _pick_device(db: Session, params: dict) -> Device:
    """报警/TAG 模板应用时的目标设备：显式指定或取第一台在线设备。"""
    device_id = params.get("device_id")
    device_code = params.get("device_code")
    device = None
    if device_id:
        device = db.get(Device, int(device_id))
    elif device_code:
        device = db.scalar(select(Device).where(Device.device_code == str(device_code)))
    if device is None:
        device = db.scalar(select(Device).order_by(Device.id).limit(1))
    if device is None:
        raise TemplateError("系统中没有任何设备：请先创建设备，或在应用参数中指定 device_id。")
    return device


def _apply_devices(db: Session, content: dict, params: dict) -> dict:
    """设备类模板：devices[].device_code 支持 {n} 序号占位，逐台建档。"""
    start = int(params.get("start_index") or 1)
    specs = [dict(x) for x in (content.get("devices") or []) if isinstance(x, dict)]
    count = int(params.get("count") or content.get("count") or 1)
    created_devices, component_count = [], 0
    for spec in specs:
        for n in range(start, start + count):
            code = _substitute(str(spec.get("device_code") or ""), n)
            name = _substitute(str(spec.get("name") or code), n)
            if not code:
                raise TemplateError("设备定义缺少 device_code。")
            if db.scalar(select(Device).where(Device.device_code == code)):
                logger.info("设备 %s 已存在，跳过（模板应用是幂等的新增，不覆盖业务数据）", code)
                continue
            model = None
            model_name = spec.get("model_name")
            if model_name:
                model = db.scalar(select(DeviceModel).where(DeviceModel.model_name == str(model_name)))
                if model is None:
                    model = DeviceModel(model_name=str(model_name)[:100],
                                        manufacturer=spec.get("manufacturer"))
                    db.add(model)
                    db.flush()
            device = Device(device_code=code[:50], name=name[:150], model_id=model.id if model else None,
                            protocol=str(spec.get("protocol") or "Modbus TCP")[:30],
                            location=spec.get("location"), ip_address=spec.get("ip_address"))
            db.add(device)
            db.flush()
            created_devices.append(device)
            for comp in spec.get("components") or []:
                if not isinstance(comp, dict) or not comp.get("component_name"):
                    continue
                db.add(DeviceComponent(
                    device_id=device.id, component_name=str(comp["component_name"])[:100],
                    unit=str(comp.get("unit") or "%")[:20],
                    is_major=bool(comp.get("is_major", True)),
                    low_limit=comp.get("low_limit"), high_limit=comp.get("high_limit"),
                    tag_address=comp.get("tag_address")))
                component_count += 1
    if not created_devices:
        return {"devices": [], "components": 0}
    return {"devices": created_devices, "components": component_count}


def _apply_tags(db: Session, content: dict, params: dict) -> list[str]:
    device = _pick_device(db, params)
    created = []
    for spec in [x for x in (content.get("tags") or []) if isinstance(x, dict)]:
        code = _substitute(str(spec.get("tag_code") or ""), device.id)
        if not code:
            raise TemplateError("TAG 定义缺少 tag_code。")
        if db.scalar(select(DeviceTag).where(DeviceTag.device_id == device.id, DeviceTag.tag_code == code)):
            continue
        db.add(DeviceTag(device_id=device.id, tag_code=code[:100],
                         name=_substitute(str(spec.get("name") or code), device.id)[:150],
                         data_type=str(spec.get("data_type") or "float")[:30],
                         unit=spec.get("unit"), description=spec.get("description")))
        created.append(f"{device.device_code}/{code}")
    return created


def _apply_gateways(db: Session, content: dict) -> list[str]:
    created = []
    for spec in [x for x in (content.get("gateways") or []) if isinstance(x, dict)]:
        code = str(spec.get("gateway_code") or "").strip()
        if not code:
            raise TemplateError("网关定义缺少 gateway_code。")
        if db.scalar(select(Gateway).where(Gateway.gateway_code == code)):
            continue
        db.add(Gateway(gateway_code=code[:50], name=str(spec.get("name") or code)[:150],
                       ip_address=spec.get("ip_address"), port=spec.get("port"),
                       protocol=str(spec.get("protocol") or "MQTT")[:30]))
        created.append(code)
    return created


def _apply_alarm_rules(db: Session, content: dict, params: dict) -> list[str]:
    """报警规则模板：基于目标设备现有测点上限生成阈值规则；不绕过任何权限检查。"""
    device = _pick_device(db, params)
    comps = list(db.scalars(select(DeviceComponent).where(DeviceComponent.device_id == device.id)))
    created = []
    rules = [x for x in (content.get("alarm_rules") or []) if isinstance(x, dict)]
    for rule in rules:
        for comp in comps:
            if rule.get("match_component") not in (None, "*", comp.component_name):
                continue
            base_high = float(comp.high_limit) if comp.high_limit is not None else None
            if base_high is None and rule.get("high_limit") is None:
                continue  # 没有可依据的阈值，宁可不建规则也不建一条永远不触发的
            high = float(rule["high_limit"]) if rule.get("high_limit") is not None else \
                (base_high + float(rule.get("high_limit_delta") or 0))
            exists = db.scalar(select(AlarmRule).where(
                AlarmRule.device_id == device.id, AlarmRule.component_name == comp.component_name,
                AlarmRule.severity == str(rule.get("severity") or "warning")))
            if exists:
                continue
            db.add(AlarmRule(device_id=device.id, component_name=comp.component_name,
                             rule_type=str(rule.get("rule_type") or "threshold")[:30],
                             high_limit=high,
                             severity=str(rule.get("severity") or "warning")[:20]))
            created.append(f"{device.device_code}/{comp.component_name}/{rule.get('severity')}")
    return created


def _apply_assets(db: Session, content: dict) -> list[str]:
    created = []
    for spec in [x for x in (content.get("assets") or []) if isinstance(x, dict)]:
        code = str(spec.get("asset_code") or "").strip()
        if not code:
            raise TemplateError("资产定义缺少 asset_code。")
        if db.scalar(select(Asset).where(Asset.asset_code == code)):
            continue
        db.add(Asset(asset_code=code[:50], name=str(spec.get("name") or code)[:150],
                     category=spec.get("category"), status=str(spec.get("status") or "in_use")[:20],
                     depreciation_years=spec.get("depreciation_years", 10)))
        created.append(code)
    return created


def _apply_sops(db: Session, content: dict) -> list[str]:
    created = []
    for spec in [x for x in (content.get("sops") or []) if isinstance(x, dict)]:
        title = str(spec.get("title") or "").strip()
        body = str(spec.get("content") or "").strip()
        if not title or not body:
            raise TemplateError("SOP 定义缺少 title 或 content。")
        exists = db.scalar(select(MaintenanceSOP).where(MaintenanceSOP.title == title))
        if exists:
            continue
        db.add(MaintenanceSOP(sop_type=str(spec.get("sop_type") or "daily")[:30],
                              title=title[:200], content=body))
        created.append(title)
    return created


def _apply_knowledge(db: Session, content: dict, tpl: TemplateRegistry, user: Any) -> list[str]:
    spec = content.get("knowledge") or {}
    title = str(spec.get("title") or f"{tpl.name} 生成文档")[:255]
    exists = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.title == title))
    if exists:
        return []
    doc = KnowledgeDocument(title=title, doc_type=str(spec.get("doc_type") or "manual")[:30],
                            uploaded_by=getattr(user, "id", None), status="ready")
    db.add(doc)
    db.flush()
    from app.models.knowledge import KnowledgeChunk

    body = str(spec.get("content") or "")
    for i, para in enumerate([p for p in re.split(r"\n\s*\n|\n", body) if p.strip()][:20]):
        db.add(KnowledgeChunk(document_id=doc.id, chunk_index=i, content=para.strip()[:1000]))
    return [title]


def record_audit(db: Session, user: Any, action: str, entity: str, entity_id: int | None,
                 detail: str) -> None:
    """统一走 services.audit 的审计落库（失败不阻断主流程）。"""
    try:
        _record_audit(db, user_id=getattr(user, "id", None), action=action, entity=entity,
                      entity_id=entity_id, detail={"summary": detail})
    except Exception:  # noqa: BLE001
        logger.warning("模板操作审计写入失败", exc_info=True)


def export_template_package(db: Session, tpl: TemplateRegistry) -> tuple[str, bytes]:
    """导出单个模板为 .aimstpl（12.4-D）。返回 (文件名, 字节)。"""
    content = get_version_content(db, tpl)
    version_rows = list(db.scalars(select(TemplateVersion).where(TemplateVersion.template_id == tpl.id)
                                   .order_by(TemplateVersion.version)))
    manifest = {
        "uid": tpl.template_uid,
        "name": tpl.name,
        "category": tpl.category,
        "version": f"1.{tpl.current_version}.0",
        "min_aims_version": APP_VERSION,
        "author": tpl.author or "AIMS",
        "description": tpl.description,
        "tags": tpl.tags or [],
        "is_public": bool(tpl.is_public),
        "exported_from_version": tpl.current_version,
        "history_versions": [v.version for v in version_rows],
        "dependencies": [
            {"type": d.dep_type, "key": d.dep_key, "version": d.dep_version}
            for d in db.scalars(select(TemplateDependency).where(TemplateDependency.template_id == tpl.id))
        ],
    }
    from app.services.template_package import build_package

    safe_name = re.sub(r"[^\w.-]+", "_", tpl.name)[:60] or "template"
    data = build_package(manifest=manifest, template=content, readme=tpl.description)
    return f"{safe_name}.aimstpl", data
