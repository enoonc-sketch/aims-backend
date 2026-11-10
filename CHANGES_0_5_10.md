# 0.5.10 — Windows 桌面独立版（基于 0.5.9）

桌面独立版改造在 0.5.9 源码基础上开发，发布时升版为 0.5.10（v0.5.9 的 tag 已存在，指向旧提交）。

1. 新增部署引擎 `app/deploy/`：程序/数据目录分离、Config/aims.env 与 secrets.json、deployment.json、内置 PostgreSQL（initdb/启停/dump/restore/归属修正）、Windows 服务、只增不减的结构对账（pgvector/TimescaleDB/BRIN）、备份/恢复/保留策略、模型登记与探针、安装/升级编排（幂等）、逐项自检、启动引导、运维 CLI（`AIMS-Backend.exe deploy <cmd>`，14 个子命令）、30 项功能自检。
2. 新增桌面启动器 `app/launcher/`（入口 `aims_launcher.py`，打 AIMS.exe）：单实例命名互斥体、按依赖顺序启动（PostgreSQL → AI 自检 → 后端 → 主界面）、托盘菜单、错误弹窗带中文说明与日志路径、启动画面、父进程看门狗（启动器被强杀后端 3 秒内退出）。
3. 新增安装器 `installer/AIMS.iss`：覆盖升级（升级前备份 + 旧程序副本可回滚）、安装后健康检查、卸载默认保留数据；装到 `C:\Program Files\AIMS`，新 AppId `{8C1F2A64-3B7D-4E52-9A11-6E2D5C0B7F31}`——与 0.5.9 的 `installer/AIMS-Backend.iss`（`C:\Program Files\AIMS Backend`）互不识别，已装 0.5.9 的机器会并排装两个。GitHub Actions 仍用 AIMS-Backend.iss 打包，AIMS.iss 只由本地 `build/*.ps1` 用。
4. 修复 8 处真实缺陷：端口上有别的 PostgreSQL 时误判（改用 postmaster.pid 判定）、恢复备份后对象归属变超级用户（恢复后 normalize_ownership）、删除被引用设备 500（改 409 + 中文说明）、内置 PG 依赖系统默认 unix socket 目录（固定到数据目录内）、启动器被强杀留孤儿后端（父进程看门狗）、init_db 盲目 create_hypertable 刷 traceback（统一走 deploy.schema + BRIN 回退）、`--root` 只能写在子命令前（两种顺序都支持）、secrets.json 换目录旧安装读不到（新位置优先、旧位置只读兼容）。
5. `run_standalone.py`：AIMS_ROOT 覆盖、内置 PG 检测（有 pgsql/bin 才走 PG，否则回落 SQLite）、`deploy` 子命令分发；`app/main.py`：AIMS_ROOT 前端查找；`app/core/config.py`：EMBEDDING_STORAGE（auto/vector/json）；`app/models/knowledge.py`：按 EMBEDDING_STORAGE 选 pgvector 或 JSON；`build_exe.spec`：显式收集 app.deploy/app.launcher 与 psycopg2/pgvector/pystray/PIL。
6. 合版说明：桌面版底包是修 bug 前的 0.5.9，合版时从已发布 0.5.9 恢复了 NO_PROXY 修复及回归测试、lock 的 pefile/pywin32-ctypes、CHANGES_0_5_4/0_5_5 合并版、加固版工作流、修好的 AIMS-Backend.iss（GUID 花括号 bug + 已发布 AppId）、bat 的 webview hidden-import；AIMS.iss 补了 UTF-8 BOM。详见 `CHANGES_0_5_9_DESKTOP.md` 与核查报告。
7. 版本号 0.5.9 → 0.5.10：`app/main.py` 三处、`app/deploy/__init__.py` 的 APP_VERSION、两个 .iss、workflow 示例标签、README、LICENSES.txt、构建/验收脚本示例。
