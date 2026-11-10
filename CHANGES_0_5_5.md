# 0.5.5 — BUG 修复 + 按电脑安装（两条线合并，基于 0.5.4）

注意：曾经有两个不同的 0.5.5。仓库里已发布的 0.5.5（main 分支提交
`662d8da`，2026-11-09）是 NO_PROXY 修复；另一条线做的 0.5.5 是按电脑
安装。本版把两条线合起来，版本号仍按另一条线走到 0.5.9。

1. **httpx NO_PROXY 崩溃修复**（真 BUG，0.5.4 全量初跑 4 项失败的根因，
   来自已发布 0.5.5）：`app/services/ai_cores.py` 的 `_http_client()` 用
   `httpx.Client()` 默认读代理环境变量；httpx 解析不了 `NO_PROXY` 里带
   方括号的 IPv6（如 `[::1]`），建客户端时就抛 `InvalidURL: Invalid
   port: ':1]'`，模型下载全灭（最小复现：`[::1]` 必挂、`::1` 不挂）。
   修法：建客户端先按原样走代理，碰到 `InvalidURL` 降级成
   `trust_env=False` 再建并记警告；公司内网要走代理的机器照常走代理，
   不受影响。回归测试：`tests/test_ai_cores_download.py` 新加
   `test_download_survives_bracketed_ipv6_in_no_proxy`。
2. **安装包改为“按电脑安装”**：`installer/AIMS-Backend.iss`：
   `PrivilegesRequired=admin`、默认装到 `{autopf}\AIMS Backend`
   （Program Files，所有用户可用，安装时弹 UAC）。数据目录权限：
   `[Dirs]` 给 `aims_data`、`data`、`cores\models` 授予“用户可修改”
   并在卸载时保留；程序目录本身保持只读。安装结束“立即运行”加
   `runasoriginaluser`，以当前登录用户而不是管理员启动。
   注意：相对已发布 0.5.5 的按用户安装（`%LOCALAPPDATA%`、无需管理
   员），本版改成需要管理员权限、装到 Program Files；同一台电脑上的
   所有 Windows 用户共用这份数据。
3. **安装包脚本路径修法保留**（已发布 0.5.4 `ba2c82d` 的修法）：
   `.iss` 的 `SourceDir=..`（本版改写成 `..\dist\...` /
   `..\installer_output` 相对路径，等价）与 WebView2 检测补查 HKCU
   都保留；`AppId` 沿用已发布版 `{2B5C694D-8468-45C9-9B97-E586A493AF76}`，
   换 GUID 会让升级认不出旧安装。
4. **GitHub Actions 打安装包**（来自已发布 0.5.5 的加固版工作流）：
   `choco install innosetup` → ISCC 编译（x86/x64 路径回退、校验退出
   码）→ 校验 `installer_output\AIMS-Backend-Setup.exe` 存在 → 另传一
   个 artifact（lite 时叫 `AIMS-Backend-Setup-Lite`）。每次构建现出两
   个产物：便携文件夹 + 安装包。另删掉 0.5.4 又带回来的 `github/`（无
   点号的 workflow 误放副本，只留 `.github/`）。
5. `run_standalone.py`：崩溃日志在安装目录不可写时依次回退到
   `aims_data`、系统 TEMP；新增对应测试。
6. 版本号 0.5.4 → 0.5.5（`app/main.py` 三处、`.iss`、workflow 示例标签），
   README 的安装包说明同步。
