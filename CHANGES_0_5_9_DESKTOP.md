# 0.5.9 — Windows 桌面独立版改造

本次改造目标：把 AIMS 从"需要开发环境才能跑的后端"变成"双击安装、桌面启动、
数据本地、可备份恢复、可覆盖升级、可安全卸载"的 Windows 独立软件。
**没有删除、简化或替换任何现有业务功能**（136 个接口 / 18 个模块全保留，
对照表见 `docs/功能对照表.md`）。

## 新增

### 部署引擎 `app/deploy/`

| 模块 | 职责 |
|---|---|
| `paths.py` | 程序目录 / 数据目录分离的路径解析（产品模式与绿色模式共存） |
| `conf.py` | `Config/aims.env` 与 `secrets.json` 的生成、读取、权限收紧 |
| `state.py` | `deployment.json`：安装版本、升级历史、组件版本 |
| `pg.py` | 内置 PostgreSQL 集群：initdb、配置、启动停止、建库建用户、dump/restore、归属修正 |
| `pgservice.py` | Windows 服务注册/注销/启停 |
| `schema.py` | 只增不减的结构对账；pgvector / TimescaleDB / BRIN 处理 |
| `backup.py` | 备份、校验、保留策略、恢复（含恢复前安全备份） |
| `ai_engine.py` | 模型清单、登记入库、内置模型落盘、探针与 warmup |
| `provision.py` | 安装/升级编排（幂等） |
| `doctor.py` | 逐项自检与报告 |
| `runtime.py` | 应用启动前的引导（配置 → 数据库 → 结构） |
| `cli.py` | `AIMS-Backend.exe deploy <command>` 运维命令 |
| `selftest.py` | 30 项真实业务请求的功能自检 |

### 桌面启动器 `app/launcher/`

- 单实例（命名互斥体）；第二次双击直接打开已有界面
- 按依赖顺序启动：PostgreSQL → AI 自检 → 后端 → 主界面
- 托盘菜单：打开界面、立即备份、数据目录、日志、诊断报告、重启、开机启动、退出
- 错误弹窗带中文说明与日志路径；启动画面显示进度
- 父进程看门狗：启动器被强杀时，后端 3 秒内自行退出（不留孤儿进程）

### 安装与构建

- `installer/AIMS.iss`：覆盖升级（升级前备份 + 旧程序副本可回滚）、
  安装后健康检查（不通过不算装好）、卸载默认保留数据
- `build/build_x64.ps1`、`build_arm64.ps1`、`dependency_check.ps1`、`package_release.ps1`
- `tools/`：`install/upgrade/uninstall/backup/restore/Start-Aims/Stop-Aims/Repair-Aims/Get-AimsStatus/Register-AimsTasks/Windows-Acceptance`

### 文档

`docs/部署说明.md`、`用户手册.md`、`故障排查.md`、`功能对照表.md`、
`依赖与许可证清单.md`、`测试报告.md`、`LICENSES.txt`

## 修复（真实缺陷）

1. 端口上有别的 PostgreSQL 时误判为"自己的库在跑" → 用 `postmaster.pid` 判定归属
2. 恢复备份后对象归属变超级用户 → 恢复后自动 `normalize_ownership()`
3. 删除被引用的设备返回 500 → 返回 409 + 中文说明
4. 内置 PG 依赖系统默认 unix socket 目录 → 固定到数据目录内
5. 启动器被强杀后留下孤儿后端 → 父进程看门狗
6. `init_db` 盲目 `create_hypertable` 刷 traceback → 统一走 `deploy.schema` + BRIN 回退
7. `--root` 只能写在子命令前 → 两种顺序都支持
8. `secrets.json` 换目录导致旧安装读不到 → 新位置优先、旧位置只读兼容

## 兼容性

- 老版本 SQLite / 绿色运行方式仍可用（无 `pgsql` 目录时自动回落）
- 老版本 `aims_data/secrets.json` 自动识别
- 表结构只增不减；不重命名、不合并、不删除任何业务表

## 已知限制

- TimescaleDB 无 Windows 官方构建 → BRIN 索引替代（放入扩展文件后可自动启用）
- ARM64 的数据库走 x64 仿真 → 未真机验收前不得宣称原生支持
- 单机部署：应用与数据库只监听 `127.0.0.1`
- SQLite → PostgreSQL 不自动迁移（结构差异大，需重新导入台账/测点）
