"""`AIMS-Backend.exe deploy <command>` — the operations CLI.

The installer calls `provision`; the desktop launcher calls `ensure`; the
PowerShell scripts in tools\\ are thin wrappers around the rest, so every action
exists in exactly one implementation (this one) and can be tested without Windows.

Exit codes (stable, the installer and the .ps1 scripts depend on them):

    0  success
    2  bad usage
    3  PostgreSQL not available / not startable
    4  database or schema error
    5  restore refused (running app, bad archive, no safety copy)
    9  unexpected error
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from app.deploy import APP_VERSION, ai_engine, backup, conf, doctor, pgservice, provision as provision_mod
from app.deploy import state as state_mod
from app.deploy.paths import IS_WINDOWS, load_layout
from app.deploy.pg import Postgres, PgError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PG = 3
EXIT_DB = 4
EXIT_RESTORE = 5
EXIT_UNEXPECTED = 9


# --------------------------------------------------------------------------- #
def _setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")],
    )


def _app_running(port: int = 8000) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def _pg(layout, port: int | None = None) -> Postgres:
    state = state_mod.read_state(layout)
    return Postgres(layout, port=port or int((state.get("pg") or {}).get("port") or 55432))


def _superuser_password(layout) -> str:
    secrets = conf.read_secrets(layout)
    if not secrets.get("DB_SUPERUSER_PASSWORD"):
        raise PgError("配置目录下的 secrets.json 缺少数据库口令，无法执行该操作（请重新运行 provision）")
    return secrets["DB_SUPERUSER_PASSWORD"]


# --------------------------------------------------------------------------- #
def cmd_provision(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    try:
        report = provision_mod.provision(
            root=layout.root,
            mode=args.mode,
            pg_port=args.pg_port,
            api_port=args.api_port,
            admin_username=args.admin_user,
            admin_password=args.admin_password,
            seed=not args.no_seed,
            verify_ai=args.verify_ai,
            keep_config=args.keep_config,
            with_service=not args.no_service,
        )
    except provision_mod.ProvisionError as exc:
        print(f"\n[安装失败] {exc}\n", file=sys.stderr)
        return EXIT_PG
    except PgError as exc:
        print(f"\n[数据库错误] {exc}\n", file=sys.stderr)
        return EXIT_DB
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    state = state_mod.read_state(layout)
    pg = _pg(layout)
    ready = pg.is_ready(timeout=2) if pg.initialized else False
    info = {
        "version": APP_VERSION,
        "installed_version": state.get("app_version"),
        "installed_at": state.get("installed_at"),
        "updated_at": state.get("updated_at"),
        "root": str(layout.root),
        "data_dir": str(layout.data_dir),
        "postgres": {
            "initialized": pg.initialized,
            "ready": ready,
            "port": pg.port,
            "mode": (state.get("pg") or {}).get("mode"),
            "service": pg.service if IS_WINDOWS else None,
            "service_registered": pgservice.exists(pg.service) if IS_WINDOWS else False,
            "version": (pg.version()[0] if pg.has_binaries() else None),
        },
        "app_running": _app_running(int(conf.load_env(layout).get("PORT", 8000) or 8000)),
        "backups": len(backup.list_backups(layout)),
        "history": state.get("history", [])[-5:],
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        p = info["postgres"]
        print(f"AIMS Backend {info['version']}  （已安装 {info['installed_version'] or '未知'}）")
        print(f"  安装目录 : {info['root']}")
        print(f"  数据目录 : {info['data_dir']}")
        print(f"  数据库   : PostgreSQL {p['version'] or '?'} @ 127.0.0.1:{p['port']}  "
              f"{'运行中' if p['ready'] else '未运行'}  模式={p['mode'] or '?'}")
        if IS_WINDOWS:
            print(f"  服务     : {p['service']} {'已注册' if p['service_registered'] else '未注册'}")
        print(f"  应用进程 : {'运行中' if info['app_running'] else '未运行'}")
        print(f"  备份     : {info['backups']} 份")
        for h in info["history"]:
            print(f"  {h.get('at')}  {h.get('action'):8s} {h.get('version')}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    result = doctor.run(root=layout.root, api_port=args.api_port)
    path = doctor.write_report(layout, result)
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(doctor.report_text(result))
    print(f" 报告已写入 {path}")
    return EXIT_OK if result["ok"] else EXIT_DB


def cmd_start(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    state = state_mod.read_state(layout)
    pg = _pg(layout)
    if not pg.initialized:
        print("数据库尚未初始化，请先运行：AIMS-Backend.exe deploy provision", file=sys.stderr)
        return EXIT_PG
    mode = (state.get("pg") or {}).get("mode") or ("service" if IS_WINDOWS else "process")
    try:
        pgservice.ensure_running(pg, mode=mode, announce=lambda m: print(m, flush=True))
    except PgError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PG
    print(f"PostgreSQL 已就绪：127.0.0.1:{pg.port}")
    return EXIT_OK


def cmd_stop(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    state = state_mod.read_state(layout)
    pg = _pg(layout)
    mode = (state.get("pg") or {}).get("mode")
    if (mode == "service" or (IS_WINDOWS and pgservice.exists(pg.service))) and pgservice.exists(pg.service):
        ok, msg = pgservice.stop(pg.service)
        print(msg)
        if not ok:
            return EXIT_PG
    try:
        pg.stop()
    except PgError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PG
    print("PostgreSQL 已停止")
    # 依赖关系提示（总方案 8.4）：停止数据库会影响所有依赖它的组件
    from app.deploy.dependencies import impact_of_stopping

    impact = impact_of_stopping("postgresql")
    if impact["direct"] or impact["chain"]:
        print("提示：本次停止影响了以下组件 —— " + "、".join(impact["direct"] + impact["chain"]))
    return EXIT_OK


def cmd_backup(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    pg = _pg(layout)
    if not pg.is_ready():
        print("数据库未运行，无法备份。先运行 deploy start 或 tools\\Start-Aims.ps1", file=sys.stderr)
        return EXIT_PG
    try:
        meta = backup.create_backup(pg, password=_superuser_password(layout),
                                    reason=args.reason, layout=layout,
                                    keep=args.keep if args.keep is not None else None)
    except PgError as exc:
        print(f"备份失败：{exc}", file=sys.stderr)
        return EXIT_DB
    print(f"备份完成：{layout.backups_dir / meta['file']}"
          f"（{meta['size_bytes'] / 1e6:.1f} MB，{meta['duration_s']} 秒）")
    if args.export:
        dest = Path(args.export)
        if dest.is_dir():
            dest = dest / meta["file"]
        backup.copy_to(layout.backups_dir / meta["file"], dest)
        print(f"已导出到：{dest}")
    return EXIT_OK


def cmd_backups(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    items = backup.list_backups(layout)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return EXIT_OK
    if not items:
        print(f"还没有备份（备份目录：{layout.backups_dir}）")
        return EXIT_OK
    print(f"{'文件':<34}{'时间':<21}{'大小':>10}  版本")
    for b in items:
        tag = " (恢复前安全备份)" if b["safety"] else ""
        print(f"{b['file']:<34}{b['mtime']:<21}{b['size_bytes'] / 1e6:>8.1f}MB  {b['app_version'] or '-'}{tag}")
    return EXIT_OK


def cmd_restore(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    env = conf.load_env(layout)
    api_port = int(env.get("PORT", 8000) or 8000)
    if _app_running(api_port):
        print("AIMS 正在运行，请先关闭程序窗口（或运行 tools\\Stop-Aims.ps1）再恢复。", file=sys.stderr)
        return EXIT_RESTORE
    pg = _pg(layout)
    if not pg.initialized:
        print("数据库尚未初始化。", file=sys.stderr)
        return EXIT_PG
    try:
        src = backup.resolve(layout, args.file or "latest")
    except PgError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_RESTORE
    if not args.yes:
        print(f"即将用 {src.name} 覆盖当前数据库（当前数据会先自动备份）。")
        print("确认请加 --yes 重试。")
        return EXIT_USAGE
    if not pg.is_ready():
        try:
            pgservice.ensure_running(pg, mode=(state_mod.read_state(layout).get("pg") or {}).get("mode") or "process")
        except PgError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_PG
    try:
        result = backup.restore(pg, src, password=_superuser_password(layout), layout=layout)
    except PgError as exc:
        print(f"恢复失败：{exc}", file=sys.stderr)
        return EXIT_RESTORE
    print(f"恢复完成：{result['restored']}")
    if result.get("safety_backup"):
        print(f"恢复前的数据已备份为：{result['safety_backup']}")
    print("请重新启动 AIMS。")
    return EXIT_OK


def cmd_upgrade(args: argparse.Namespace) -> int:
    """覆盖升级: keep the config the admin may have edited, keep every byte of data."""
    args.keep_config = True
    args.verify_ai = args.verify_ai or False
    return cmd_provision(args)


def cmd_uninstall(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    pg = _pg(layout)
    print(f"停止数据库服务 {pg.service if IS_WINDOWS else ''} …")
    if IS_WINDOWS and pgservice.exists(pg.service):
        ok, msg = pgservice.stop(pg.service)
        print(f"  {msg}")
        ok, msg = pgservice.unregister(pg)
        print(f"  注销服务：{msg}")
    try:
        pg.stop()
    except PgError as exc:
        print(f"  (停止进程时提示：{exc})")

    if args.purge:
        if not args.yes:
            print("--purge 会删除数据库与全部备份，需同时加 --yes 确认。", file=sys.stderr)
            return EXIT_USAGE
        # Final safety backup before destroying anything.
        try:
            if pg.is_ready():
                meta = backup.create_backup(pg, password=_superuser_password(layout),
                                            reason="pre-uninstall", safety=True, layout=layout)
                print(f"  卸载前备份：{layout.backups_dir / meta['file']}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 卸载前备份失败：{exc}")
        for target in (layout.pg_data, layout.backups_dir, layout.logs_dir):
            print(f"  删除 {target}")
            shutil.rmtree(target, ignore_errors=True)
        for leftover in (layout.data_dir / "deployment.json", layout.config_dir / "secrets.json"):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        print(f"数据已保留：{layout.pg_data}、{layout.data_dir}")
    if IS_WINDOWS:
        try:
            import subprocess

            subprocess.run(["schtasks", "/Delete", "/TN", "AIMS Backup", "/F"],
                           capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            pass
    print("卸载完成。程序目录本身由 Windows 的“应用和功能”或安装包卸载程序删除。")
    return EXIT_OK


def cmd_version(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    state = state_mod.read_state(layout)
    print(f"AIMS Backend {APP_VERSION}")
    print(f"部署版本：{state.get('app_version') or '未部署'}")
    if state.get("build_id"):
        print(f"构建号：{state['build_id']}")
    print(f"安装时间：{state.get('installed_at') or '-'}")
    print(f"配置：{layout.env_file}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
def cmd_selftest(args: argparse.Namespace) -> int:
    """功能自检：对运行中的实例跑一遍真实业务请求（安装验收/升级验收都用它）。"""
    from app.deploy import selftest

    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    env = conf.load_env(layout)
    port = int(args.api_port or conf.env_int(env, "PORT", 8000))
    user = args.user or env.get("BOOTSTRAP_ADMIN_USERNAME") or "admin"
    password = args.password or conf.read_secrets(layout).get("ADMIN_PASSWORD")
    base = args.url or f"http://127.0.0.1:{port}"

    if not _app_running(port):
        print(f"AIMS 没有在 127.0.0.1:{port} 上运行，自检无法进行。\n"
              f"请先启动 AIMS（桌面图标或 tools\\Start-Aims.ps1）后重试。", file=sys.stderr)
        return EXIT_PG
    if not password:
        print("找不到管理员口令（Config/secrets.json），请用 --password 指定。", file=sys.stderr)
        return EXIT_USAGE

    report_path = args.report
    if not report_path:
        report_path = str(layout.logs_dir / "selftest.json")
    result = selftest.run(base_url=base, username=user, password=password,
                          report_path=report_path, verbose=not args.quiet)
    print(selftest.report_text(result))
    print(f" 报告已写入 {report_path}")
    if not result["ok"] and result["passed"] == 0:
        print("提示：全部失败通常意味着管理员口令已被修改，请用 --user/--password 指定当前账号。",
              file=sys.stderr)
    return EXIT_OK if result["ok"] else EXIT_DB


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="AIMS-Backend deploy", description="AIMS 本地部署运维命令")
    p.add_argument("--root", help="安装根目录（默认自动检测）")
    sub = p.add_subparsers(dest="command", required=True)

    def add_provision_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--pg-port", type=int, default=None, help="PostgreSQL 端口（默认 55432，被占用则自动顺延）")
        sp.add_argument("--api-port", type=int, default=8000, help="应用端口（写入配置）")
        sp.add_argument("--admin-user", default="admin", help="首个管理员用户名")
        sp.add_argument("--admin-password", default=None, help="首个管理员密码（默认自动生成，见 secrets.json）")
        sp.add_argument("--mode", choices=["auto", "service", "process"], default="auto",
                        help="PostgreSQL 运行方式：service=Windows 服务，process=随程序启动")
        sp.add_argument("--no-service", action="store_true", help="不注册 Windows 服务")
        sp.add_argument("--no-seed", action="store_true", help="不写入演示数据")
        sp.add_argument("--keep-config", action="store_true", help="保留已有配置文件中的人工修改")
        sp.add_argument("--verify-ai", action="store_true", help="额外做一次本地模型加载自检（较慢）")
        sp.add_argument("--json", default=None, help="把结果写入该 JSON 文件")

    sp = sub.add_parser("provision", aliases=["install"], help="初始化/修复本地部署（安装程序调用）")
    add_provision_common(sp)
    sp.set_defaults(func=cmd_provision)

    sp = sub.add_parser("upgrade", help="覆盖升级：保留数据与配置，补齐新版本表结构")
    add_provision_common(sp)
    sp.set_defaults(func=cmd_upgrade)

    sp = sub.add_parser("status", help="查看部署状态")
    sp.add_argument("--json", nargs="?", const="-", default=None)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("doctor", help="安装自检")
    sp.add_argument("--api-port", type=int, default=8000)
    sp.add_argument("--json", default=None)
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("start", help="启动数据库")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="停止数据库")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("backup", help="立即备份")
    sp.add_argument("--reason", default="manual")
    sp.add_argument("--keep", type=int, default=None, help="保留份数（默认 14）")
    sp.add_argument("--export", default=None, help="导出到指定文件或目录（U 盘/网络盘）")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("backups", help="列出备份")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_backups)

    sp = sub.add_parser("restore", help="从备份恢复")
    sp.add_argument("file", nargs="?", default="latest", help="备份文件名（默认最新一份）")
    sp.add_argument("--yes", action="store_true", help="确认覆盖当前数据库")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("uninstall", help="停止服务并（可选）清除数据")
    sp.add_argument("--purge", action="store_true", help="删除数据库与备份")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser("version", help="显示版本")
    sp.set_defaults(func=cmd_version)

    sp = sub.add_parser("selftest", help="功能自检：对运行中的实例跑真实业务用例")
    sp.add_argument("--url", default=None, help="默认 http://127.0.0.1:<配置端口>")
    sp.add_argument("--api-port", type=int, default=0)
    sp.add_argument("--user", default=None)
    sp.add_argument("--password", default=None)
    sp.add_argument("--report", default=None, help="JSON 报告路径（默认 Logs/selftest.json）")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_selftest)

    sp = sub.add_parser("ai-probe", help="检查本地 AI 引擎（含模型加载自检）")
    sp.add_argument("--warmup", action="store_true")
    sp.set_defaults(func=lambda a: _cmd_ai_probe(a))

    # `--root` 也能写在子命令后面。SUPPRESS 保证"没写"时不会用 None 覆盖
    # 写在 `deploy` 前面的那个值（argparse 的子解析器默认值会覆盖父级）。
    for group in p._subparsers._group_actions:  # type: ignore[attr-defined]
        for _name, sub in group.choices.items():
            if not any(a.dest == "root" for a in sub._actions):
                sub.add_argument("--root", default=argparse.SUPPRESS, help="安装根目录")
    return p


def _cmd_ai_probe(args: argparse.Namespace) -> int:
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    info = ai_engine.probe(layout, deep=args.warmup)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return EXIT_OK if info.get("backend") else EXIT_PG


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    layout = load_layout(Path(args.root) if getattr(args, "root", None) else None)
    try:
        _setup_logging(layout.logs_dir / "provision.log")
    except Exception:  # noqa: BLE001
        logging.basicConfig(level=logging.INFO)
    logging.info("deploy %s (version %s, root %s)", args.command, APP_VERSION, layout.root)
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        logging.exception("deploy command failed")
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
