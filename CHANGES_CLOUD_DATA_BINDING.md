# 云数据 ↔ 数据管理 深度绑定

## 前端 (frontend.html)
- 系统设置上方的「数据管理」(page-ext) 更名为 **云数据 / Cloud Data**（导航、页面标题、中英文字典）。
- 「数据管理」(page-data) 重做为 4 个标签：
  - **导入**：选设备后上传 CSV/Excel（真实调用 `POST /measurements/import`），下方「导入记录」同时列出本地导入与云数据同步批次。
  - **趋势图**：设备/组分来自真实数据（`/measurements/components`），可多选，纯 SVG 绘制（离线可用）。
  - **历史数据**：分页查询，可按设备/组分/来源(本地|云数据)/时间筛选，显示每条数据的来源，支持导出当前页 CSV。
  - **云数据同步**：列出云数据里配置的数据库，可「立即同步」「查看数据」，并可跳转「配置云数据源」。
- 云数据页 → 数据管理：顶部说明条、数据库列表「在数据管理中查看」、断点列表「看趋势」。
- 同步逻辑合并为 `cloudSync()`，云数据页与数据管理页共用，同步后自动刷新导入记录。
- 修复：切换中文后页面标题仍显示英文（如 "Data"）。

## 后端
- `measurement_imports` 新增 `source`(local/cloud) 与 `external_database_id`；迁移 `006_measurement_import_source`，SQLite(EXE) 启动时自动补列。
- 云同步（external_import）每次登记一个 `source=cloud` 批次，并给写入的测量点打上 `import_id`。
- 新接口：`GET /measurements/history`、`GET /measurements/imports`、`GET /measurements/components`。
