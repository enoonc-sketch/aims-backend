#!/usr/bin/env python3
"""AIMS 性能与稳定性基准（阶段 9-C）。

四个可重复的子测试，全部打真实接口、真实数据库，不做模拟：

    --latency      单请求延迟分布（p50/p95/max），逐个端点
    --throughput   并发吞吐（多线程，统计 QPS 与错误率）
    --import N     导入 N 行 CSV 到 /measurements/import，测吞吐（行/秒）
    --soak SEC     混合负载持续 SEC 秒，采样后端 RSS 与线程数，判断是否持续增长

结果写 JSON，供《测试报告》引用。用法：

    python tools/perf_bench.py --api http://127.0.0.1:8000 \
        --admin admin --admin-password '***' --latency --throughput \
        --import 10000 --soak 120 --json perf.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)


def _login(client: httpx.Client, user: str, password: str) -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"username": user, "password": password})
    if r.status_code != 200:
        raise SystemExit(f"登录失败 HTTP {r.status_code}: {r.text[:200]}")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return ordered[idx]


def latency(client: httpx.Client, headers: dict[str, str], repeats: int) -> list[dict[str, Any]]:
    endpoints = [
        ("GET", "/health", "存活探针"),
        ("GET", "/api/v1/dashboard/stats", "看板统计"),
        ("GET", "/api/v1/devices?limit=50", "设备列表"),
        ("GET", "/api/v1/templates", "模板中心列表"),
        ("GET", "/api/v1/alarms?limit=50", "报警列表"),
        ("GET", "/api/v1/system/components", "组件信息"),
    ]
    rows = []
    for method, path, label in endpoints:
        samples: list[float] = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            r = client.request(method, path, headers=headers)
            dt = (time.perf_counter() - t0) * 1000
            if r.status_code >= 400:
                samples.append(float("nan"))
                continue
            samples.append(dt)
        ok = [s for s in samples if s == s]
        rows.append({
            "endpoint": path,
            "label": label,
            "status": "ok" if len(ok) == len(samples) else "partial",
            "n": len(samples),
            "p50_ms": round(_percentile(ok, 50), 1),
            "p95_ms": round(_percentile(ok, 95), 1),
            "max_ms": round(max(ok), 1) if ok else 0.0,
            "mean_ms": round(statistics.fmean(ok), 1) if ok else 0.0,
        })
    return rows


def throughput(client: httpx.Client, headers: dict[str, str], threads: int, per_thread: int) -> dict[str, Any]:
    paths = ["/api/v1/dashboard/stats", "/api/v1/devices?limit=50", "/api/v1/templates",
             "/api/v1/alarms?limit=50", "/api/v1/system/components"]
    errors: list[str] = []
    latencies: list[float] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        local_err: list[str] = []
        local_lat: list[float] = []
        for i in range(per_thread):
            path = paths[(idx + i) % len(paths)]
            t0 = time.perf_counter()
            try:
                r = client.get(path, headers=headers)
                ok = r.status_code < 400
            except Exception as exc:  # noqa: BLE001
                local_err.append(f"{path}: {exc!r}")
                continue
            local_lat.append((time.perf_counter() - t0) * 1000)
            if not ok:
                local_err.append(f"{path}: HTTP {r.status_code}")
        with lock:
            errors.extend(local_err)
            latencies.extend(local_lat)

    started = time.perf_counter()
    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    elapsed = time.perf_counter() - started
    total = threads * per_thread
    return {
        "threads": threads,
        "per_thread": per_thread,
        "requests": total,
        "ok": len(latencies),
        "errors": len(errors),
        "error_samples": errors[:5],
        "elapsed_s": round(elapsed, 2),
        "qps": round(len(latencies) / elapsed, 1) if elapsed > 0 else 0.0,
        "p95_ms": round(_percentile(latencies, 95), 1),
    }


def _ensure_device(client: httpx.Client, headers: dict[str, str], code: str = "PERF-BENCH-DEV") -> int:
    r = client.get("/api/v1/devices?limit=200", headers=headers)
    if r.status_code == 200:
        items = r.json()
        items = items.get("items", items) if isinstance(items, dict) else items
        for d in items:
            if d.get("device_code") == code:
                return int(d["id"])
    r = client.post("/api/v1/devices", headers=headers,
                    json={"device_code": code, "name": "性能基准设备", "protocol": "Modbus TCP",
                          "location": "PERF"})
    if r.status_code in (200, 201):
        return int(r.json()["id"])
    raise SystemExit(f"无法准备基准设备：HTTP {r.status_code} {r.text[:200]}")


def import_bench(client: httpx.Client, headers: dict[str, str], rows: int,
                 device_code: str = "PERF-BENCH-DEV") -> dict[str, Any]:
    device_id = _ensure_device(client, headers, device_code)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "temperature", "vibration", "pressure"])
    base = datetime(2026, 1, 1)
    for i in range(rows):
        ts = base + timedelta(minutes=i)
        w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), round(60 + (i % 10) * 0.5, 2),
                    round(1.2 + (i % 7) * 0.01, 3), round(0.8 + (i % 5) * 0.02, 3)])
    payload = buf.getvalue().encode()
    started = time.perf_counter()
    r = client.post("/api/v1/measurements/import", headers=headers,
                    files={"file": ("perf.csv", payload, "text/csv")},
                    data={"device_id": str(device_id)}, timeout=600.0)
    elapsed = time.perf_counter() - started
    ok = r.status_code in (200, 201)
    body: dict[str, Any] = {}
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        pass
    # 宽表 1 行 CSV 会展开成 N 条读数（每个测点一条），吞吐必须按"读数"算，
    # 否则会把 6 万条读数说成 2 万行，凭空少算 3 倍。
    inserted = body.get("rows_inserted") or body.get("imported") or body.get("inserted")
    per_second = round((inserted or rows) / elapsed, 1) if elapsed > 0 else 0.0
    return {
        "csv_rows": rows,
        "measurements": inserted,
        "status_code": r.status_code,
        "ok": ok,
        "elapsed_s": round(elapsed, 2),
        "rows_per_second": per_second,
        "detail": (r.text[:200] if not ok else ""),
    }


def _find_backend_pid(port: int) -> int | None:
    try:
        import psutil
    except ImportError:
        return None
    for conn in psutil.net_connections(kind="inet"):
        if conn.status == "LISTEN" and conn.laddr and conn.laddr.port == port:
            return conn.pid
    return None


def soak(client: httpx.Client, headers: dict[str, str], seconds: int, port: int) -> dict[str, Any]:
    import psutil

    pid = _find_backend_pid(port)
    proc = psutil.Process(pid) if pid else None
    samples: list[dict[str, Any]] = []
    # 注意：/measurements 必须带 device_id，缺参数会 422——那不是稳定性问题，
    # 而是脚本写错了，所以这里只用能独立访问的端点。
    paths = ["/api/v1/dashboard/stats", "/api/v1/devices?limit=50", "/api/v1/templates",
             "/api/v1/alarms?limit=50", "/api/v1/system/components"]
    errors = 0
    total = 0
    started = time.perf_counter()
    while time.perf_counter() - started < seconds:
        for path in paths:
            total += 1
            try:
                if client.get(path, headers=headers).status_code >= 400:
                    errors += 1
            except Exception:  # noqa: BLE001
                errors += 1
        if proc is not None:
            try:
                with proc.oneshot():
                    mem = proc.memory_info().rss / 1024 / 1024
                    samples.append({
                        "t_s": round(time.perf_counter() - started, 1),
                        "rss_mb": round(mem, 1),
                        "threads": proc.num_threads(),
                        "fds": proc.num_fds() if hasattr(proc, "num_fds") else None,
                    })
            except Exception:  # noqa: BLE001
                proc = None
        time.sleep(0.5)
    first = samples[0] if samples else None
    last = samples[-1] if samples else None
    growth = round(last["rss_mb"] - first["rss_mb"], 1) if first and last else None
    return {
        "duration_s": seconds,
        "requests": total,
        "errors": errors,
        "rss_start_mb": first["rss_mb"] if first else None,
        "rss_end_mb": last["rss_mb"] if last else None,
        "rss_growth_mb": growth,
        "threads_start": first["threads"] if first else None,
        "threads_end": last["threads"] if last else None,
        "samples": samples[:: max(1, len(samples) // 12)],
        # 判定：RSS 增长 < 15% 且线程数不增长，视为无泄漏迹象（不是内存分析，是回归哨兵）
        "verdict": ("stable" if (growth is not None and first and last
                                 and growth < max(20.0, 0.15 * first["rss_mb"])
                                 and last["threads"] <= first["threads"] + 2) else "investigate"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AIMS 性能与稳定性基准")
    ap.add_argument("--api", default=os.environ.get("AIMS_PERF_API", "http://127.0.0.1:8000"))
    ap.add_argument("--admin", default=os.environ.get("AIMS_PERF_USER", "admin"))
    ap.add_argument("--admin-password", default=os.environ.get("AIMS_PERF_PASSWORD", ""))
    ap.add_argument("--latency", action="store_true")
    ap.add_argument("--latency-repeats", type=int, default=30)
    ap.add_argument("--throughput", action="store_true")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--per-thread", type=int, default=50)
    ap.add_argument("--import", dest="import_rows", type=int, default=0)
    ap.add_argument("--device-code", default="PERF-BENCH-DEV",
                    help="导入基准使用的设备编号；换个编号即可测\"首次导入\"吞吐")
    ap.add_argument("--soak", type=int, default=0)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    if not args.admin_password:
        print("需要 --admin-password（或环境变量 AIMS_PERF_PASSWORD）", file=sys.stderr)
        return 2

    report: dict[str, Any] = {"api": args.api, "started_at": datetime.now().isoformat(timespec="seconds")}
    port = int(httpx.URL(args.api).port or 80)
    with httpx.Client(base_url=args.api, timeout=120.0) as client:
        headers = _login(client, args.admin, args.admin_password)
        if args.latency:
            report["latency"] = latency(client, headers, args.latency_repeats)
        if args.throughput:
            report["throughput"] = throughput(client, headers, args.threads, args.per_thread)
        if args.import_rows:
            report["import"] = import_bench(client, headers, args.import_rows, args.device_code)
        if args.soak:
            report["soak"] = soak(client, headers, args.soak, port)

    print("=" * 78)
    print(" AIMS 性能基准")
    print("=" * 78)
    for row in report.get("latency", []):
        print(f"  {row['label']:12} {row['endpoint'][:44]:46} p50 {row['p50_ms']:7.1f}ms  "
              f"p95 {row['p95_ms']:7.1f}ms  max {row['max_ms']:7.1f}ms")
    if "throughput" in report:
        t = report["throughput"]
        print(f"  并发吞吐  {t['threads']} 线程 × {t['per_thread']} 次 = {t['requests']} 请求  "
              f"{t['qps']} QPS  错误 {t['errors']}  p95 {t['p95_ms']}ms")
    if "import" in report:
        i = report["import"]
        print(f"  导入性能  CSV {i['csv_rows']} 行 → 读数 {i['measurements']} 条  {i['elapsed_s']}s  "
              f"{i['rows_per_second']} 条/秒  HTTP {i['status_code']}")
    if "soak" in report:
        s = report["soak"]
        print(f"  稳定性    {s['duration_s']}s / {s['requests']} 请求  错误 {s['errors']}  "
              f"RSS {s['rss_start_mb']}→{s['rss_end_mb']}MB（{s['rss_growth_mb']:+}）  "
              f"线程 {s['threads_start']}→{s['threads_end']}  判定 {s['verdict']}")
    print("=" * 78)

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f" 报告：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
