# AIMS Backend 0.5.4

- EXE 在冻结模式下通过 pywebview 桌面窗口展示 `/ui`，并在窗口不可用时回退系统浏览器。
- 新增桌面窗口逻辑的 mock 测试，并将 pywebview 依赖及 Windows 后端加入 EXE 打包配置。
- 新增 Inno Setup 安装脚本和本地安装包构建脚本，默认安装到用户目录并提示缺少 WebView2 Runtime。
- GitHub Actions 增加 Windows 安装包构建与独立 artifact 上传。
- 将应用版本提升至 0.5.4，并更新桌面版与安装包文档。
