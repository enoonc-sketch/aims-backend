# 0.5.3 — 补改（基于 0.5.2 性能优化版，提交 740dc04）

## 本版 8 处改动

1. 删除 pandas 预热：移除 `PREHEAT_PANDAS` 配置项、`app/main.py` lifespan 里的后台线程、`run_standalone.py` 的默认开启、`.env.example` 的说明；该功能不做。
2. 轻量版改为默认：`build_exe.bat` / `build_onefile.bat` 仅在 `AIMS_BUILD_AI=1` 时安装 AI 栈（未设置、为 0 或为空都是轻量版）；`build-exe.yml` 的 `ai_stack` 默认改为 `lite`。
3. `app/services/py_packages.py` 的 llama CPU 条目去掉 0.3.4（CPU 索引没有该版本）；cu121 / cu124 条目不动。
4. 删除 UPX 开关：`build_exe.spec` 的 `AIMS_UPX` / `_UPX` 去掉，两处回到 `upx=False`；workflow 的 `upx` 输入一并删除。
5. `build_exe.spec` 的 `hiddenimports` 显式声明 `apscheduler`、`apscheduler.schedulers.background`、`apscheduler.triggers.interval`。
6. 补回回归测试 `tests/test_performance_opts.py`（守护设备列表 N+1、仪表盘 GROUP BY、`/trend` 上限）；其中 `/trend` 的参数名按本版实际的 `limit` 写。
7. 清理：删除 `.pytest_cache/` 与误放的 `github/` 目录；版本号 0.5.2 → 0.5.3（`app/main.py` 三处）；新增本文件。
8. `build-exe.yml`：`upload-artifact@v5` → `@v7`；"Install dependencies" 与 "Download open-source cores" 两步加 `PYTHONUTF8: "1"`；示例标签改为 v0.5.3。

## 沿用 0.5.2 性能优化（未改动）

设备列表 `selectinload`、`/dashboard/stats` GROUP BY、llama 首选 0.3.2、CI pip 缓存、`requirements-exe.lock`、排除 `watchfiles`、`/trend` 默认 5000 点上限、轻量版/完整版拆分机制、去掉 uvloop、EXE 加回 APScheduler。详见 `CHANGES_PERF_OPT_0_5_2.md`（该历史文件里仍会提到已被本版移除的预热与 UPX 开关）。
