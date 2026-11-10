# 0.5.8 — 数据安全与可维护性（基于 0.5.7）

1. 启动时自动备份 SQLite 数据库：用 sqlite3 在线备份接口复制到 `aims_data/backups/aims-时间戳.db`，12 小时内最多一份，保留最近 `AIMS_BACKUP_KEEP`（默认 7）份，设为 0 关闭；备份失败不影响启动。只在确认没有其他实例在运行之后才执行。
2. 日志文件：除控制台外，同时写入 `aims_data/logs/aims.log`（2MB × 3 个文件轮转，`AIMS_LOG_FILE=0` 关闭）；应用日志与 uvicorn 日志都记录，且不重复。
3. 网页图标：新增 `/favicon.ico`（读取 `assets/aims.ico`，EXE 里一并打包），`frontend.html` 的 `<head>` 加 `<link rel="icon">`，窗口和浏览器标签页都显示 AIMS 标志。
4. 新增 `tests/test_robustness.py`（9 项），主流程测试里屏蔽备份和文件日志；版本号 0.5.7 → 0.5.8（`app/main.py` 三处、`.iss`、workflow 示例标签）。
