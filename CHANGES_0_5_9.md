# 0.5.9 — 备份恢复命令与升级检测（基于 0.5.8）

1. 新增命令 `AIMS-Backend.exe backups`（列出自动备份）和 `AIMS-Backend.exe restore [latest|文件名]`（恢复）。恢复前：确认程序没在运行、用 `PRAGMA integrity_check` 校验备份、把当前数据库另存为 `backups/pre-restore-时间戳.db`（不参与自动清理）；用 sqlite3 在线备份接口写入临时文件后原子替换，并清理 `-wal` / `-shm`。
2. Windows 互斥体：程序运行时创建 `AIMS-Backend-Mutex`；安装包加 `AppMutex=AIMS-Backend-Mutex`，升级或卸载时若程序仍在运行会提示先关闭，避免文件被占用而覆盖失败（仅 Windows，非 Windows 上是空操作）。
3. 新增 `tests/test_restore.py`（8 项）；版本号 0.5.8 → 0.5.9（`app/main.py` 三处、`.iss`、workflow 示例标签）。
