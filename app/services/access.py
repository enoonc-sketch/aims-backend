"""账号的角色 / 页面权限 / 界面登录状态（托盘用）。

* 页面权限：权限码 ``page.<页面id>``，只管“界面能不能打开这个页面”，由前端执行；
  接口级权限（device.write 等）仍由服务端 require_permission 强制。
* 超级管理员永远不受限；还没有任何角色的老账号视为“不限制页面”（升级前行为不变）。
* 界面登录状态：前端登录 / 刷新令牌 / 退出时上报一次，服务端按令牌自己判定是谁，
  启动器（托盘）凭启动时生成的口令读取。进程内存，不落库；令牌过期且没有新上报就算未登录。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.system import Permission, Role, RolePermission, User, UserRole

# 与前端 showPage 的页面 id 对应；“sys”（系统）不受限：改密码、退出登录都在那里。
PAGE_IDS = ("dash", "dev", "comm", "alarm", "data", "danal", "ai", "know", "tpl",
            "maint", "asset", "lab", "ext")
PAGE_PERM_PREFIX = "page."


def role_codes_for(db: Session, user_ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {uid: [] for uid in user_ids}
    if not user_ids:
        return out
    rows = db.execute(select(UserRole.user_id, Role.code).join(Role, Role.id == UserRole.role_id)
                      .where(UserRole.user_id.in_(user_ids)).order_by(Role.code)).all()
    for uid, code in rows:
        out[uid].append(code)
    return out


def role_names_for(db: Session, user_id: int) -> list[str]:
    return list(db.scalars(select(Role.name).join(UserRole, UserRole.role_id == Role.id)
                           .where(UserRole.user_id == user_id).order_by(Role.code)).all())


def allowed_pages(db: Session, user: User) -> Optional[list[str]]:
    """None = 不限制；否则是允许打开的页面 id 列表。"""
    if user.is_superuser:
        return None
    if db.scalar(select(UserRole.role_id).where(UserRole.user_id == user.id).limit(1)) is None:
        return None
    codes = db.scalars(select(Permission.code).join(RolePermission, RolePermission.permission_id == Permission.id)
                       .join(UserRole, UserRole.role_id == RolePermission.role_id)
                       .where(UserRole.user_id == user.id, Permission.code.like(PAGE_PERM_PREFIX + "%"))).all()
    allowed = {c[len(PAGE_PERM_PREFIX):] for c in codes}
    return [p for p in PAGE_IDS if p in allowed]


def permission_label(db: Session, user: User) -> str:
    if user.is_superuser:
        return "超级管理员（全部权限）"
    names = role_names_for(db, user.id)
    return "、".join(names) if names else "未分配角色"


# ---------------------------------------------------------------- 界面登录状态
_lock = threading.Lock()
_state: dict = {}


def report_login(user: User, label: str, access_exp: float) -> None:
    with _lock:
        _state.clear()
        _state.update(username=user.username, full_name=user.full_name or "",
                      is_superuser=bool(user.is_superuser), permission=label,
                      expires_at=float(access_exp), reported_at=time.time())


def report_logout() -> None:
    with _lock:
        _state.clear()


def snapshot() -> dict:
    with _lock:
        s = dict(_state)
    if s and s.get("expires_at", 0) > time.time():
        return {"logged_in": True, "username": s["username"], "full_name": s["full_name"],
                "is_superuser": s["is_superuser"], "permission": s["permission"]}
    return {"logged_in": False, "username": "", "full_name": "", "is_superuser": False, "permission": ""}
