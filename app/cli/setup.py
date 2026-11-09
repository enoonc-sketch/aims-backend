"""一步初始化向导: `AIMS-Backend.exe setup` (源码: python run_standalone.py setup)

以前第一次部署要手动做四件事: 生成密钥、建 .env、配证书、建管理员。这里一步做完:

  1. 生成 SECRET_KEY 并写进 .env —— **一次性生成并持久化**。已经有合格密钥就原样保留(换密钥会让所有会话
     失效、并让已保存的外部数据源密码解不开), 占位符/弱值会被替换。应用本身仍然从不隐式生成或保存密钥:
     密钥来自部署方运行这个向导的显式动作, 落在 .env(代码和镜像之外)。
  2. 本机 HTTPS: 生成自签名证书并写入 SSL_CERTFILE / SSL_KEYFILE(--http-local 则改为仅限本机的明文 HTTP)。
  3. 创建首个管理员(库里已有用户就跳过); 密码必须过策略, 交互输入、不回显。

幂等: 重复运行不会换掉已有密钥、不会重复建用户。
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.secret_rules import is_acceptable_secret, secret_problem


class SetupError(Exception):
    pass


@dataclass
class SetupReport:
    env_path: Path
    secret_created: bool = False
    secret_replaced_reason: Optional[str] = None
    tls: str = "kept"                  # kept / dev_cert / http_local / external
    env_updates: dict = field(default_factory=dict)  # 本次写进 .env 的键(不含 SECRET_KEY 的值以外的秘密)
    secret: str = ""                   # 当前生效的密钥(仅供 main() 导出到本进程环境, 不打印)


# ------------------------------------------------------------------ .env ----
def read_env(path: Path) -> dict[str, str]:
    """读 .env(容忍 Windows 记事本的 BOM)。不是完整的 dotenv 解析器: 只处理 KEY=VALUE 行。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    """就地更新/追加键值, 保留其他行和注释; 统一写成不带 BOM 的 UTF-8。"""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    pending = dict(updates)
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                lines[i] = f"{key}={pending.pop(key)}"
    for k, v in pending.items():
        lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    try:
        os.chmod(path, 0o600)  # 里面有密钥; Windows 上 chmod 基本无效, 见提示信息
    except OSError:
        pass


# ------------------------------------------------------------- file steps ----
def prepare_files(base: Path, *, http_local: bool = False) -> SetupReport:
    """密钥 / TLS / .env(纯文件操作, 不碰数据库, 不修改进程环境变量)。"""
    base = Path(base)
    env_path = base / ".env"
    existing = read_env(env_path)
    report = SetupReport(env_path=env_path)
    updates: dict[str, str] = {}

    # 1) 密钥: 进程环境里已有合格值 = 部署方已经外部注入, 不动; 否则看 .env; 都不行才生成
    env_secret = os.environ.get("SECRET_KEY", "")
    file_secret = existing.get("SECRET_KEY", "")
    if is_acceptable_secret(env_secret) and not file_secret:
        report.secret = env_secret
    elif is_acceptable_secret(file_secret):
        report.secret = file_secret
    else:
        if file_secret:
            report.secret_replaced_reason = secret_problem(file_secret)
        report.secret = secrets.token_urlsafe(64)
        updates["SECRET_KEY"] = report.secret
        report.secret_created = True

    # 2) TLS
    already_tls = bool(existing.get("SSL_CERTFILE") and existing.get("SSL_KEYFILE")) or \
        bool(os.environ.get("SSL_CERTFILE") and os.environ.get("SSL_KEYFILE"))
    if http_local:
        updates["APP_ENV"] = "development"
        updates["REQUIRE_HTTPS"] = "false"
        report.tls = "http_local"
    elif already_tls:
        report.tls = "kept"
    elif existing.get("REQUIRE_HTTPS", "").lower() == "true" and not existing.get("SSL_CERTFILE"):
        report.tls = "external"  # 已明确声明由反向代理终止 TLS, 不替用户生成证书
    else:
        from app.cli.devcert import generate_dev_cert

        cert, key = generate_dev_cert(base / "certs")
        updates["SSL_CERTFILE"] = "certs/dev-cert.pem"
        updates["SSL_KEYFILE"] = "certs/dev-key.pem"
        report.tls = "dev_cert"

    if updates or not env_path.is_file() or env_path.read_bytes().startswith(b"\xef\xbb\xbf"):
        upsert_env(env_path, updates)
    report.env_updates = {k: v for k, v in updates.items() if k != "SECRET_KEY"}
    return report


# -------------------------------------------------------------- admin step ----
def create_first_admin_if_needed(db, username: str, password: str) -> bool:
    """库里已有用户 -> 跳过(返回 False); 否则创建并 commit(返回 True)。密码不合格抛 SetupError。"""
    from app.services.auth_service import create_admin_user, has_any_user

    if has_any_user(db):
        return False
    try:
        create_admin_user(db, username, password, audit_action="setup.admin_created")
    except ValueError as e:
        raise SetupError(str(e)) from e
    db.commit()
    return True


def _prompt_admin(default_user: str = "admin") -> tuple[str, str]:
    from app.core.security import validate_password_strength

    user = input(f"管理员用户名 [{default_user}]: ").strip() or default_user
    for attempt in range(3):
        pw = getpass.getpass("管理员密码(至少 12 位, 输入不显示): ")
        try:
            # 0.5.0 的 validate_password_strength 不合格时抛 ValueError(secure 包是返回原因列表)
            validate_password_strength(pw, user)
        except ValueError as e:
            print("  密码不合格: " + str(e))
            continue
        if getpass.getpass("再输入一次: ") != pw:
            print("  两次输入不一致")
            continue
        return user, pw
    raise SetupError("密码连续 3 次不合格, 已退出(其他配置已保存; 再运行一次 setup 即可)")


# --------------------------------------------------------------------- CLI ----
def main(argv: Optional[list[str]] = None, *, base: Optional[Path] = None) -> int:
    ap = argparse.ArgumentParser(prog="setup", description="一步初始化: 密钥 + HTTPS 证书 + 首个管理员")
    ap.add_argument("--http-local", action="store_true", help="不用 HTTPS, 仅限本机明文 HTTP(只适合本机试用, 生产禁用)")
    ap.add_argument("--admin-user", help="管理员用户名(默认交互询问, 缺省 admin)")
    ap.add_argument("--password-env", help="从这个环境变量读取管理员密码(自动化用; 不要把密码写在命令行)")
    ap.add_argument("--skip-admin", action="store_true", help="只做密钥/证书, 不创建管理员")
    args = ap.parse_args(argv)
    base = Path(base) if base else Path.cwd()

    try:
        report = prepare_files(base, http_local=args.http_local)
    except Exception as e:  # noqa: BLE001
        print(f"初始化失败: {e}")
        return 1

    if report.secret_created:
        why = f"(原来的值{report.secret_replaced_reason}, 已替换)" if report.secret_replaced_reason else ""
        print(f"[1/3] 已生成 SECRET_KEY 并写入 {report.env_path} {why}")
    else:
        print("[1/3] SECRET_KEY 已就绪(沿用现有值, 未改动)")
    print({
        "dev_cert": "[2/3] 已生成本机 HTTPS 证书(certs/, 自签名: 浏览器会提示不受信任, 点\"继续访问\"即可)",
        "http_local": "[2/3] 已设为仅限本机的明文 HTTP(APP_ENV=development, REQUIRE_HTTPS=false)",
        "kept": "[2/3] HTTPS 证书配置沿用现有值",
        "external": "[2/3] 沿用\"由反向代理终止 TLS\"的现有配置",
    }[report.tls])

    # 后面要导入 app.db / app.core.security, 它们在导入时读取配置: 先把本次生效的值导出到本进程环境
    os.environ["SECRET_KEY"] = report.secret
    for k, v in report.env_updates.items():
        os.environ[k] = v
    os.environ.setdefault("DATABASE_URL", read_env(report.env_path).get("DATABASE_URL", os.environ.get("DATABASE_URL", "")) or "")
    if not os.environ["DATABASE_URL"]:
        del os.environ["DATABASE_URL"]

    if args.skip_admin:
        print("[3/3] 已跳过创建管理员(--skip-admin)")
    else:
        try:
            from app.db.init_db import create_tables
            from app.db.session import get_session_factory
            from app.services.auth_service import has_any_user

            create_tables()
            db = get_session_factory()()
            try:
                if has_any_user(db):
                    print("[3/3] 库里已有用户, 跳过创建管理员")
                else:
                    if args.password_env:
                        pw = os.environ.get(args.password_env, "")
                        if not pw:
                            raise SetupError(f"环境变量 {args.password_env} 为空")
                        user = args.admin_user or "admin"
                    elif sys.stdin.isatty():
                        user, pw = _prompt_admin(args.admin_user or "admin")
                    else:
                        raise SetupError("非交互环境下创建管理员需要 --password-env <环境变量名>(或用 --skip-admin)")
                    create_first_admin_if_needed(db, user, pw)
                    print(f"[3/3] 已创建管理员 {user}")
            finally:
                db.close()
        except SetupError as e:
            print(f"[3/3] 未能创建管理员: {e}")
            return 1

    print()
    print("初始化完成。下一步: 直接运行 AIMS-Backend.exe (源码: python run_standalone.py)")
    port = os.environ.get("PORT") or read_env(report.env_path).get("PORT") or "8000"
    print(f"  {'http' if args.http_local else 'https'}://127.0.0.1:{port}/ui")
    print(f"注意: {report.env_path} 里有密钥, 请保护好这个目录(Windows 上文件权限不会被自动收紧), 不要提交到版本库或发给别人。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
