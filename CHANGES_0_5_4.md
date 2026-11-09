# AIMS Backend 0.5.4 — 桌面窗口 + 安装包

底包：0.5.3（提交 `5aa90694`）。本版只改下面 5 项，业务代码与安全配置不动。

1. **pywebview 桌面窗口**：冻结 EXE 启动后不再跳系统浏览器，而是在内嵌的桌面窗口里打开 `/ui`；`run_standalone.py` 改为服务器跑后台线程、主线程开窗口，关窗口即退出；TLS 模式、源码运行、没有 WebView2、窗口失败一律回退浏览器，`AIMS_OPEN_BROWSER=0` 仍表示只跑服务。`requirements-exe.txt` / `requirements-exe.lock` 加入 `pywebview==6.2.1`（`pythonnet`、`clr_loader` 为 Windows 专用依赖，lock 里带 `sys_platform == "win32"` 标记）；新增 `tests/test_desktop_window.py`（mock `webview`，不开真窗口）。
2. **PyInstaller 配置**：`build_exe.spec` 显式收集 `webview` 全部子模块并声明 `clr_loader`、`pythonnet`；`build_exe.bat` 与 `build_onefile.bat` 的命令行兜底同步加这三个 `--hidden-import`；`console=True` 不变。
3. **Inno Setup 安装包**：新增 `installer/AIMS-Backend.iss`（装到 `%LOCALAPPDATA%\AIMS Backend`，数据目录不动，缺 WebView2 时只提示不阻止安装）和本地打包脚本 `build_installer.bat`。
4. **GitHub Actions**：`build-exe.yml` 在构建后用 Inno Setup 打安装包并检查，额外上传 `AIMS-Backend-Setup` 产物；原文件夹产物与 lite/full 逻辑不变。
5. **版本号与文档**：`0.5.3` → `0.5.4`（`app/main.py` 三处、`.iss` 一处）；README 新增"桌面版与安装包"一节；新增本文件。

沿用 0.5.2 性能优化与 0.5.3 补改的全部内容，未改动（见 `CHANGES_PERF_OPT_0_5_2.md`、`CHANGES_0_5_3.md`）。

## 实现中对清单的两处必要调整

- `installer/AIMS-Backend.iss` 加了 `SourceDir=..`：脚本放在 `installer\` 子目录，而 Inno Setup 的 `Source` / `OutputDir` 相对路径以 `SourceDir`（默认是脚本所在目录）为准；不加的话会去 `installer\dist\` 找产物。加上后产物仍在仓库根目录的 `installer_output\`。
- WebView2 检测在 HKLM 两个视图之外补查了 `HKCU`：普通用户权限安装的 Runtime 把键写在 HKCU，只查 HKLM 会对已装用户误报。
