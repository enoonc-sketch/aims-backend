# 0.5.7 — 应用图标（基于 0.5.6）

1. 新增 `assets/`：`aims-logo.png`（原始标志）、`aims.ico`（16–256px 多尺寸，取标志左侧的 "A + 轨道" 图形、保留透明底）、`aims-icon-512.png`。
2. `build_exe.spec` 给 EXE 设置 `icon`（文件缺失时自动跳过）；`build_exe.bat` / `build_onefile.bat` 的命令行兜底加 `--icon assets\aims.ico`。桌面窗口、任务栏和快捷方式使用 EXE 图标。
3. 安装包：`SetupIconFile`（安装程序图标）与 `UninstallDisplayIcon`（控制面板"卸载程序"里的图标）。
4. 版本号 0.5.6 → 0.5.7（`app/main.py` 三处、`.iss`、workflow 示例标签），README 增加图标说明。
