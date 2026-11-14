#!/usr/bin/env python3
"""用户运维命令行(在服务器上用, 不经过网络接口):

    python scripts/manage_users.py create-admin <用户名>      # 创建管理员(交互输入密码)
    python scripts/manage_users.py reset-password <用户名>    # 重置密码 + 重新启用 + 让所有旧会话失效
    python scripts/manage_users.py activate <用户名>          # 仅重新启用
    python scripts/manage_users.py list
    (打包后的 EXE: AIMS-Backend.exe create-admin <用户名>, 子命令相同)

第一次部署更省事的做法是 `setup` 向导(生成密钥 + 证书 + 创建管理员一步完成)。
密码永远不作为命令行参数(会进 shell 历史和进程列表): 默认用 getpass 交互输入;
自动化场景用 --password-env <环境变量名> 从环境变量读取。需要和服务一样的 SECRET_KEY / DATABASE_URL 环境。
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys


def _read_password(args) -> str:
    if args.password_env:
        pw = os.environ.get(args.password_env, "")
        if not pw:
            sys.exit(f"环境变量 {args.password_env} 为空")
        return pw
    pw = getpass.getpass("新密码: ")
    if pw != getpass.getpass("再输入一次: "):
        sys.exit("两次输入不一致")
    return pw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("create-admin", "reset-password"):
        sp = sub.add_parser(name)
        sp.add_argument("username")
        sp.add_argument("--password-env", help="从这个环境变量读取密码(不要把密码写在命令行)")
    sub.add_parser("activate").add_argument("username")
    sub.add_parser("list")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.core.security import hash_password, is_dev_default_credential, validate_password_strength
    from app.db.init_db import _ensure_roles, create_tables
    from app.db.session import get_session_factory
    from app.models.system import User
    from app.services.auth_service import create_admin_user, invalidate_user_sessions, write_audit

    create_tables()
    db = get_session_factory()()
    try:
        _ensure_roles(db)
        if args.cmd == "list":
            for u in db.scalars(select(User).order_by(User.id)).all():
                print(f"{u.id:>4}  {u.username:<24} {'superuser' if u.is_superuser else 'user     '}  {'active' if u.is_active else 'DISABLED'}")
            return 0

        user = db.scalar(select(User).where(User.username == args.username))
        if args.cmd == "activate":
            if user is None:
                sys.exit("用户不存在")
            user.is_active = True
            write_audit(db, action="cli.user_activated", user_id=None, entity="users", entity_id=user.id,
                        detail={"username": user.username})
            db.commit()
            print(f"已启用 {user.username}")
            return 0

        if args.cmd == "create-admin" and user is not None:
            sys.exit("用户已存在; 要改密码请用 reset-password")
        if args.cmd == "reset-password" and user is None:
            sys.exit("用户不存在")
        password = _read_password(args)

        if args.cmd == "create-admin":
            try:
                user = create_admin_user(db, args.username, password, allow_dev_default=True)
            except ValueError as e:
                sys.exit(str(e))
            print(f"已创建管理员 {user.username}")
        else:
            # 0.5.0 的 validate_password_strength 不合格时抛 ValueError(secure 包是返回原因列表)
            if not is_dev_default_credential(args.username, password):   # 开发默认 admin/admin123 放行（生产环境永不放行）
                try:
                    validate_password_strength(password, args.username)
                except ValueError as e:
                    sys.exit("密码不符合策略: " + str(e))
            user.hashed_password = hash_password(password)
            user.is_active = True
            invalidate_user_sessions(db, user, "password_changed")
            write_audit(db, action="cli.password_reset", entity="users", entity_id=user.id, detail={"username": user.username})
            print(f"已重置 {user.username} 的密码并启用账号; 其所有旧会话已失效")
        db.commit()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
