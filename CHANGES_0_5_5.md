# AIMS Backend 0.5.5 — BUG 修复 + 安装包打通

底包：`enoonc-sketch/aims-backend` main 分支提交 `ba2c82d`（0.5.4 安装脚本修正版）。
本版只改 BUG 修复与打包相关内容，版本号 0.5.4 → 0.5.5。

## 本版改动

1. **httpx NO_PROXY 崩溃修复**（真 BUG，0.5.4 全量初跑 4 项失败的根因）：
   `app/services/ai_cores.py` 的 `_http_client()` 用 `httpx.Client()` 默认读代理环境变量；
   httpx 解析不了 `NO_PROXY` 里带方括号的 IPv6（如 `[::1]`），建客户端时就抛
   `InvalidURL: Invalid port: ':1]'`，模型下载全灭（最小复现：`[::1]` 必挂、`::1` 不挂）。
   修法：建客户端先按原样走代理，碰到 `InvalidURL` 降级成 `trust_env=False` 再建并记警告；
   公司内网要走代理的机器照常走代理，不受影响。
   回归测试：`tests/test_ai_cores_download.py` 新加
   `test_download_survives_bracketed_ipv6_in_no_proxy`。
2. **安装包脚本路径 BUG 已由 ba2c82d 修好，本版只升版**：0.5.4 的 `installer/AIMS-Backend.iss`
   里 `Source: "dist\AIMS-Backend\*"` 会被 ISCC 解析成 `installer\dist\...`（不存在），
   因为相对路径以 .iss 所在目录为准；用户在 `ba2c82d` 里加了 `SourceDir=..` 修好，
   并补查了 HKCU 下的 WebView2 注册表键。本版保留该修法，只把 `MyAppVersion` 升到 0.5.5。
3. **GitHub Actions 真正打出安装包**（`.github/workflows/build-exe.yml`）：`CHANGES_0_5_4.md`
   第 4 项称工作流“在构建后用 Inno Setup 打安装包”，但工作流文件里根本没有这步。
   本版补上：`choco install innosetup` → ISCC 编译 `installer/AIMS-Backend.iss` →
   校验 `installer_output\AIMS-Backend-Setup.exe` 存在 → 另传一个 artifact
   （lite 时叫 `AIMS-Backend-Setup-Lite`）。每次构建现出两个产物：便携文件夹 + 安装包。
4. 删掉 0.5.4 又带回来的 `github/`（无点号的 workflow 误放副本，只留 `.github/`）；
   0.5.3 清理时已删过一次。
5. 升版：`app/main.py` 三处、`installer/AIMS-Backend.iss` 的 `MyAppVersion` 改为 0.5.5
   （.iss 的 `SourceDir=..` 路径修法沿用 `ba2c82d`，未动）。

## 验证（Linux 沙箱，Python 3.12.3）

- 全套 pytest：**437 passed、1 skipped、0 failed**（0.5.4 修前为 429 通过、4 失败；
  变基到 `ba2c82d` 后重跑，含其改写的桌面窗口测试，共 437 项全过）。
- 说明：安装包那步只能在 Windows 上验证——沙箱没有 ISCC，`installer_output` 的落点与
  `Source` 路径按 Inno Setup 官方规则核对过（相对路径以 .iss 所在目录为准），
  以 GitHub Actions 上那次构建的实际结果为准。
