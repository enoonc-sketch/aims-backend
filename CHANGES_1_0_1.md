# AIMS 1.0.1 变更说明（2026-11-11）

1.0.1 是 1.0.0 的修复版：1.0.0 正式版在真机上启动即崩溃（见下），
本版只修启动问题并合入已准备好的品牌与可观测性改动，不做功能加法。

## 启动崩溃修复（P0，1.0.0 真机启动即崩）

- 现象：安装 1.0.0 后双击启动，只弹出标题 "AIMS Backend"、内容只有
  "Press Enter to exit..." 的报错框，没有任何原因。
- 根因：1.0.0 把 `build_exe.spec` 改成 `console=False` 后，Windows 下
  `sys.stdin/stdout/stderr` 全是 `None`；uvicorn 的 ColourizedFormatter
  在 `configure_logging()` 里调 `sys.stdout.isatty()`，`config.load()` 直接抛
  `ValueError: Unable to configure formatter 'default'`，服务线程还没起就死了。
  同时 `_pause()` 把等回车的提示语当作弹窗文本显示，真正报错看不见。
- 修复（`run_standalone.py`）：启动第一时间 `_redirect_missing_streams()`
  把缺失的三个流接到空设备（PyInstaller 官方推荐做法）；`_pause(detail)`
  弹窗显示真正的报错原因 + 崩溃日志路径；服务线程包一层记异常，
  "未就绪"与"配置错误"路径也写崩溃日志。
- 回归：`tests/test_no_console_window.py` 新增 4 项，Linux 上模拟 windowed
  环境即可复现与验证。

## 品牌更新（1.0.0 后续变更）

- `assets/aims-logo.png`、`assets/aims-icon-512.png`、`assets/aims.ico`：
  新版 AIMS 标志（A + 轨道 + 柱状图 + AiMS 字样），EXE / 托盘 / 快捷方式 /
  安装器 / 浏览器标签页图标全部随之更新。
- `frontend.html`：侧边栏顶部文字 LOGO 替换为内嵌 PNG 图片 LOGO。

## 可观测性 / 导入吞吐 / 签名自动化（1.0.0 后续补强）

- 结构化日志（`app/core/logging_setup.py`）：`AIMS_LOG_FORMAT=json` 时每条日志
  一行 JSON，含请求上下文；默认仍是文本格式。
- 运行指标（`app/core/metrics.py` + `GET /api/v1/system/metrics`，仅超管）：
  请求总数/状态类/方法/路由、延迟直方图（p50/p90/p95/p99）、并发数、进程 RSS
  与线程数；`?format=prometheus` 直接输出抓取格式。
- 导入吞吐（2.1 万 → 4.5 万条读数）：批量导入改用 `Reading`（`__slots__`）、
  时间列批量 `dt.to_pydatetime()`、PostgreSQL 大批量写入改 `execute_values`
  多值 INSERT + `ON CONFLICT DO UPDATE`，幂等语义不变。
- 代码签名自动化：`Sign-AimsFile` / `Resolve-SignTool` / `Get-SignConfig`，
  环境变量 `AIMS_SIGN_CERT` / `AIMS_SIGN_PASSWORD` / `AIMS_SIGN_TIMESTAMP` /
  `AIMS_SIGN_REQUIRED`；顺序（主程序 → 安装包 → 校验和）由
  `tests/test_release_chain.py` 锁死；未签名时显式告警并在
  `release-manifest.json` 记 `code_signing.signed=false`。

## 测试

全量 **588 passed / 2 skipped**（1.0.0 是 560；新增：可观测性 20、
签名 4、无控制台回归 4、补回的 NO_PROXY 回归测试 1；其中 588 = 560 + 28）。
实跑：/health 1.0.1、/favicon.ico 200、/ui 200、OpenAPI 199、
匿名访问受保护接口均为 401、`run_standalone.py backups` 正常。

## 合版说明（对照已发布 1.0.0 核实）

本包底包基于 1.0.0 之前的桌面版源码，合版时从已发布 1.0.0 恢复了：
NO_PROXY 修复及回归测试、CHANGES_0_5_4/0_5_5 合并版、CHANGES_0_5_10.md
与 CHANGES_0_6_0.md、加固版 build-exe 工作流（示例标签改到 v1.0.1）、
修好的 AIMS-Backend.iss（GUID 花括号 bug + 已发布 AppId，版本号 1.0.1）、
bat 的 webview hidden-import、lock 的 pefile/pywin32-ctypes、LICENSES.txt 版本、
build/ 目录（5 个 .ps1；来包缺失，build-installer.yml 与签名测试都要用它）。
工作流文件不能用 push-files 推，须在网页上改。
