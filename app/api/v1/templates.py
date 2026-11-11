"""Template Center API — 模板中心（计划书第十二章）。

分类清单见 app/services/template_center.py::CATEGORIES。
导入走"先预览后确认"两段式（12.5 第四/五步），失败全部带明确中文原因。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.pagination import LimitQuery, SkipQuery
from app.models.system import User
from app.models.template import (
    TemplateImportLog,
    TemplateRegistry,
    TemplateUsageLog,
    TemplateVersion,
)
from app.schemas.template import (
    ImportPreviewOut,
    TemplateApply,
    TemplateCreate,
    TemplateDetailOut,
    TemplateOut,
    TemplateUpdate,
)
from app.services import template_center as tc
from app.services.template_package import (
    TemplatePackage,
    TemplatePackageError,
    build_package,
    iter_bundle,
    parse_package,
)

logger = logging.getLogger("aims.templates")
router = APIRouter(prefix="/templates", tags=["Templates"])

#: 系统内置模板的保护性限制（12.9-8）
_SYSTEM_READONLY_FIELDS = {"name", "category", "source"}


def _get_template(db: Session, template_id: int) -> TemplateRegistry:
    tpl = db.get(TemplateRegistry, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在。")
    return tpl


def _ensure_can_modify(tpl: TemplateRegistry, user: User) -> None:
    """内容级修改（编辑/删除/版本回退）的统一权限闸门。

    * 系统内置模板：只允许管理员做受控修改（改名/改分类在任何情况下都被拒绝）；
    * 自定义 / 导入模板：只有创建者本人或管理员可以修改、删除——其他用户
      只能查看、收藏、复制出自己独立副本或导出，保证模板之间相互隔离。
    """
    if user.is_superuser:
        return
    if tpl.source == "system":
        raise HTTPException(status_code=403, detail="系统内置模板只有管理员可以修改。")
    if tpl.created_by is not None and tpl.created_by != user.id:
        raise HTTPException(status_code=403,
                            detail="只有模板创建者或管理员可以修改/删除该模板。如需使用，可先「复制」出自己的副本。")


# ============================================================== 分类与列表 ===
@router.get("/categories")
def list_categories() -> dict:
    return {"categories": tc.CATEGORIES}


@router.get("", response_model=list[TemplateOut])
def list_templates(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    skip: int = SkipQuery,
    limit: int = LimitQuery(200),
    category: str | None = Query(default=None, description="按分类过滤"),
    source: str | None = Query(default=None, description="system / custom / imported"),
    q: str | None = Query(default=None, max_length=100, description="名称/描述搜索"),
    favorite: bool | None = Query(default=None, description="仅收藏"),
    recent: bool = Query(default=False, description="按最近使用排序"),
):
    tc.seed_builtin_templates(db)  # 幂等：首次访问把内置模板补齐
    stmt = select(TemplateRegistry)
    if category:
        stmt = stmt.where(TemplateRegistry.category == category)
    if source:
        stmt = stmt.where(TemplateRegistry.source == source)
    if favorite:
        stmt = stmt.where(TemplateRegistry.is_favorite.is_(True))
    if q:
        like = f"%{q}%"
        from sqlalchemy import or_

        stmt = stmt.where(or_(TemplateRegistry.name.ilike(like),
                              TemplateRegistry.description.ilike(like)))
    if recent:
        stmt = stmt.order_by(TemplateRegistry.last_used_at.desc().nullslast(),
                             TemplateRegistry.updated_at.desc())
    else:
        stmt = stmt.order_by(TemplateRegistry.source.asc(), TemplateRegistry.name.asc())
    return db.scalars(stmt.offset(skip).limit(limit)).all()


# ============================================================== 创建/编辑 ===
@router.post("", response_model=TemplateOut, status_code=201)
def create_template(payload: TemplateCreate, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    tc.seed_builtin_templates(db)
    if payload.category not in tc.CATEGORIES:
        raise HTTPException(status_code=400, detail=f"未知分类「{payload.category}」，可选：{('、'.join(tc.CATEGORIES))}")
    if db.scalar(select(TemplateRegistry).where(
            TemplateRegistry.name == payload.name, TemplateRegistry.source == "custom")):
        raise HTTPException(status_code=409, detail=f"已存在同名自定义模板「{payload.name}」。")
    tpl = TemplateRegistry(
        template_uid=tc.new_uid(), name=payload.name, category=payload.category,
        source="custom", description=payload.description, author=payload.author or user.username,
        tags=payload.tags, is_public=payload.is_public, is_default=payload.is_default,
        created_by=user.id, current_version=1,
    )
    db.add(tpl)
    db.flush()
    db.add(TemplateVersion(template_id=tpl.id, version=1, content=payload.content, created_by=user.id))
    tc.record_audit(db, user, "TEMPLATE_CREATE", "templates", tpl.id, f"创建模板「{payload.name}」")
    db.commit()
    db.refresh(tpl)
    return tpl


@router.get("/{template_id}", response_model=TemplateDetailOut)
def get_template(template_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    tpl = _get_template(db, template_id)
    versions = list(db.scalars(select(TemplateVersion.version)
                               .where(TemplateVersion.template_id == tpl.id)
                               .order_by(TemplateVersion.version)))
    latest = db.scalar(select(TemplateVersion).where(
        TemplateVersion.template_id == tpl.id, TemplateVersion.version == tpl.current_version))
    return TemplateDetailOut(
        **TemplateOut.model_validate(tpl).model_dump(),
        content=dict(latest.content or {}), versions=versions,
        readme=(latest.manifest or {}).get("description") if latest else None,
    )


@router.put("/{template_id}", response_model=TemplateOut)
def update_template(template_id: int, payload: TemplateUpdate,
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """编辑模板。改了 content 就追加新版本（旧版本保留，可回退）；系统模板仅允许管理员改描述等次要字段。"""
    tpl = _get_template(db, template_id)
    if tpl.source == "system" and (payload.name is not None or payload.category is not None):
        raise HTTPException(status_code=403, detail="系统内置模板不允许改名或改分类。")
    _ensure_can_modify(tpl, user)
    data = payload.model_dump(exclude_unset=True)
    content = data.pop("content", None)
    changelog = data.pop("changelog", None)
    for field, value in data.items():
        setattr(tpl, field, value)
    if content is not None:
        if not isinstance(content, dict):
            raise HTTPException(status_code=400, detail="模板正文（content）必须是 JSON 对象。")
        tc.add_version(db, tpl, content, changelog=changelog, user_id=user.id)
        tc.record_audit(db, user, "TEMPLATE_UPDATE", "templates", tpl.id,
                        f"修改模板「{tpl.name}」→ v{tpl.current_version}")
    db.commit()
    db.refresh(tpl)
    return tpl


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """删除模板：只删模板与其版本历史，**绝不触碰已应用生成的业务数据**（12.9-8）。"""
    tpl = _get_template(db, template_id)
    if tpl.source == "system":
        raise HTTPException(status_code=403, detail="系统内置模板不允许删除。")
    _ensure_can_modify(tpl, user)  # 只有创建者或管理员可以删除自定义/导入模板
    db.delete(tpl)  # versions/usage/dependencies 走 CASCADE
    tc.record_audit(db, user, "TEMPLATE_DELETE", "templates", tpl.id, f"删除模板「{tpl.name}」")
    db.commit()


@router.post("/{template_id}/duplicate", response_model=TemplateOut, status_code=201)
def duplicate_template(template_id: int, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    tpl = _get_template(db, template_id)
    copy = TemplateRegistry(
        template_uid=tc.new_uid(), name=f"{tpl.name}（副本）"[:150], category=tpl.category,
        source="custom", description=tpl.description, author=user.username,
        tags=tpl.tags, created_by=user.id, current_version=1,
    )
    db.add(copy)
    db.flush()
    content = tc.get_version_content(db, tpl)
    db.add(TemplateVersion(template_id=copy.id, version=1, content=content,
                           changelog=f"复制自模板 #{tpl.id}", created_by=user.id))
    db.add(TemplateUsageLog(template_id=tpl.id, action="duplicate",
                            target_summary=f"复制为 #{copy.id}", created_by=user.id))
    db.commit()
    db.refresh(copy)
    return copy


@router.post("/{template_id}/favorite", response_model=TemplateOut)
def toggle_favorite(template_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    tpl = _get_template(db, template_id)
    tpl.is_favorite = not tpl.is_favorite
    db.commit()
    db.refresh(tpl)
    return tpl


@router.post("/{template_id}/default", response_model=TemplateOut)
def set_default(template_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """设为该分类默认模板：同分类其它模板的 is_default 一并取消。"""
    tpl = _get_template(db, template_id)
    for other in db.scalars(select(TemplateRegistry).where(
            TemplateRegistry.category == tpl.category, TemplateRegistry.is_default.is_(True))):
        other.is_default = False
    tpl.is_default = True
    db.commit()
    db.refresh(tpl)
    return tpl


# ============================================================== 导入（12.5） ===
async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空。")
    name = file.filename or "template.aimstpl"
    return name, data


def _import_one(db: Session, filename: str, data: bytes, *, confirm: bool,
                user: User) -> ImportPreviewOut:
    try:
        pkg = parse_package(data)
    except TemplatePackageError as exc:
        if confirm:
            tc.record_import_failure(db, filename=filename, reason=str(exc), size=len(data), user=user)
        return ImportPreviewOut(ok=False, filename=filename, error=str(exc))
    if not confirm:
        try:
            preview = tc.preview_import(db, pkg)
        except tc.TemplateError as exc:
            tc.record_import_failure(db, filename=filename, reason=str(exc), size=len(data), user=user)
            return ImportPreviewOut(ok=False, filename=filename, error=str(exc))
        return ImportPreviewOut(ok=True, filename=filename, **preview)
    try:
        tpl = tc.confirm_import(db, pkg, filename=filename, user=user)
        return ImportPreviewOut(ok=True, filename=filename, manifest=pkg.manifest,
                                summary={"template_id": tpl.id, "name": tpl.name,
                                         "version": tpl.current_version})
    except TemplatePackageError as exc:
        return ImportPreviewOut(ok=False, filename=filename, error=str(exc))


@router.post("/import")
async def import_templates(
    files: list[UploadFile] = File(..., description=".aimstpl 单包 / 多文件 / 内含多包的批量 ZIP"),
    confirm: bool = Query(default=False, description="false=仅预览（第四步）；true=确认导入（第五步）"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    results: list[ImportPreviewOut] = []
    for file in files[:50]:  # 批量上限 50 个文件
        filename, data = await _read_upload(file)
        if filename.lower().endswith(".aimstpl"):
            results.append(_import_one(db, filename, data, confirm=confirm, user=user))
        else:
            # 批量 ZIP：内部每个 .aimstpl 逐个走同一套校验
            try:
                bundle = iter_bundle(data)
            except TemplatePackageError as exc:
                tc.record_import_failure(db, filename=filename, reason=str(exc), size=len(data), user=user)
                results.append(ImportPreviewOut(ok=False, filename=filename, error=str(exc)))
                continue
            if not bundle:
                results.append(ImportPreviewOut(ok=False, filename=filename,
                                                error="批量包内没有找到 .aimstpl 模板文件。"))
            for inner_name, inner_data in bundle:
                results.append(_import_one(db, f"{filename}/{inner_name}", inner_data,
                                           confirm=confirm, user=user))
    return {"results": [r.model_dump() for r in results],
            "total": len(results),
            "ok_count": sum(1 for r in results if r.ok)}


@router.get("/import/logs")
def list_import_logs(db: Session = Depends(get_db), user: User = Depends(get_current_user),
                     skip: int = SkipQuery, limit: int = LimitQuery(100)):
    stmt = select(TemplateImportLog).order_by(TemplateImportLog.id.desc()).offset(skip).limit(limit)
    rows = db.scalars(stmt).all()
    return [{"id": r.id, "filename": r.filename, "template_name": r.template_name,
             "template_id": r.template_id, "status": r.status, "reason": r.reason,
             "file_size": r.file_size, "file_sha256": r.file_sha256,
             "created_at": r.created_at} for r in rows]


# ============================================================== 导出（12.4-D） ===
@router.get("/{template_id}/export")
def export_template(template_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    tpl = _get_template(db, template_id)
    filename, data = tc.export_template_package(db, tpl)
    db.add(TemplateUsageLog(template_id=tpl.id, version=tpl.current_version, action="export",
                            target_summary=filename, created_by=user.id))
    db.commit()
    # HTTP 头只允许 latin-1：中文文件名走 RFC 5987（filename*=UTF-8''…），ASCII 部分兜底
    ascii_name = filename.encode("ascii", "ignore").decode() or "template.aimstpl"
    encoded = quote(filename)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}",
                 "X-Template-Uid": tpl.template_uid},
    )


@router.post("/export/batch")
def export_templates_batch(
    body: dict[str, Any] | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量导出：body 可给 {"ids":[...]} / {"category":"..."} / {"all_custom":true}。"""
    body = body or {}
    ids = body.get("ids")
    category = body.get("category")
    all_custom = bool(body.get("all_custom"))
    stmt = select(TemplateRegistry)
    if ids:
        stmt = stmt.where(TemplateRegistry.id.in_([int(i) for i in ids]))
    elif category:
        stmt = stmt.where(TemplateRegistry.category == category)
    elif all_custom:
        stmt = stmt.where(TemplateRegistry.source.in_(("custom", "imported")))
    else:
        raise HTTPException(status_code=400, detail="请提供 ids、category 或 all_custom 之一。")
    tpls = list(db.scalars(stmt))
    if not tpls:
        raise HTTPException(status_code=404, detail="没有匹配的模板可导出。")
    buf = __import__("io").BytesIO()
    import zipfile

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for tpl in tpls:
            filename, data = tc.export_template_package(db, tpl)
            zf.writestr(filename, data)
            db.add(TemplateUsageLog(template_id=tpl.id, version=tpl.current_version,
                                    action="export", target_summary=f"batch:{filename}",
                                    created_by=user.id))
    db.commit()
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="aims-templates-export.zip"'})


# ============================================================== 版本（12.7） ===
@router.get("/{template_id}/versions")
def list_versions(template_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    tpl = _get_template(db, template_id)
    rows = list(db.scalars(select(TemplateVersion).where(TemplateVersion.template_id == tpl.id)
                           .order_by(TemplateVersion.version)))
    return [{"version": r.version, "changelog": r.changelog, "created_at": r.created_at,
             "created_by": r.created_by,
             "summary": tc._content_summary(dict(r.content or {}))} for r in rows]


@router.get("/{template_id}/diff")
def diff_versions(template_id: int, frm: int = Query(alias="from"), to: int = Query(),
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = _get_template(db, template_id)
    try:
        a = tc.get_version_content(db, tpl, frm)
        b = tc.get_version_content(db, tpl, to)
    except tc.TemplateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"from": frm, "to": to, "changes": tc.diff_versions(a, b)}


@router.post("/{template_id}/versions/{version}/restore", response_model=TemplateOut)
def restore_version(template_id: int, version: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """版本回退：把旧版本内容追加为新版本（历史不丢，回退本身也可回退）。"""
    tpl = _get_template(db, template_id)
    _ensure_can_modify(tpl, user)
    try:
        content = tc.get_version_content(db, tpl, version)
    except tc.TemplateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    new_ver = tc.add_version(db, tpl, content, changelog=f"回退自 v{version}", user_id=user.id)
    db.add(TemplateUsageLog(template_id=tpl.id, version=new_ver, action="restore",
                            target_summary=f"回退到 v{version}", created_by=user.id))
    tc.record_audit(db, user, "TEMPLATE_RESTORE", "templates", tpl.id,
                    f"模板「{tpl.name}」回退 v{version} → v{new_ver}")
    db.commit()
    db.refresh(tpl)
    return tpl


# ============================================================== 应用（12.6） ===
@router.post("/{template_id}/apply")
def apply_template(template_id: int, payload: TemplateApply | None = None,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = _get_template(db, template_id)
    try:
        summary = tc.apply_template(db, tpl, payload.model_dump() if payload else {}, user=user)
    except tc.TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "template": {"id": tpl.id, "name": tpl.name, "version": tpl.current_version},
            "result": summary}


# ============================================================== 使用日志 ===
@router.get("/{template_id}/usage")
def template_usage(template_id: int, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user),
                   limit: int = LimitQuery(50)):
    _get_template(db, template_id)
    rows = list(db.scalars(select(TemplateUsageLog)
                           .where(TemplateUsageLog.template_id == template_id)
                           .order_by(TemplateUsageLog.id.desc()).limit(limit)))
    return [{"id": r.id, "action": r.action, "version": r.version,
             "target_summary": r.target_summary, "created_by": r.created_by,
             "created_at": r.created_at} for r in rows]
