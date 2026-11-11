#!/usr/bin/env python3
"""AIMS 发布前安全审计。

做两件事，都不依赖"人工看一眼"：

1. **静态**：扫描 FastAPI 路由表，列出每个端点的认证依赖；发现"业务路由没有任何
   认证依赖"直接判为高危（这正是 0.6.0 的 H-1 漏洞形态：70 个 GET 裸奔）。
2. **动态**：对一个**运行中的实例**发真实请求，验证：
   - 未认证访问受保护端点必须是 401（不能是 200，也不能是 500）
   - 普通用户访问超管端点必须是 403
   - 伪造 / 篡改 / alg=none 的 JWT 必须被拒
   - 默认弱口令（admin/admin123）必须登录失败
   - 安全响应头（CSP 等）存在
   - 登录失败节流生效（连续失败应出现 429）

输出 JSON 报告（给《安全审计报告》引用）+ 控制台摘要；有高危项时退出码非 0，
可以直接卡在发布流水线里。

用法：
    python tools/security_audit.py --api http://127.0.0.1:8000 \
        --admin reladmin --admin-password '***' --json security-audit.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# 审计会发几十个请求，把 httpx 的 INFO 日志压掉，只保留结论
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUTH_FUNCS = ("get_current_user", "get_current_superuser", "require_permission")
#: 明确对匿名开放的路由（必须逐个列出理由，新增必须走评审）
PUBLIC_PATHS = {
    "/": "前端页面",
    "/ui": "前端页面",
    "/health": "存活探针",
    "/api/v1/health": "存活探针",
    "/api/v1/auth/login": "登录入口",
    "/api/v1/auth/refresh": "刷新令牌入口",
    "/api/v1/auth/logout": "登出（无需身份，按令牌自身判断）",
    "/api/v1/system/version": "版本信息（只读，不含敏感数据）",
}


@dataclass
class Finding:
    level: str          # high / medium / low / info
    key: str
    title: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "key": self.key, "title": self.title, "detail": self.detail}


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, level: str, key: str, title: str, detail: str = "") -> None:
        self.findings.append(Finding(level, key, title, detail))

    def check(self, name: str, ok: bool, detail: str = "", level: str = "high") -> bool:
        self.checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            self.add(level, name, name, detail)
        return ok

    @property
    def high(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "high"]

    @property
    def medium(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "medium"]


# --------------------------------------------------------------------------- #
# 静态：路由认证覆盖
# --------------------------------------------------------------------------- #
def _dep_names(dependant) -> set[str]:  # noqa: ANN001 - FastAPI 内部类型
    names: set[str] = set()
    stack = [dependant]
    while stack:
        d = stack.pop()
        if d is None:
            continue
        call = getattr(d, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", str(call)))
        stack.extend(getattr(d, "dependencies", []) or [])
    return names


def scan_routes() -> tuple[list[dict[str, Any]], list[Finding]]:
    from app.main import app  # 延迟导入：需要 app 目录在 sys.path 上

    rows: list[dict[str, Any]] = []
    problems: list[Finding] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = sorted(getattr(route, "methods", []) or [])
        if not path.startswith("/api/") and path not in ("/health", "/", "/ui"):
            continue
        if not methods:
            continue
        deps = _dep_names(getattr(route, "dependant", None))
        auth = sorted(d for d in deps if d in AUTH_FUNCS)
        rows.append({"path": path, "methods": methods, "auth": auth})
        is_public = path in PUBLIC_PATHS
        write = any(m in ("POST", "PUT", "PATCH", "DELETE") for m in methods)
        if not auth and not is_public:
            problems.append(Finding(
                "high", "route-no-auth", f"{','.join(methods)} {path} 没有任何认证依赖",
                "未认证即可访问业务数据，等同于 0.6.0 的 H-1 漏洞",
            ))
        elif auth == ["get_current_user"] and write and path.startswith("/api/v1/system"):
            problems.append(Finding(
                "medium", "system-write-not-superuser", f"{','.join(methods)} {path} 写操作只要求登录",
                "系统管理类写操作应提升到 get_current_superuser",
            ))
    return rows, problems


# --------------------------------------------------------------------------- #
# 动态：真实请求验证
# --------------------------------------------------------------------------- #
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _login(client: httpx.Client, username: str, password: str) -> tuple[int, dict[str, Any]]:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {}
    return r.status_code, body


def dynamic_checks(api: str, admin: str, admin_password: str, timeout: float = 20.0) -> list[Finding]:
    findings: list[Finding] = []
    with httpx.Client(base_url=api, timeout=timeout, follow_redirects=False) as client:
        # 1) 默认弱口令必须失效
        code, _ = _login(client, "admin", "admin123")
        if code == 200:
            findings.append(Finding("high", "default-password", "默认口令 admin/admin123 仍可登录",
                                    "安装后必须强制设置管理员口令"))
        # 2) 错误口令
        code, _ = _login(client, admin, admin_password + "x")
        if code != 401:
            findings.append(Finding("medium", "bad-password-status", f"错误口令返回 {code}，期望 401"))
        # 3) 正确登录
        code, body = _login(client, admin, admin_password)
        if code != 200:
            findings.append(Finding("high", "admin-login", f"管理员登录失败（HTTP {code}）",
                                    "无法继续动态检查，请确认账号与口令"))
            return findings
        token = body.get("access_token") or ""
        headers = {"Authorization": f"Bearer {token}"}

        # 4) 篡改令牌：改 payload、改签名、alg=none
        parts = token.split(".")
        if len(parts) == 3:
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            payload["is_superuser"] = True
            forged = f"{parts[0]}.{_b64(json.dumps(payload).encode())}.{parts[2]}"
            r = client.get("/api/v1/system/users", headers={"Authorization": f"Bearer {forged}"})
            if r.status_code == 200:
                findings.append(Finding("high", "jwt-payload-forgery", "篡改 JWT 载荷后仍被接受",
                                        "签名校验失效"))
            none_token = f"{_b64(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}." \
                         f"{parts[1]}."
            r = client.get("/api/v1/system/users", headers={"Authorization": f"Bearer {none_token}"})
            if r.status_code == 200:
                findings.append(Finding("high", "jwt-alg-none", "alg=none 的令牌被接受",
                                        "必须固定 HS256 并校验签名"))

        # 5) 未认证访问受保护端点
        routes, _ = scan_routes()
        unauth_bad = []
        for row in routes:
            if row["path"] in PUBLIC_PATHS or "GET" not in row["methods"]:
                continue
            probe = row["path"]
            for ph in ("{device_id}", "{tag_id}", "{alarm_id}", "{template_id}", "{core_id}",
                       "{item_id}", "{doc_id}", "{id}", "{key}", "{code}", "{asset_id}",
                       "{order_id}", "{rule_id}", "{batch_id}", "{user_id}", "{filename}"):
                probe = probe.replace(ph, "1")
            try:
                r = client.get(probe)
            except Exception as exc:  # noqa: BLE001
                unauth_bad.append(f"{probe} 请求异常 {exc}")
                continue
            if r.status_code not in (401, 403):
                unauth_bad.append(f"{probe} -> {r.status_code}")
        if unauth_bad:
            findings.append(Finding("high", "unauth-access", f"{len(unauth_bad)} 个端点未认证可访问",
                                    "；".join(unauth_bad[:8])))

        # 6) 普通用户越权（超管端点必须 403）
        regular = "secaudit_probe"
        pwd = "SecAudit!2026#Probe"
        created_id = None
        r = client.post("/api/v1/system/users",
                        json={"username": regular, "password": pwd, "full_name": "security audit probe",
                              "is_superuser": False}, headers=headers)
        if r.status_code == 201:
            created_id = (r.json() or {}).get("id")
        code, body = _login(client, regular, pwd)
        if code == 200:
            rtoken = body.get("access_token") or ""
            rheaders = {"Authorization": f"Bearer {rtoken}"}
            superuser_eps = [
                ("GET", "/api/v1/system/users"),
                ("GET", "/api/v1/system/audit-logs"),
                ("POST", "/api/v1/system/users"),
                ("GET", "/api/v1/external/sources"),
                ("POST", "/api/v1/ai/cores/scan-local"),
                ("DELETE", "/api/v1/ai/cores/__audit_probe__"),
            ]
            leaked = []
            for method, ep in superuser_eps:
                r = client.request(method, ep, headers=rheaders,
                                   json={"username": "x", "password": "Y!2026#abcdef"} if method == "POST" else None)
                if r.status_code not in (401, 403, 404, 405, 409, 422):
                    leaked.append(f"{method} {ep} -> {r.status_code}")
            if leaked:
                findings.append(Finding("high", "rbac", "普通用户可访问超管端点", "；".join(leaked)))
        else:
            findings.append(Finding("medium", "rbac-probe", f"无法创建审计用普通用户（HTTP {code}），越权检查未执行"))

        # 清理探针账号，避免审计本身污染用户表
        if created_id is not None:
            client.delete(f"/api/v1/system/users/{created_id}", headers=headers)

        # 7) 安全响应头
        r = client.get("/health")
        h = {k.lower(): v for k, v in r.headers.items()}
        required = {
            "x-content-type-options": "nosniff",
            "x-frame-options": None,
            "content-security-policy": None,
            "referrer-policy": None,
        }
        missing = [k for k in required if k not in h]
        if missing:
            findings.append(Finding("medium", "security-headers", "缺少安全响应头",
                                    "、".join(missing)))

        # 8) 接口文档暴露：生产环境必须关闭，开发环境开放属预期
        env_name = ""
        try:
            env_name = str(client.get("/health").json().get("env") or "")
        except Exception:  # noqa: BLE001
            pass
        r = client.get("/openapi.json")
        if r.status_code == 200 and env_name.lower() in ("production", "prod"):
            findings.append(Finding("high", "openapi-exposed", "生产模式下 /openapi.json 仍可匿名访问",
                                    "生产必须关闭 /docs 与 /openapi.json"))
        elif r.status_code == 200:
            findings.append(Finding("info", "openapi-dev-only",
                                    f"/openapi.json 可匿名访问（APP_ENV={env_name or 'unknown'}，开发环境属预期）",
                                    "生产模式（APP_ENV=production）已关闭，发布验收需在 production 下复核"))

        # 9) 登录节流：连续失败应出现 429
        # 用不存在的用户名试，只有 6~8 次——只触发"按用户名"的锁定（默认 5 次/15 分钟），
        # 不会把本机 IP 的 20 次配额打满，避免审计把后续测试全锁死。
        codes = []
        for _ in range(8):
            c, _ = _login(client, "__audit_throttle_probe__", "wrong-password-for-throttle")
            codes.append(c)
            if c == 429:
                break
        if 429 not in codes:
            findings.append(Finding("medium", "login-throttle",
                                    "连续 8 次登录失败未触发 429", "登录节流可能未生效"))
    return findings


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AIMS 发布前安全审计")
    ap.add_argument("--api", default=os.environ.get("AIMS_AUDIT_API", "http://127.0.0.1:8000"))
    ap.add_argument("--admin", default=os.environ.get("AIMS_AUDIT_USER", "admin"))
    ap.add_argument("--admin-password", default=os.environ.get("AIMS_AUDIT_PASSWORD", ""))
    ap.add_argument("--json", default="")
    ap.add_argument("--static-only", action="store_true", help="只做静态路由审计（不连实例）")
    args = ap.parse_args(argv)

    report = Report()
    routes, static_findings = scan_routes()
    report.routes = routes
    report.findings.extend(static_findings)
    no_auth = [r["path"] for r in routes if not r["auth"] and r["path"] not in PUBLIC_PATHS]
    report.check("静态：业务路由认证覆盖", not no_auth,
                 f"{len(routes)} 个路由，{len(no_auth)} 个无认证依赖：{no_auth[:5]}")

    if not args.static_only:
        if not args.admin_password:
            print("需要 --admin-password（或环境变量 AIMS_AUDIT_PASSWORD）才能做动态检查", file=sys.stderr)
            return 2
        dyn = dynamic_checks(args.api, args.admin, args.admin_password)
        report.findings.extend(dyn)
        report.check("动态：未认证/越权/令牌/头/节流",
                     not any(f.level == "high" for f in dyn),
                     f"{len(dyn)} 项发现，其中高危 {sum(1 for f in dyn if f.level == 'high')}")

    print("=" * 74)
    print(" AIMS 安全审计")
    print("=" * 74)
    print(f" 路由总数：{len(routes)}；受保护：{sum(1 for r in routes if r['auth'])}；"
          f"公开白名单：{sum(1 for r in routes if not r['auth'])}")
    for f in report.findings:
        mark = {"high": "[高危]", "medium": "[中危]", "low": "[低危]", "info": "[信息]"}[f.level]
        print(f" {mark} {f.title}")
        if f.detail:
            print(f"        {f.detail}")
    if not report.findings:
        print(" 未发现问题。")
    print("-" * 74)
    print(f" 结论：高危 {len(report.high)} / 中危 {len(report.medium)} / 合计 {len(report.findings)}")
    print("=" * 74)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "api": args.api,
            "route_count": len(routes),
            "protected": sum(1 for r in routes if r["auth"]),
            "routes": routes,
            "checks": report.checks,
            "findings": [f.as_dict() for f in report.findings],
            "high": len(report.high),
            "medium": len(report.medium),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f" 报告：{args.json}")

    return 1 if report.high else 0


if __name__ == "__main__":
    raise SystemExit(main())
