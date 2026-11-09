"""Operator CLI — the only supported way to create/recover accounts.

    python -m app.cli gen-secret                      # print a fresh SECRET_KEY value
    python -m app.cli create-admin --username ops     # prompts for the password
    python -m app.cli reset-password --username ops   # also signs the user out everywhere
    python -m app.cli unlock --username ops           # clear login-failure lockout

Non-interactive use (CI/provisioning): `--password-env VAR` reads the password from an
environment variable instead of prompting (never pass passwords on the command line).
All account changes are written to the audit log (user_id NULL, action CLI_*).
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from datetime import datetime, timezone


def _read_password(args) -> str:
    if args.password_env:
        pw = os.environ.get(args.password_env, "")
        if not pw:
            sys.exit(f"Environment variable {args.password_env} is empty or not set")
        return pw
    first = getpass.getpass("New password: ")
    if first != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    return first


def _session():
    from app.db.init_db import create_tables
    from app.db.session import get_session_factory

    create_tables()
    return get_session_factory()()


def cmd_gen_secret(_args) -> int:
    print(secrets.token_urlsafe(64))
    return 0


def cmd_create_admin(args) -> int:
    from sqlalchemy import select

    from app.core.security import hash_password, validate_password_strength
    from app.db.init_db import _seed_permissions, _seed_roles
    from app.models.system import Role, User, UserRole
    from app.services.audit import record_audit

    password = _read_password(args)
    try:
        validate_password_strength(password, args.username)
    except ValueError as e:
        sys.exit(f"Password rejected: {e}")

    db = _session()
    try:
        _seed_roles(db)
        _seed_permissions(db)
        if db.scalar(select(User).where(User.username == args.username)):
            sys.exit(f"User {args.username!r} already exists (use reset-password)")
        user = User(username=args.username, hashed_password=hash_password(password),
                    full_name=args.full_name, email=args.email, is_superuser=True)
        db.add(user)
        db.flush()
        role = db.scalar(select(Role).where(Role.code == "admin"))
        if role is not None:
            db.add(UserRole(user_id=user.id, role_id=role.id))
        record_audit(db, user_id=None, action="CLI_CREATE_ADMIN", entity="users", entity_id=user.id,
                     detail={"username": user.username})
        db.commit()
        print(f"Administrator {args.username!r} created.")
        return 0
    finally:
        db.close()


def cmd_reset_password(args) -> int:
    from sqlalchemy import delete, select

    from app.core.security import hash_password, validate_password_strength
    from app.models.system import LoginFailure, User
    from app.services.audit import record_audit
    from app.services.auth_sessions import revoke_all_for_user

    password = _read_password(args)
    try:
        validate_password_strength(password, args.username)
    except ValueError as e:
        sys.exit(f"Password rejected: {e}")

    db = _session()
    try:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            sys.exit(f"User {args.username!r} not found")
        user.hashed_password = hash_password(password)
        user.is_active = True
        revoked = revoke_all_for_user(db, user.id, datetime.now(timezone.utc))
        db.execute(delete(LoginFailure).where(LoginFailure.username == args.username.strip().lower()))
        record_audit(db, user_id=None, action="CLI_RESET_PASSWORD", entity="users", entity_id=user.id,
                     detail={"username": user.username, "sessions_revoked": revoked})
        db.commit()
        print(f"Password for {args.username!r} reset; {revoked} session(s) revoked.")
        return 0
    finally:
        db.close()


def cmd_unlock(args) -> int:
    from sqlalchemy import delete

    from app.models.system import LoginFailure
    from app.services.audit import record_audit

    db = _session()
    try:
        n = db.execute(delete(LoginFailure).where(LoginFailure.username == args.username.strip().lower())).rowcount
        record_audit(db, user_id=None, action="CLI_UNLOCK", entity="users", detail={"username": args.username})
        db.commit()
        print(f"Cleared {n or 0} recorded failure(s) for {args.username!r}.")
        return 0
    finally:
        db.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("gen-secret", help="print a random SECRET_KEY").set_defaults(fn=cmd_gen_secret)

    p = sub.add_parser("create-admin", help="create a superuser")
    p.add_argument("--username", required=True)
    p.add_argument("--full-name", default="System Administrator")
    p.add_argument("--email", default=None)
    p.add_argument("--password-env", default=None, metavar="VAR")
    p.set_defaults(fn=cmd_create_admin)

    p = sub.add_parser("reset-password", help="set a new password and revoke all sessions")
    p.add_argument("--username", required=True)
    p.add_argument("--password-env", default=None, metavar="VAR")
    p.set_defaults(fn=cmd_reset_password)

    p = sub.add_parser("unlock", help="clear login-failure lockout for a username")
    p.add_argument("--username", required=True)
    p.set_defaults(fn=cmd_unlock)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
