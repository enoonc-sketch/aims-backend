# AIMS Backend — Industrial Analyzer Smart O&M Platform

> **Windows 离线安装版（0.5.10）**
>
> 本仓库现在同时是「Windows 桌面独立软件」的源码：单文件安装包、零外部依赖、
> 内置 PostgreSQL 16 + pgvector + 本地 AI 引擎、桌面托盘启动器、一键备份/恢复、
> 覆盖升级与安全卸载。
>
> | 我该看哪个文档 | 内容 |
> |---|---|
> | [docs/部署说明.md](docs/部署说明.md) | 目录布局、安装/升级/卸载、配置项、ARM64 与 TimescaleDB 的真实情况 |
> | [docs/用户手册.md](docs/用户手册.md) | 终端用户：启动、托盘、登录、备份恢复 |
> | [docs/故障排查.md](docs/故障排查.md) | 症状 → 原因 → 处理 |
> | [docs/功能对照表.md](docs/功能对照表.md) | 136 个接口逐模块对照，证明功能零删减 |
> | [docs/依赖与许可证清单.md](docs/依赖与许可证清单.md) · [LICENSES.txt](LICENSES.txt) | 组件、版本、许可证 |
> | [docs/测试报告.md](docs/测试报告.md) | 已执行的测试与证据，以及**尚未执行**的部分 |
>
> 快速上手（构建机）：
>
> ```powershell
> cd build; .\build_x64.ps1          # 依赖 → 内置PG → 打包 → 冒烟 → 出安装包
> .\package_release.ps1 -Installer ..\installer_output\AIMS-0.5.10-x64-Setup.exe
> ```
>
> 快速上手（目标机，无需任何开发环境）：
>
> ```
> 双击 AIMS-0.5.10-x64-Setup.exe → 桌面 AIMS 图标 → 托盘菜单
> 运维：C:\Program Files\AIMS\tools\*.ps1（备份/恢复/诊断/卸载）
> 验收：tools\Windows-Acceptance.ps1 -Setup <安装包>
> ```
>
> 安装布局：程序在 `C:\Program Files\AIMS`，数据在 `C:\ProgramData\AIMS`
> （Config / Database / Models / Logs / Uploads / Backup），升级与卸载默认都不动数据。


后端基础框架，对应前端原型 `AIMS_完整版.html`（Dashboard / Devices / Comm / Data /
Data Analysis / AI Center / Knowledge / Maintenance / Assets / Lab / System 等模块）。

## 技术栈

| 组件 | 说明 |
|---|---|
| **FastAPI** | Web 框架，自动生成 OpenAPI 文档 (`/docs`) |
| **PostgreSQL 16** | 主数据库 |
| **TimescaleDB** | `measurements` 表转换为 hypertable，用于分析仪时序数据 |
| **pgvector** | `knowledge_chunks.embedding` 向量列，支撑 AI Center 的知识库 RAG 检索 |
| **SQLAlchemy 2.0** | ORM |
| **Docker Compose** | 一键启动 db + api + adminer |

## 目录结构

```
aims-backend/
├── docker-compose.yml       # api / db(自定义镜像) / adminer 三个服务
├── Dockerfile                # FastAPI 应用镜像
├── requirements.txt
├── .env.example               # 复制为 .env 后按需修改
├── db/
│   ├── Dockerfile             # postgres:16 + TimescaleDB + pgvector
│   └── init/
│       └── 00_extensions.sql  # CREATE EXTENSION timescaledb / vector / ...
│                                （表结构由 app/models/ + migrations/ 定义，见下）
├── app/
│   ├── main.py                 # FastAPI 入口
│   ├── core/config.py          # 环境变量配置（pydantic-settings）
│   ├── db/session.py           # SQLAlchemy engine / Session / get_db 依赖
│   ├── models/                 # ORM 模型，按业务域拆分为 8 个文件，共 35 张表
│   ├── schemas/                # Pydantic 请求/响应模型
│   └── api/v1/                 # dashboard / devices / measurements / alarms / ai 路由
└── tests/                       # pytest 集成测试（对应 5 个核心 API 模块）
```

## 35 张核心业务表（按业务域）

| 业务域 | 表数 | 表名 |
|---|---|---|
| System 系统 | 7 | users, roles, user_roles, permissions, role_permissions, system_settings, audit_logs |
| Device 设备 | 6 | device_models, devices, device_components, gateways, gateway_devices, device_calibration_records |
| Measurement 测量（Timescale hypertable） | 2 | measurement_imports, measurements |
| Alarm 报警 | 2 | alarm_rules, alarms |
| Maintenance 运维 | 4 | work_orders, maintenance_records, maintenance_sop, spare_parts |
| Asset 资产 | 2 | assets, asset_documents |
| Lab 实验室比对 | 2 | lab_samples, lab_correlation_results |
| Knowledge 知识库（pgvector） | 2 | knowledge_documents, knowledge_chunks |
| AI Center | 5 | ai_diagnoses, ai_predictions, ai_health_scores, ai_chat_sessions, ai_chat_messages |
| Analysis / Dashboard | 3 | data_analysis_datasets, dashboard_snapshots, notifications |

详细字段定义见 `app/models/`（唯一权威来源，见下方"数据库迁移"一节）。

## 已实现的核心 API（对应原型页面）

对应原型的 **Dashboard / Devices / Data / AI Center / (Alarms 集成在 Comm/Dashboard 中)** 页面，
`app/api/v1/` 下实现了 5 个模块：

- `GET /api/v1/dashboard/stats` — 首页 KPI（设备总数/在线/告警数/工单数…）
- `GET /api/v1/dashboard/health-overview` — 设备健康度图表数据
- `GET|POST|PATCH|DELETE /api/v1/devices` — 设备 CRUD + 组件列表
- `GET|POST /api/v1/measurements`、`POST /api/v1/measurements/batch`、`GET /api/v1/measurements/trend`
  — 时序数据写入/查询/趋势（对应 Data / Data Analysis 页面）
- `GET|POST /api/v1/alarms`、`/acknowledge`、`/resolve`、`/rules` — 告警列表与规则
- `POST /api/v1/ai/diagnose`、`/predict`、`GET /ai/health/{id}`、`POST /ai/chat`
- AI 中心 ↔ 其他菜单联动（设备搜索 / 选型）：`GET /ai/devices/search`、`POST /ai/devices/select`、`GET /ai/devices/{id}/context`、`GET /ai/devices/facets`；`POST /ai/chat` 可带 `device_id`。详见 `CHANGES_AI_DEVICE_BINDING.md`
- AI Studio（维护 SOP/ROI 由 AI 生成、数据分析 AI 读取、知识库 AI 问答、实验室由数据分析数据驱动的比对与 AI 认证）：`POST /ai/sop/generate`、`POST /ai/roi/generate`、`POST /ai/analysis/read`、`POST /ai/knowledge/ask`、`POST /lab/analysis-compare`、`POST /lab/certify`、`GET /lab/certifications`。详见 `CHANGES_AI_STUDIO.md`
- AI 选型 ↔ AI Studio 打通：选型评分与型号推荐纳入 AI 认证结果与成本健康度；AI 中心 360 面板新增“化验认证”“ROI/维护成本健康度”两个跳转项；认证未通过/有条件通过时可一键创建工单；`GET /lab/certifications?latest=true` 改为 SQL 级查询并加了索引。详见 `CHANGES_AI_SELECTION_BINDING.md`
  — AI 诊断 / 预测 / 健康评分 / 知识问答（无 `AI_API_KEY` 时使用内置规则引擎离线运行）
- `GET|POST|PATCH /api/v1/maintenance/work-orders`、`/close`、`/records`、`/sop`、`/sop/generate`、
  `/spare-parts`、`/spare-parts/{id}/adjust-stock`
  — 工单全生命周期、维护记录、SOP 库（按设备型号+类型生成）、备件库存（对应 Maintenance 页面）
- `GET|POST|PATCH|DELETE /api/v1/assets`、`/{id}/roi`、`/by-device/{id}/roi`、`/{id}/documents`
  — 资产登记、按直线折旧法计算 ROI/净值（对应 Assets 页面 + Maintenance > ROI Analysis 页签）

启动后访问 `http://localhost:8000/docs` 查看完整 OpenAPI 交互文档。

其余业务域（Lab / Knowledge / System 等）已建表并可直接在
`app/models/` 基础上按相同模式扩展 Pydantic schema + 路由。

## 快速开始（Docker Compose）

```bash
cp .env.example .env          # 按需修改数据库密码 / AI_API_KEY 等
docker compose up -d --build  # 构建并启动 db(TimescaleDB+pgvector) / api / adminer

# 首次启动：db 容器执行 db/init/00_extensions.sql 建扩展；api 容器启动时
# AUTO_INIT_DB=true 会自动从 app/models/ 建 35 张表 + 写入演示数据
# （见 app/db/init_db.py:create_tables / _seed_if_empty）
docker compose logs -f api    # 查看 API 日志
```

- API 文档: http://localhost:8000/docs
- 健康检查: http://localhost:8000/health
- Adminer(数据库管理): http://localhost:8080 （系统: PostgreSQL，服务器: db，用户名/密码见 .env）

停止并清空数据卷：

```bash
docker compose down -v
```

## 本地开发（不使用 Docker 运行 API）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 仍需要一个可用的 Postgres(+timescaledb+vector)，例如只启动 db 服务：
docker compose up -d db

export DATABASE_URL=postgresql+psycopg2://aims:aims_password@localhost:5432/aims
uvicorn app.main:app --reload
```

## 数据库迁移

**唯一权威来源是 `app/models/*.py`**（SQLAlchemy ORM 模型）。表结构不再有第二份手写
副本：早期 `db/init/01_schema.sql` 曾整份手抄一遍 35 张表定义，和模型各改各的，
已经出现过真实的字段遗漏（`device_components.gateway_id`/`tag_address` 在模型里存在、
SQL 文件里没有）——该文件与 `02_seed.sql`（同理重复 `_seed_if_empty()` 的种子数据）
已删除。TimescaleDB hypertable 转换、pgvector ivfflat 索引这些 ORM 建表语句表达不了的
Postgres 专属操作，改为在 `app/db/init_db.py:_ensure_postgres_extras()` 里用一小段
幂等 SQL 执行，紧跟在 `Base.metadata.create_all()` 之后。

项目已接入 `alembic`（`migrations/`，`target_metadata = Base.metadata`），用于给已有
数据库做增量变更（新增列等）：

```bash
alembic upgrade head                              # 应用尚未执行的迁移
alembic revision --autogenerate -m "describe change"  # 改动 app/models/ 后生成新迁移
alembic upgrade head
```

- 配置：`alembic.ini` + `migrations/env.py`
- 从零启动（新数据库、无历史数据）不需要手动跑 alembic：`AUTO_INIT_DB=true`
  时 `create_tables()` 已用 `Base.metadata.create_all()` 建好全部表，
  `migrations/versions/001_initial_schema.py` 起点也正是这份 metadata。
  alembic 主要用于**已有数据、需要增量升级列/索引**的场景。

## 测试

测试为集成测试，需要一个可用的 Postgres 测试库（含 `vector` 扩展；不需要
`timescaledb`，测试通过 `Base.metadata.create_all` 建普通表即可）：

```bash
docker compose up -d db
export TEST_DATABASE_URL=postgresql+psycopg2://aims:aims_password@localhost:5432/aims
pip install -r requirements.txt
pytest -v
```

测试覆盖 Dashboard / Devices / Measurements / Alarms / AI Center / Maintenance / Assets
七个模块的主要路径和边界情况（404、重复编码冲突、库存不足、ROI 折旧计算等）。

## 环境变量说明（.env）

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | SQLAlchemy 连接串，Docker Compose 中自动指向 `db` 服务 |
| `SECRET_KEY` | JWT 签名密钥。**生产必填**（≥32 位随机值，`python -m app.cli gen-secret`）；开发/独立 EXE 可留空，自动生成并保存在数据库同目录的 `.dev_secret_key` |
| `ALLOW_DEV_ADMIN` / `DEV_ADMIN_USERNAME` / `DEV_ADMIN_PASSWORD` | 开发环境空库时自动创建的开发管理员（默认 `admin` / `admin123`）。生产环境禁止，强制开启会拒绝启动 |
| `BOOTSTRAP_ADMIN_USERNAME` / `_PASSWORD` | 空库首次启动时注入的管理员（须满足密码策略），优先于开发管理员 |
| `AI_PROVIDER` / `AI_API_KEY` / `AI_API_BASE` / `AI_CHAT_MODEL` | AI Center 对接的 LLM（OpenAI 兼容接口），留空则以内置规则引擎离线运行 |
| `AI_EMBEDDING_DIM` | 需与 `knowledge_chunks.embedding` 的 `vector(N)` 维度一致 |
| `CORS_ORIGINS` | 前端域名，逗号分隔 |

## 后续可扩展方向

1. 补充 Lab / Knowledge / System 模块的 REST API（表结构已就绪，Maintenance / Assets 已完成）。
2. 接入真实鉴权（JWT + `users`/`roles`/`permissions` 表，`passlib`/`python-jose` 已在依赖中）。
3. 知识库文档解析 + 分块 + Embedding 写入 `knowledge_chunks`，实现真正的 RAG 问答。
4. `ai_predictions` 目前为线性基线模型，可替换为真实时序预测模型。
5. 引入 Alembic 做结构化数据库迁移，替代当前的一次性 init SQL。
6. Work Order 目前需要手动创建；可加一个后台任务，在 `alarms` 触发 critical 告警时自动生成工单。

---

## 独立 EXE 模式（Windows 一键运行，无需 Docker / Postgres）

已完善数据库初始化逻辑，默认支持 **SQLite 单文件数据库**，可用 PyInstaller 打包成独立可执行文件。

### 已完善内容

1. **数据库自动初始化**（`app/db/init_db.py`）
   - 启动时自动 `CREATE TABLE`（35 张表）
   - 空库时（开发环境）自动创建开发管理员 admin / admin123；演示设备数据需设置 `SEED_DEMO_DATA=true`
2. **双数据库支持**
   - 默认：`sqlite:///./aims_data/aims.db`（适合 EXE / 本地开发）
   - 生产：继续使用 PostgreSQL + TimescaleDB + pgvector（Docker Compose）
3. **知识库向量列兼容**
   - PostgreSQL → `pgvector.Vector`
   - SQLite → JSON 数组存储 embedding（功能降级，不影响其它 API）
4. **打包脚本**
   - `build_exe.spec` + `build_exe.bat`（Windows 一键生成 `AIMS-Backend.exe`）
   - 入口：`run_standalone.py`

### 在 Windows 上打包 EXE

```bat
# 1. 进入项目目录
cd aims-backend

# 2. 双击运行（或命令行）
build_exe.bat
```

完成后输出：

```
dist\AIMS-Backend.exe
```

### 运行 EXE

1. 双击 `AIMS-Backend.exe`
2. 控制台会显示地址，并自动打开浏览器：`http://127.0.0.1:8000/docs`
3. 数据库文件自动创建在 EXE 同目录：`aims_data\aims.db`
4. 开发管理员：`admin` / `admin123`（仅非生产环境；生产请用 `python -m app.cli create-admin`）
5. 停止：关闭控制台窗口或 `Ctrl+C`

### 从源码以 SQLite 模式运行（开发调试）

```bash
# 不需要 Docker
export DATABASE_URL=sqlite:///./aims_data/aims.db
export AUTO_INIT_DB=true
python run_standalone.py
# 或
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 注意

- EXE 模式使用 SQLite，**不支持** Timescale hypertable 与 pgvector 近似搜索；适合演示、离线部署、单机使用。
- 若需要完整时序分析 + 向量 RAG，请继续使用 Docker Compose 的 PostgreSQL 方案。
- 打包请在 **Windows** 机器上执行 `build_exe.bat`（跨平台交叉编译不推荐）。

---

## v0.3 继续完善内容

### 新增 API 模块

| 模块 | 前缀 | 说明 |
|------|------|------|
| **Auth** | `/api/v1/auth` | `POST /login`、`POST /refresh`、`POST /logout`、`POST /change-password`、`GET /me`（JWT；开发环境默认 admin/admin123） |
| **System** | `/api/v1/system` | 用户 CRUD、角色列表、系统设置、审计日志 |
| **Lab** | `/api/v1/lab` | 实验室样品、线性回归比对 `POST /correlations/compute` |
| **Knowledge** | `/api/v1/knowledge` | 文档/分块 CRUD、关键词检索 `/search` |

### 其它改进

- JWT 鉴权依赖（`get_current_user` / `get_current_superuser`），System 写操作需登录
- 演示种子数据扩充：备件、资产、工单、知识库文档、Lab 样品、系统设置
- 独立模式启动后默认打开 **`/ui`**（内置前端原型 HTML）
- EXE 打包时自动把 `AIMS_*.html` 打进包内
- Dashboard 今日告警统计改为 SQLite 兼容写法
- 测试 conftest 默认支持内存 SQLite，无需 Postgres 即可跑测试

### 快速验证（源码）

```bash
python run_standalone.py
# 浏览器打开 http://127.0.0.1:8000/ui
# API 文档 http://127.0.0.1:8000/docs
# 登录: POST /api/v1/auth/login  {"username":"admin","password":"admin123"}
```

---

## v0.4 全部完善

### 1. 前端对接后端
- `AIMS_完整版.html` 已注入 **API Bridge**（`aimsFetch`）
- 默认 API：`http://127.0.0.1:8000/api/v1`
- 已对接：设备列表、Dashboard 统计、资产、登录、诊断/预测/健康、AI 对话、知识库检索、RAG、Lab 比对
- 后端不可用时自动回退本地演示数据

### 2. Alembic 数据库迁移
```bash
# 使用当前 DATABASE_URL
alembic upgrade head

# 生成新迁移（改模型后）
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```
- 配置：`alembic.ini` + `migrations/env.py`
- 初始版本：`migrations/versions/001_initial_schema.py`（35 张表）

### 3. Critical 告警自动生成工单
- `POST /api/v1/alarms` 当 `severity=critical` 时，自动创建 `priority=urgent` 的工单
- 逻辑：`app/services/alarms_hooks.py`（同 alarm 已有 open 工单则不重复）

### 4. 知识库 Embedding + RAG
- `app/services/rag.py`：词袋余弦相似度（离线）+ 可选 OpenAI embedding
- `POST /api/v1/ai/chat` 支持 `"use_rag": true`
- `POST /api/v1/knowledge/chunks` 在配置了 `AI_API_KEY` 时自动写 embedding
- `GET /api/v1/knowledge/search` 混合检索（关键词 + 向量）

### 推荐验证流程
```bash
python run_standalone.py
# 打开 http://127.0.0.1:8000/ui
# 系统页登录（开发环境默认 admin / admin123）
# Knowledge 搜索 Methane → 应返回种子文档
# AI Chat 勾选/调用 use_rag 提问
# POST critical 告警 → 检查 Maintenance 工单
```

---

## Windows 打包 EXE（推荐步骤）

> 若以前打包失败，请按下面 **干净重装** 做一遍。

### 环境要求
- Windows 10/11
- **Python 3.10 或 3.11**（不推荐 3.12+）
- 安装时勾选 **Add Python to PATH**
- 项目放在简单路径，例如 `C:\aims-backend`（避免桌面/中文路径）

### 推荐：目录模式（最稳）

```bat
build_exe.bat
```

成功后运行：

```
dist\AIMS-Backend\AIMS-Backend.exe
```

### 备选：单文件模式

```bat
build_onefile.bat
```

成功后运行：

```
dist\AIMS-Backend.exe
```

### 若仍然失败
1. 把黑色窗口里的 **红色报错全文** 复制保存  
2. 确认 `python --version` 为 3.10.x / 3.11.x  
3. 关闭杀毒软件后再打包  
4. 删除 `.venv`、`build`、`dist` 后重试  

EXE 仅使用 SQLite，不依赖 Docker / PostgreSQL。


---

## 桌面版与安装包（0.5.4）

- 双击 `AIMS-Backend.exe` 打开的是**桌面窗口**（pywebview 内嵌 `/ui`），不再跳到系统浏览器；关闭窗口即停止服务、退出进程。控制台窗口仍会保留，用来显示启动报错和日志。
- 没有 WebView2 Runtime（Windows 11 自带，Windows 10 通常随 Edge 更新带上）、开了 TLS（`SSL_CERTFILE` / `HTTPS_REQUIRED`）、或从源码运行 `python run_standalone.py` 时，会回退到系统浏览器；`AIMS_OPEN_BROWSER=0` 则什么都不打开、只跑服务。
- 运行 `build_installer.bat` 打 Windows 安装包：先调用 `build_exe.bat`，再用 Inno Setup 6（<https://jrsoftware.org/isdl.php>，或 `choco install innosetup`）编译 `installer\AIMS-Backend.iss`，产出 `installer_output\AIMS-Backend-Setup.exe`。按电脑安装（所有用户可用）：需要管理员权限，默认装到 `C:\Program Files\AIMS Backend`；数据目录 `aims_data`、`data`、`cores\models` 在安装目录内，安装包已授予普通用户读写权限，卸载时不会被删除。同一台电脑上的所有 Windows 用户共用这份数据。
- 重复双击不会再起第二个服务：检测到本机端口上已有 AIMS 在运行时，只会再开一个窗口连到它；端口被其他程序占用时会立刻报错并保持控制台打开（改 `.env` 里的 `PORT` 即可）。
- 程序图标取自 `assets/aims-logo.png`：`assets/aims.ico` 用于 EXE、桌面窗口、快捷方式和安装包。换图标时替换 `assets/aims.ico` 即可。
- 数据安全：启动时自动备份数据库到 `aims_data\backups\`（12 小时内最多一份，保留最近 7 份，`AIMS_BACKUP_KEEP=0` 关闭）；日志同时写入 `aims_data\logs\aims.log`（2MB × 3 轮转，`AIMS_LOG_FILE=0` 关闭）。恢复备份：先关闭程序，再在安装目录里运行 `AIMS-Backend.exe backups`（列出备份）和 `AIMS-Backend.exe restore`（恢复最新一份；`restore aims-时间戳.db` 恢复指定的一份）。恢复前会校验备份并把当前数据库另存为 `backups\pre-restore-时间戳.db`；程序还在运行时会拒绝恢复。
- 默认构建是轻量版（不含 AI 栈）；完整版请先 `set AIMS_BUILD_AI=1` 再构建。

## 数据分析开源 CORES（EXE 构建时自动下载）

构建脚本会自动下载并打包以下开源核心：

| 核心 | 用途 | 协议 |
|------|------|------|
| SheetJS (xlsx) | 解析 Excel 导入 | Apache-2.0 |
| Chart.js | 曲线/趋势图 | MIT |
| chartjs-adapter-date-fns | 时间轴 | MIT |

- 下载脚本：`scripts/download_cores.py`
- 清单：`cores/manifest.json`
- 本地目录：`cores/vendor/`
- 运行时访问：`http://127.0.0.1:8000/cores/...`
- API：`GET /api/v1/analysis/cores-info`

手动下载：

```bat
python scripts\download_cores.py
```


### 人工智能开源 CORES

| 核心 | 文件 | 用途 |
|------|------|------|
| Diagnosis Rule Pack | `cores/ai/diagnosis_rules.json` | 离线故障诊断规则 |
| Health Score Weights | `cores/ai/health_weights.json` | 健康评分扣分权重 |
| RAG Prompt Pack | `cores/ai/rag_prompts.json` | 对话/RAG 系统提示词 |
| Prediction Baseline | `cores/ai/prediction_baseline.json` | 线性外推预测配置 |

构建时由 `scripts/download_cores.py` 复制到 `cores/vendor/ai/` 并打进 EXE。

- API: `GET /api/v1/ai/cores-info`
- 诊断接口自动匹配规则包；配置 `AI_API_KEY` 时可再经 LLM 润色


---

## 0.5.0

See `CHANGES_MERGE_0_5_0.md` (chain loop + industrial core + security hardening), `CHANGES_CHAIN_LOOP.md`, `CHANGES_AIMS_CORE_V04.md`, `CHANGES_SECURITY_HARDENING.md`.
