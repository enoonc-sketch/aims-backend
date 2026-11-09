# AIMS Backend 0.5.2 — 性能与体积优化（报告优先级 1–12）

实施日期：2026-11-09。依据为当日只读分析报告（0.5.2，201 个文件），
报告优先级表中的 1–6 项全部是“安全可做”，先实施并验证；7–12 项
原需用户决策，用户随后把报告原文重新发来作为继续做的指令，本轮
按报告建议值实施，其中改行为的项一律保留开关、默认行为尽量接近
原版（见下表“默认”一列）。

## 改了什么

| # | 优化 | 改动位置 |
|---|---|---|
| 1 | 设备列表 N+1：`select(Device)` → `select(Device).options(selectinload(Device.components))` | `app/api/v1/devices.py:52` |
| 2 | `/dashboard/stats` 4 条状态 COUNT 并成 1 条 `GROUP BY status`（整接口 6 条 SQL → 3 条；告警、工单是不同表，仍各 1 条） | `app/api/v1/dashboard.py:19-29` |
| 3 | llama-cpp-python 首选 0.3.2（0.3.4 在 abetlen CPU wheel 索引里不存在，实查 0 次；0.3.2 出现 438 次），fallback 0.2.90 | `build_exe.bat`、`build_onefile.bat`、`.github/workflows/build-exe.yml`、`github/workflows/build-exe.yml`、`requirements-exe-ai.txt` |
| 4 | CI 加 pip 缓存：`setup-python` 增加 `cache: pip`，缓存键覆盖 `requirements-exe.txt` + `requirements-exe.lock`；AI 安装步骤去掉 `--no-cache-dir`（否则缓存不生效） | 两个 `build-exe.yml` |
| 5 | 新增 `requirements-exe.lock`（精确版本，见下）；CI 与两个 `.bat` 改为优先用 lock 安装，`requirements-exe.txt` 保留为浮动源并作 fallback | `requirements-exe.lock`、两个 workflow、两个 `.bat` |
| 6 | `build_exe.spec` 的 `excludes` 增加 `watchfiles`（uvicorn `--reload` 监视器，EXE 以 `reload=False` 运行，从未使用，约 1.2MB）；两个 `.bat` 的命令行 fallback 构建同步排除 | `build_exe.spec`、`build_exe.bat`、`build_onefile.bat` |

第 1、2 项不改行为、不改返回结构（`DeviceOut` 不含 `model` 字段，
无需额外 eager-load；`/stats` 的 total/online/warning/fault 数值不变）。

## 锁定版本（`requirements-exe.lock`）

2026-11-09 在全新 venv（Python 3.12.3，Linux）中按
`requirements-exe.txt` 实装后 `pip freeze` 生成，与分析报告给出的
版本完全一致：fastapi 0.162.2、SQLAlchemy 2.1.8、pydantic 2.14.3、
pandas 3.1.1、numpy 2.5.4、bcrypt 5.0.0、cryptography 50.0.2、
uvicorn 0.54.0、pyinstaller 6.22.3 等。lock 文件排除了 pytest 及其
测试专用依赖（iniconfig、pluggy、Pygments），测试另外安装 pytest。
lock 在全新 venv 中 `pip install --dry-run` 解析通过，版本与 freeze 一致。
升级依赖后请重新 freeze 并跑全套测试再提交新 lock（建议每季度复核一次）。

## 验证（Linux 沙箱，Python 3.12.3）

- 冒烟脚本（50 设备 × 每设备 3 测点，SQL 事件计数）：
  `GET /devices?limit=100` **2 条 SQL**（改前 51 条），50 设备 × 3 测点
  返回完整；`GET /dashboard/stats` **3 条 SQL**（改前 6 条），
  返回 `total 50 / online 13 / warning 13 / fault 12 / alarms_today 0 /
  open_work_orders 0`，与种子数据一致；`GET /health` 200。
- 全套 pytest：**421 passed、1 skipped、4 failed**（131.76s）。
  4 个失败全部在 `tests/test_ai_cores_download.py`（下载回退/断点续传），
  报错为 `Invalid port: ':1]'`；已在**未改动的代码上复现同样 4 个失败**
  （`git stash` 后重跑，4 failed、13 passed），确认是改动前就存在的问题，
  与本次 1–6 项无关（疑似 httpx/testclient 版本组合问题，未在本轮修）。
- `build_exe.spec` 可编译；两个 workflow YAML 可解析；两个 `.bat`
  保持 CRLF 换行。

## 未做的（报告 7–12，需用户定）

- 7. `/trend` 加单测点行数上限或降采样（改行为，需定上限值/策略）
- 8. 拆分“轻量版 / 完整版”EXE（AI 栈约 240MB，完整版预计 300–400MB）
- 9. 启动后后台预热 pandas（首次导入 1.19s→约 0.2s；启动 +0.5s、常驻 +50MB）
- 10. UPX 压缩（体积预计 -30%～-40%；杀毒误报率上升）
- 11. 去掉 uvloop（省约 15MB；IO 吞吐下降约 10–30%，估算）
- 12. EXE 依赖加回 APScheduler，恢复外部数据源自动刷新
- 报告明确不建议的未动：排除 numpy/pandas、动 OpenBLAS（24MB）、改 onefile、
  加 GZip/`/ui` 缓存头（本机回环访问收益约等于零）。

## 第二批：报告 7–12（2026-11-09 同日补做）

用户把报告原文重发一遍作为继续做的指令后，按报告建议值实施。
改行为的项都有开关，默认尽量接近原版。

| # | 优化 | 默认 | 改动位置 |
|---|---|---|---|
| 7 | `/trend` 单测点上限：`?limit=` 参数，默认 5000 行/测点（上限 20000；报告建议值即 5000）。此前无上限，1 秒/点 × 24h = 86,400 行/测点一次返回。取的是**最近**的 5000 点再按时间正序返回（不是窗口开头那 5000 点） | 上限生效（改行为，超限时曲线只显示最近 5000 点） | `app/api/v1/measurements.py:311` |
| 8 | 轻量版 / 完整版拆分：`AIMS_BUILD_AI=0` 跳过 AI 栈安装（llama/sklearn/statsmodels，约 240MB），打出约 149MB 轻量版；spec 本来就容忍 AI 包缺失，构建照常成功 | 完整版（`AIMS_BUILD_AI` 未设 = 1） | `build_exe.bat`、`build_onefile.bat`、新增 `build_exe_lite.bat`（一键轻量版）、两个 `build-exe.yml`（Actions 里 `ai_stack` 选 full/lite，产物名 lite 时为 `AIMS-Backend-Lite`） |
| 9 | 启动后后台线程预热 pandas：首次导入约 1.19s→约 0.2s；代价启动 +约 0.5s、常驻 +约 50MB；预热失败不影响启动 | 独立 EXE（`run_standalone.py`）默认开；其他部署默认关（`PREHEAT_PANDAS=false` 可关） | `app/core/config.py`（`PREHEAT_PANDAS`）、`app/main.py` lifespan、`run_standalone.py`、`.env.example` |
| 10 | UPX 压缩开关：`AIMS_UPX=1` 时 spec 的 `upx=True`，预计体积 -30%～-40% | 关（杀毒误报风险，报告建议需用户定） | `build_exe.spec`、两个 `build-exe.yml`（Actions 里 `upx` 开关） |
| 11 | 去掉 uvloop：`uvicorn[standard]` → 普通 `uvicorn`，`websockets` 单独保留（中间件处理 websocket 作用域、uvicorn 仍需它）；spec 与两个 `.bat` 的命令行 fallback 同步排除 `uvloop`/`httptools`；lock 随之去掉 uvloop 0.23.0、httptools 0.9.0、watchfiles 1.3.0、PyYAML 6.0.3（后者只是 `[standard]` 的传递依赖，代码里没有任何地方 import yaml） | 生效（uvicorn 自动回退 asyncio/h11；单机部署 IO 吞吐下降约 10–30% 为估算，实测接口无变化） | `requirements-exe.txt`、`requirements-exe.lock`、`build_exe.spec`、两个 `.bat` |
| 12 | EXE 依赖加回 APScheduler 3.10.4（与 `requirements.txt` 同版本，含 tzlocal/pytz），恢复外部数据源自动刷新；此前 EXE 里 `init_all_jobs()` 静默跳过 | 生效 | `requirements-exe.txt`、`requirements-exe.lock`、两个 `.bat` 的无 lock fallback |

## 第二批验证（Linux 沙箱，Python 3.12.3，与第一批同一 venv 更新 lock 后）

- 冒烟脚本 `/tmp/smoke_perf_opt2.py`（1 设备、单测点 6000 行）：
  `GET /trend` 默认返回 **5000 点**（6000 行被截断），`?limit=100` 返回
  **100 点**；`GET /devices` 仍 **2 条 SQL**（第 1 项未退化）；
  启动后 `pandas` 已在 `sys.modules`（第 9 项预热生效）；
  `get_scheduler()` 返回运行中的 scheduler（第 12 项，APScheduler 可用）。
- 全套 pytest：**421 passed、1 skipped、4 failed**（151.77s）。
  4 个失败仍是 `tests/test_ai_cores_download.py` 那 4 个（`Invalid port: ':1]'`），
  与第一批、与未改动代码上复现的结果完全一致，改前就存在的问题。
- lock 重新 freeze（47 个包）：新增 APScheduler 3.10.4、tzlocal 5.4.5、
  pytz 2026.6；去掉 uvloop、httptools、watchfiles、PyYAML；其余版本不变
  （fastapi 0.162.2、pandas 3.1.1、uvicorn 0.54.0、websockets 17.4 等），
  `pip install --dry-run -r requirements-exe.lock` 通过。
- `build_exe.spec` 可编译；两个 workflow YAML 可解析；三个 `.bat`
  保持 CRLF 换行；`AIMS_UPX=1` 时 spec 的 `_UPX` 解析为 True，默认 False。
