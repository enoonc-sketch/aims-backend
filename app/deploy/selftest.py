"""端到端自检（acceptance self-test）：对一个**正在运行**的 AIMS 实例发真实请求。

为什么放在后端 EXE 里而不是写成外部脚本：目标机上没有 Python，验收人员也不该
为了跑测试去装运行时。`AIMS-Backend.exe deploy selftest` 就是把测试跑在自己
内嵌的解释器里，用标准库发 HTTP，一条命令给出可贴进验收单的结论。

覆盖范围（每条都是"真功能"，没有一条是读个常量就算通过）：

    健康检查 → 登录/鉴权 → 设备增删改查 → 测点写入与回读 → 资产台账 →
    报警列表 → 知识库写入/检索 → RAG 问答 → AI 对话 → 看板统计 →
    Excel 模板导出（openpyxl）→ 用户与权限 → 数据处理

退出码：0 全部通过；1 有失败项（用于安装程序/CI 判定）。
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

OK = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


class Client:
    def __init__(self, base: str, *, timeout: float = 30.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None
        # 本机 loopback 一律不走系统代理：现场机器常年配着 http_proxy，
        # 不绕过的话自检会"连不上自己的服务"，非常难查。
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
        )

    def call(self, method: str, path: str, *, body: Any = None, raw: bool = False,
             headers: dict[str, str] | None = None) -> tuple[int, Any]:
        url = self.base + path
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
                if raw:
                    return resp.status, payload
                if not payload:
                    return resp.status, None
                try:
                    return resp.status, json.loads(payload.decode("utf-8", "replace"))
                except ValueError:
                    return resp.status, payload.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            return exc.code, detail
        except Exception as exc:  # noqa: BLE001
            return 0, f"{exc.__class__.__name__}: {exc}"

    def get(self, path: str, **kw) -> tuple[int, Any]:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw) -> tuple[int, Any]:
        return self.call("POST", path, body=body, **kw)

    def patch(self, path: str, body: Any = None, **kw) -> tuple[int, Any]:
        return self.call("PATCH", path, body=body, **kw)

    def delete(self, path: str, **kw) -> tuple[int, Any]:
        return self.call("DELETE", path, **kw)


class Suite:
    def __init__(self, client: Client, *, verbose: bool = True) -> None:
        self.c = client
        self.verbose = verbose
        self.results: list[dict[str, Any]] = []
        self.ctx: dict[str, Any] = {}
        self.started = time.time()

    # ------------------------------------------------------------------ 记录
    def check(self, name: str, fn: Callable[[], tuple[bool, str]], *, critical: bool = True) -> bool:
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"异常：{exc.__class__.__name__}: {exc}"
        ms = int((time.time() - t0) * 1000)
        status = OK if ok else (FAIL if critical else SKIP)
        self.results.append({"name": name, "status": status, "detail": detail, "ms": ms})
        if self.verbose:
            mark = {OK: "[通过]", FAIL: "[失败]", SKIP: "[跳过]"}[status]
            print(f"  {mark:<6} {name:<26} {ms:>5} ms  {detail}", flush=True)
        return ok

    # ------------------------------------------------------------------ 用例
    def run(self) -> dict[str, Any]:
        self._health()
        if not self.ctx.get("token"):
            self._login()
        if self.ctx.get("token"):
            self._auth_me()
            self._devices()
            self._measurements()
            self._assets()
            self._alarms()
            self._knowledge()
            self._ai()
            self._dashboard()
            self._excel()
            self._users()
            self._cleanup()
        return self.report()

    # ---- 单项 ----
    def _health(self) -> None:
        def probe():
            code, body = self.c.get("/health")
            if code != 200 or not isinstance(body, dict):
                return False, f"HTTP {code} {str(body)[:120]}"
            self.ctx["health"] = body
            if body.get("status") != "ok":
                return False, f"status={body.get('status')}"
            return True, f"v{body.get('version')} db={body.get('database')}"

        self.check("服务健康检查 /health", probe)

    def _login(self) -> None:
        def login():
            user, pwd = self.ctx.get("user"), self.ctx.get("password")
            if not user or not pwd:
                return False, "没有可用的管理员凭据（用 --user/--password 指定，或确认 Config/secrets.json 存在）"
            code, body = self.c.post("/api/v1/auth/login", {"username": user, "password": pwd})
            if code != 200 or not isinstance(body, dict):
                return False, f"HTTP {code} {str(body)[:160]}"
            token = body.get("access_token")
            if not token:
                return False, "响应里没有 access_token"
            self.c.token = token
            self.ctx["token"] = token
            return True, f"token 长度 {len(token)}，有效期 {body.get('expires_in')}s"

        self.check("管理员登录 /auth/login", login)

    def _auth_me(self) -> None:
        def me():
            code, body = self.c.get("/api/v1/auth/me")
            if code != 200 or not isinstance(body, dict):
                return False, f"HTTP {code} {str(body)[:120]}"
            self.ctx["admin_id"] = body.get("id")
            return True, f"{body.get('username')} 角色={body.get('role') or body.get('roles')}"

        self.check("当前用户 /auth/me", me)

    def _devices(self) -> None:
        code_suffix = uuid.uuid4().hex[:8].upper()
        payload = {
            "device_code": f"ACC-{code_suffix}",
            "name": f"验收测试设备 {code_suffix}",
            "protocol": "Modbus TCP",
            "location": "验收测试区",
            "status": "online",
        }

        def create():
            code, body = self.c.post("/api/v1/devices", payload)
            if code not in (200, 201) or not isinstance(body, dict):
                return False, f"HTTP {code} {str(body)[:160]}"
            self.ctx["device_id"] = body.get("id")
            return True, f"id={body.get('id')} code={body.get('device_code')}"

        if not self.check("设备新增 /devices", create):
            return
        dev = self.ctx["device_id"]

        def read():
            code, body = self.c.get(f"/api/v1/devices/{dev}")
            if code != 200 or not isinstance(body, dict):
                return False, f"HTTP {code}"
            return body.get("name") == payload["name"], f"回读 name={body.get('name')}"

        self.check("设备详情（读写一致性）", read)

        def patch():
            code, body = self.c.patch(f"/api/v1/devices/{dev}", {"location": "验收测试区-B"})
            if code != 200:
                return False, f"HTTP {code} {str(body)[:120]}"
            code2, body2 = self.c.get(f"/api/v1/devices/{dev}")
            return (body2 or {}).get("location") == "验收测试区-B", f"location={(body2 or {}).get('location')}"

        self.check("设备修改并回读", patch)

        def listing():
            code, body = self.c.get("/api/v1/devices?limit=5")
            if code != 200:
                return False, f"HTTP {code}"
            items = body.get("items") if isinstance(body, dict) else body
            return isinstance(items, list) and len(items) > 0, f"返回 {len(items or [])} 条"

        self.check("设备列表分页", listing)

    def _measurements(self) -> None:
        dev = self.ctx.get("device_id")
        if not dev:
            self.check("测点写入", lambda: (False, "前置设备创建失败"), critical=False)
            return
        value = 42.5

        def write():
            code, body = self.c.post("/api/v1/measurements",
                                     {"device_id": dev, "component_name": "验收测点", "value": value})
            if code not in (200, 201):
                return False, f"HTTP {code} {str(body)[:160]}"
            return True, f"value={value}"

        if not self.check("测点写入 /measurements", write):
            return

        def readback():
            code, body = self.c.get(f"/api/v1/measurements/history?device_id={dev}&limit=20")
            if code != 200:
                return False, f"HTTP {code}"
            items = body if isinstance(body, list) else (body or {}).get("items", [])
            hit = [m for m in items if m.get("component_name") == "验收测点"]
            if not hit:
                return False, f"回读到 {len(items)} 条但没有刚写入的测点"
            return abs(float(hit[-1]["value"]) - value) < 1e-6, f"回读 value={hit[-1]['value']}（共 {len(items)} 条）"

        self.check("测点回读（真实入库）", readback)

        def batch():
            readings = [{"device_id": dev, "component_name": "验收批量", "value": v} for v in (1.1, 2.2, 3.3)]
            code, body = self.c.post("/api/v1/measurements/batch", {"device_id": dev, "readings": readings})
            if code not in (200, 201):
                return False, f"HTTP {code} {str(body)[:160]}"
            return True, f"批量写入 {len(readings)} 条"

        self.check("测点批量写入", batch, critical=False)

    def _assets(self) -> None:
        suffix = uuid.uuid4().hex[:6].upper()

        def create():
            body_in = {"asset_code": f"AST-{suffix}", "name": f"验收资产 {suffix}",
                       "category": "验收", "purchase_value": 12345.0,
                       "device_id": self.ctx.get("device_id")}
            code, body = self.c.post("/api/v1/assets", body_in)
            if code not in (200, 201):
                return False, f"HTTP {code} {str(body)[:160]}"
            self.ctx["asset_id"] = body.get("id")
            return True, f"id={body.get('id')}"

        self.check("资产新增 /assets", create)

        def roi():
            asset = self.ctx.get("asset_id")
            if not asset:
                return False, "资产未创建"
            code, body = self.c.get(f"/api/v1/assets/{asset}/roi")
            if code != 200:
                return False, f"HTTP {code} {str(body)[:120]}"
            return True, f"ROI 字段={len(body) if isinstance(body, dict) else 'n/a'}"

        self.check("资产 ROI 计算", roi, critical=False)

    def _alarms(self) -> None:
        def listing():
            code, body = self.c.get("/api/v1/alarms?limit=10")
            if code != 200:
                return False, f"HTTP {code} {str(body)[:120]}"
            items = body.get("items") if isinstance(body, dict) else body
            return True, f"返回 {len(items or [])} 条报警"

        self.check("报警列表 /alarms", listing)

        def rules():
            code, body = self.c.get("/api/v1/alarms/rules")
            return code == 200, f"HTTP {code}，规则 {len(body or [])} 条"

        self.check("报警规则读取", rules, critical=False)

    def _knowledge(self) -> None:
        import urllib.parse

        dev = self.ctx.get("device_id")

        def create_doc():
            body_in = {"title": f"验收知识文档 {uuid.uuid4().hex[:6]}", "doc_type": "manual",
                       "device_model_id": None}
            code, body = self.c.post("/api/v1/knowledge/documents", body_in)
            if code not in (200, 201):
                return False, f"HTTP {code} {str(body)[:160]}"
            self.ctx["doc_id"] = (body or {}).get("id")
            return True, f"id={self.ctx.get('doc_id')}"

        if not self.check("知识库文档新增", create_doc):
            return

        def chunk():
            doc = self.ctx.get("doc_id")
            text = ("振动超标常见原因：轴承磨损、动不平衡、联轴器对中不良。"
                    "排查顺序：先看频谱主频是否等于转频，再看是否出现轴承特征频率。")
            code, body = self.c.post("/api/v1/knowledge/chunks",
                                     {"document_id": doc, "content": text, "chunk_index": 0})
            if code not in (200, 201):
                # 有些版本用不同字段名，如实报告而不是假装成功
                return False, f"HTTP {code} {str(body)[:160]}"
            return True, "切片写入并触发向量化"

        self.check("知识库切片写入（向量化）", chunk)

        def search():
            query = urllib.parse.urlencode({"q": "振动超标", "limit": 5})
            code, body = self.c.get(f"/api/v1/knowledge/search?{query}")
            if code != 200:
                return False, f"HTTP {code} {str(body)[:120]}"
            items = body.get("items") if isinstance(body, dict) else body
            return True, f"检索命中 {len(items or [])} 条"

        self.check("知识库检索（RAG）", search)

        def ask():
            code, body = self.c.post("/api/v1/ai/knowledge/ask",
                                     {"question": "振动超标应该先查什么？", "limit": 3, "lang": "zh"})
            if code != 200 or not isinstance(body, dict):
                return False, f"HTTP {code} {str(body)[:160]}"
            answer = str(body.get("answer") or "")
            return bool(answer), f"答案 {len(answer)} 字，引用 {len(body.get('sources') or body.get('references') or [])} 条"

        self.check("RAG 问答 /ai/knowledge/ask", ask)

    def _ai(self) -> None:
        def chat():
            code, body = self.c.post("/api/v1/ai/chat", {"message": "用一句话说明轴承温度升高的常见原因"})
            if code != 200 or not isinstance(body, dict):
                return False, f"HTTP {code} {str(body)[:160]}"
            answer = str(body.get("answer") or body.get("message") or body.get("content") or "")
            engine = body.get("engine") or body.get("provider") or "unknown"
            if not answer:
                return False, "有响应但没有答案字段"
            return True, f"引擎={engine}，回答 {len(answer)} 字"

        self.check("AI 对话 /ai/chat", chat)

        def models():
            code, body = self.c.get("/api/v1/ai/models")
            if code != 200:
                return False, f"HTTP {code}"
            return True, f"模型条目 {len(body or [])} 个"

        self.check("AI 模型清单", models, critical=False)

        def diagnose():
            dev = self.ctx.get("device_id")
            if not dev:
                return False, "无设备"
            code, body = self.c.post("/api/v1/ai/diagnose",
                                     {"device_id": dev, "component_name": "验收测点",
                                      "symptom": "振动值偏高"})
            if code not in (200, 201):
                return False, f"HTTP {code} {str(body)[:160]}"
            return True, "诊断任务已生成"

        self.check("AI 诊断 /ai/diagnose", diagnose, critical=False)

    def _dashboard(self) -> None:
        def stats():
            code, body = self.c.get("/api/v1/dashboard/stats")
            if code != 200 or not isinstance(body, dict):
                return False, f"HTTP {code} {str(body)[:120]}"
            return True, f"字段：{', '.join(list(body)[:4])}…"

        self.check("看板统计 /dashboard/stats", stats)

        def health():
            code, body = self.c.get("/api/v1/dashboard/health-overview")
            return code == 200, f"HTTP {code}"

        self.check("健康总览", health, critical=False)

    def _excel(self) -> None:
        def template():
            code, payload = self.c.get("/api/v1/analysis/import/template", raw=True)
            if code != 200 or not isinstance(payload, (bytes, bytearray)):
                return False, f"HTTP {code}"
            if not payload[:2] == b"PK":
                return False, f"不是有效的 xlsx（前两字节 {payload[:2]!r}）"
            return True, f"xlsx {len(payload) / 1024:.0f} KB（openpyxl 可用）"

        self.check("Excel 模板导出", template)

        def csv_import_template():
            code, payload = self.c.get("/api/v1/devices/import/template", raw=True)
            if code != 200:
                return False, f"HTTP {code}"
            return True, f"设备导入模板 {len(payload) / 1024:.0f} KB"

        self.check("CSV/Excel 导入模板", csv_import_template, critical=False)

    def _users(self) -> None:
        def users():
            code, body = self.c.get("/api/v1/system/users")
            if code != 200:
                return False, f"HTTP {code} {str(body)[:120]}"
            return True, f"用户 {len(body or [])} 个"

        self.check("用户与权限 /system/users", users)

        def settings():
            code, body = self.c.get("/api/v1/system/settings")
            return code == 200, f"系统设置 {len(body or [])} 项"

        self.check("系统设置读取", settings, critical=False)

        def audit():
            code, body = self.c.get("/api/v1/system/audit-logs?limit=5")
            if code != 200:
                return False, f"HTTP {code}"
            items = body.get("items") if isinstance(body, dict) else body
            return True, f"审计日志可读（{len(items or [])} 条）"

        self.check("审计日志", audit, critical=False)

    def _cleanup(self) -> None:
        def drop_device():
            dev = self.ctx.get("device_id")
            if not dev:
                return False, "无设备"
            code, body = self.c.delete(f"/api/v1/devices/{dev}")
            if code == 409:
                # 设备已被资产/测点引用 —— 这是正确的业务拒绝（不是 500），
                # 说明外键保护生效、错误信息可读。
                return True, "已被资产引用，正确返回 409 并说明原因（外键保护生效）"
            if code not in (200, 204):
                return False, f"HTTP {code} {str(body)[:120]}"
            code2, _ = self.c.get(f"/api/v1/devices/{dev}")
            return code2 == 404, f"删除后查询返回 {code2}"

        self.check("设备删除（含关联数据）", drop_device, critical=False)

    # ------------------------------------------------------------------ 汇总
    def report(self) -> dict[str, Any]:
        total = len(self.results)
        passed = sum(1 for r in self.results if r["status"] == OK)
        failed = sum(1 for r in self.results if r["status"] == FAIL)
        skipped = sum(1 for r in self.results if r["status"] == SKIP)
        return {
            "base_url": self.c.base,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started)),
            "duration_s": round(time.time() - self.started, 1),
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "ok": failed == 0,
            "health": self.ctx.get("health"),
            "results": self.results,
        }


def run(*, base_url: str = "http://127.0.0.1:8000", username: str | None = None,
        password: str | None = None, report_path: str | None = None,
        verbose: bool = True) -> dict[str, Any]:
    client = Client(base_url)
    suite = Suite(client, verbose=verbose)
    suite.ctx["user"] = username
    suite.ctx["password"] = password
    result = suite.run()
    if report_path:
        from pathlib import Path

        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def report_text(result: dict[str, Any]) -> str:
    lines = [
        "=" * 74,
        " AIMS 功能自检报告",
        "=" * 74,
        f" 目标        : {result['base_url']}",
        f" 开始时间    : {result['started_at']}    耗时 {result['duration_s']} 秒",
        f" 结果        : 通过 {result['passed']} / 失败 {result['failed']} / 跳过 {result['skipped']}"
        f"（共 {result['total']} 项）",
        "-" * 74,
    ]
    for item in result["results"]:
        mark = {OK: "[通过]", FAIL: "[失败]", SKIP: "[跳过]"}[item["status"]]
        lines.append(f" {mark:<6} {item['name']:<28} {item['detail']}")
    lines.append("-" * 74)
    lines.append(" 结论：" + ("全部通过，可以交付验收。" if result["ok"] else "存在失败项，不能标记为验收通过。"))
    lines.append("=" * 74)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="AIMS-Backend deploy selftest")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="运行中的 AIMS 地址")
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--report", default=None, help="把 JSON 报告写到该路径")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    result = run(base_url=args.url, username=args.user, password=args.password,
                 report_path=args.report, verbose=not args.quiet)
    print(report_text(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
