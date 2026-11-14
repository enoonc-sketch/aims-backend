# AIMS 1.0.12 变更说明

以已发布的 1.0.11 为底，合入 1.0.8 平行包（Claude 在 1.0.8 底上做的）里经核验可用的新工作。
平行包的其余内容（内置引擎构建方案、pywebview 桌面窗口、锁定屏回退、admin 口令旧文字）
与 1.0.9–1.0.11 已发布内容冲突或重复，本版一律不合，见末尾“未合入”。

## 托盘新增：报警（当前报警、新报警气泡、开关）

托盘已有打开主界面、设置、快捷键、备份、目录、诊断、重启、开机自启、版本、退出，
以及登录三行。对监控系统来说最大的缺口是报警——托盘常驻就是为了不开界面也能知道
出事了，所以补了这块（`app/services/tray_summary.py` 新增，`GET /auth/ui-state?alarms=1`）：

- **“当前报警”一行**（菜单顶部，登录信息下面）：`当前报警：3 条未处理（严重 1）` /
  `无未处理` / `未获取`。点它直接打开报警页（Edge --app 新窗口，`#alarm` 深链接，
  未登录会先去登录再跳过去）。托盘图标提示文字同步带上“· 报警 N”。
- **新报警气泡**：出现比已见过的更新的报警时弹一次 Windows 通知，严重级别的标题是
  “AIMS 严重报警”，否则“AIMS 新报警”，正文是最新一条的内容（多条时注明“另有 N 条”）。
  - 启动后第一次取到数据只记下现状，**不会把启动前就有的旧报警当新报警弹**。
  - 判断“新”用的是历史最大报警 id，所以把报警确认 / 解决掉不会让它倒退，也不会重复弹。
  - 新报警在两次查询之间已被处理掉的，不弹（只看“未处理”）。
- **“新报警弹出气泡通知”开关**（菜单里可勾选，存在 `aims_data/launcher.json` 的
  `alarm_notify`，默认开）。关掉后仍会更新“当前报警”一行，只是不弹气泡；重新打开不会
  补弹关闭期间的报警。
- 实现：复用托盘读登录状态的同一条受口令保护的通道，`GET /auth/ui-state?alarms=1`
  附带报警摘要（未处理数 / 严重数 / 最新 3 条 / 历史最大 id，消息截断到 120 字）；
  登录状态每 5 秒取一次，报警要查库，每 15 秒取一次。查库失败时摘要为 null，
  不影响登录信息。
- 限制：气泡只在 Windows 托盘里有；气泡内容来自报警消息原文；“严重”按报警的
  severity = critical 判断。

## 模型包列表新增 installable / bundled 标志（后端）

`GET /ai/packages` 每项新增 `installable`（本进程能否在线 pip 安装）和 `bundled`
（是否已随安装包内置）：冻结 EXE 里 `installable` 一律 False，`bundled` 表示构建时
已内置。前端本版未改（1.0.11 已有原生引擎下载区，安装版装引擎走那里）。

## 未合入（平行包里有、1.0.12 刻意不要的）

- **内置引擎构建方案**：平行包改 `build_installer.bat` / `build_exe.bat` /
  `build/build_x64.ps1`，强制把 llama-cpp-python 打进安装包，并用 `deploy ai-probe`
  在 EXE 里验证。这是与 1.0.9“下载官方预编译 llama-server.exe”不同的另一条路线，
  两套引擎机制并存会乱；且 1.0.11 的发布构建是 Lite 版，强制内置引擎会打乱发布流程。
  是否换路线，由 T 定，再单独做（需 Windows 真机完整构建验证）。
- **pywebview 桌面窗口**：平行包的 `app/launcher/main.py`、`platform_win.py`、
  `run_standalone.py`、`build_exe.spec`、`requirements-exe.txt`、安装器 WebView2 检测
  仍走 pywebview，与 1.0.11“去掉 pywebview，只用 Edge --app 窗口”的方向直接冲突，不合。
- **锁定屏回退**：平行包的 `frontend.html` 没有 1.0.9 的原地锁定屏（未登录改回跳登录页，
  数据分析页也不锁定），1.0.9 的锁定屏保留，不合。
- **原生引擎安装端点删除**：平行包的 `app/api/v1/ai.py` 删掉了 1.0.9 的 `/ai/engines`
  下载/安装/卸载接口与 `engine_installer.py`，1.0.9 已发布功能保留，不合。
- **admin 口令旧文字**：平行包的文档、`.env.example`、CLI 帮助里默认口令仍写 `admin`，
  1.0.10 已改成 `admin123`，保留 1.0.10 的修正，不合。

## 测试

- SQLite 全量：**791 通过、2 跳过、0 失败**（1.0.11 基线 775 通过、2 跳过、0 失败；新增 16 个全过）。
- 新增：`tests/test_access_permissions.py` 报警摘要 2 个；
  `tests/test_launcher_tray.py` 托盘报警 10 个（含偏好默认值/校验）；
  `tests/test_ai_package_flags.py` 4 个（installable / bundled 标志）。
- 未验证（需真机 / 真环境）：托盘气泡在真实 Windows 上的实际表现；Edge --app 打开
  `#alarm` 深链接；PostgreSQL 全量。
