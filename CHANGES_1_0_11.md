# AIMS 1.0.11 变更说明

## 去掉 pywebview，界面只用电脑自带的 Edge --app 窗口

1.0.10 之前：双击打开的是 pywebview 内嵌的原生桌面窗口（Edge WebView2 渲染），
点 × 藏到托盘、托盘菜单可显示/隐藏窗口。pywebview 本身打进 EXE、用户不用另装，
但窗口要靠机器上的 WebView2 Runtime；没有它时安装程序会提示、启动器回退浏览器。

1.0.11 起：**不再用 pywebview / WebView2 / pythonnet / clr_loader**。
界面只用系统自带的 Edge --app 窗口打开（无地址栏、像桌面软件）；
机器上没有 Edge/Chrome 时退回系统默认浏览器。窗口关了应用仍在托盘后台运行，
托盘菜单选“退出程序”才真正退出。

### 行为变化（有意为之）

- 点 × 不再是“藏到托盘”，就是关掉浏览器窗口；应用照常在托盘跑。
- 托盘菜单 / 全局热键不再“显示/隐藏已有窗口”，而是打开一个新的 Edge --app 窗口
  （Edge 会把请求交给已在跑的同 profile 窗口，不会重复开一堆）。
- CSV/Excel 导出不再走窗口内嵌的保存对话框，走浏览器自身的下载。

### 改动文件

- `app/launcher/main.py`：删桌面窗口模式（`_use_desktop_window`、`_run_desktop_window`、
  `_set_window`、`self.window` / `self._window_visible` / `self._quitting`）；`start()`、
  托盘菜单、热键、重启服务、退出全部改走 `open_app_window`；`_on_close_to_tray`
  只保留“浏览器窗口关了、第一次提醒一声”。
- `app/launcher/platform_win.py`：删 `webview_available`、`run_desktop_window`、
  `show/hide/destroy_desktop_window`、`load_desktop_url`（99 行）；`run_tray_detached`
  保留（通用托盘工具，有测试覆盖）。
- `run_standalone.py`：`_open_ui_window` 只开系统浏览器、恒返回 False；`main()` 的
  等待循环不再分“窗口模式 / 浏览器模式”。
- `build_exe.spec`：删 `collect_submodules('webview')` 与 `clr_loader` / `pythonnet` 收集项。
- `build/build_common.ps1`：启动器打包删 `--collect-submodules webview` 与
  `--hidden-import pythonnet / clr_loader`。
- `requirements-exe.txt`：删 `pywebview==6.2.1`。
- `installer/AIMS-Backend.iss`：删整个 `[Code]` 段的 WebView2 Runtime 检测与提示。
- 文档：README、安装说明、计划书对照与差距分析、依赖与许可证清单同步改写。

### 测试

- `tests/test_launcher_tray.py`：删 3 个桌面窗口关闭测试与 `_fake_webview` / `_FakeWindow`；
  热键、打开界面、打开设置、退出改测 Edge --app 打开；`_on_close_to_tray` 只测“第一次提醒”。
- `tests/test_desktop_window.py`：`_open_ui_window` 改测“源码 / 冻结 EXE / TLS 一律只开浏览器、
  且绝不 import webview”；`main()` 的接线测试不变。
- 新增 `tests/test_no_webview.py`：代码、打包配置、安装器里不得再出现
  `webview` / `pythonnet` / `clr_loader`（历史文档除外）。
