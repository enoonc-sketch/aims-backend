# 0.6.0 — 模板中心 + 桌面原生窗口（基于 0.5.10）

模板中心与桌面窗口改造在 0.5.9 桌面版源码基础上开发，发布时升版为 0.6.0（基准是已发布的 0.5.10，见合版说明）。

1. 新增模板中心（计划书第十二章）：`app/services/template_center.py`（20 个内置模板：仪表/分析仪/色谱/台账/TAG 点位/通信/报警等）、`app/services/template_package.py`（导入导出包）、`app/api/v1/templates.py`（18 个接口：列表/创建/详情/修改/删除/复制/收藏/设默认/导入/导入记录/导出/批量导出/版本/对比/恢复版本/应用/用量）、`app/models/template.py`（TemplateRegistry/TemplateVersion/TemplateImportLog/TemplateUsageLog/TemplateDependency）、`app/schemas/template.py`、前端 Templates 页（模板列表/导入模板/导入记录）、`tests/test_templates.py`（18 项）、自检新增 `_templates`（内置模板 ≥15、创建→应用→导出→清理）。
   应用引擎把模板真正落到业务数据：设备类→Device + DeviceComponent、TAG 点位→DeviceTag、通信配置→Gateway、报警配置→AlarmRule、资产→Asset、维护→MaintenanceSOP、RAG 知识库→KnowledgeDocument；不支持自动应用的分类明确返回暂不支持，绝不假成功。应用产生的是新增业务数据，模板删除不触碰它们。
2. 桌面原生窗口（计划书 3.1-A）：启动器在托盘可用且 pywebview 可导入时用原生桌面窗口（pywebview/WebView2）代替 Edge --app，避免双窗口；WebView2 缺失时回退传统路径。`build/build_common.ps1` 启动器打包补了 `--collect-submodules webview` 与 pythonnet/clr_loader。
3. 版本号集中化：`app/main.py` 三处版本号改走 `app.deploy.APP_VERSION`（0.6.0），不再各处硬编码。
4. 新增工作流 `.github/workflows/build-installer.yml`：在 GitHub Windows 机器上跑 `build/build_x64.ps1` 全流程（依赖→内置 PG/pgvector→打包→冒烟→ISCC→报告），产物 AIMS-<版本>-x64-Setup.exe 与 build-report-x64.json；推 v* 标签或手动触发。**注意：工作流文件不能用 push-files 推（应用没有 workflows 权限，会 403），须由 T 在网页上新建该文件。** checkout@v5、setup-python@v6、upload-artifact@v7。
5. 合版说明：0.6.0 底包又是基于修 bug 前的 0.5.9 桌面版做的，合版时从已发布 0.5.10 恢复了 NO_PROXY 修复及回归测试、lock 的 pefile/pywin32-ctypes、CHANGES_0_5_4/0_5_5 合并版、加固版工作流（示例标签改到 v0.6.0）、修好的 AIMS-Backend.iss（GUID 花括号 bug + 已发布 AppId，版本号改到 0.6.0）、bat 的 webview hidden-import、丢失的 CHANGES_0_5_10.md；AIMS.iss 补回 UTF-8 BOM、LICENSES.txt 版本改到 0.6.0。
6. 版本号：APP_VERSION 0.6.0；两个 .iss 都是 0.6.0；README、构建/验收脚本示例、文档里的安装包名都已是 0.6.0。
