# AI 中心与其他菜单深度绑定：设备搜索与选型

## 解决的问题

- AI 中心的 **Diagnosis / Prediction / Health 三个页签原来是假数据**（随机数、写死文案），没有调用后端已有的
  `/ai/diagnose`、`/ai/predict`、`/ai/health/{id}`。现在全部改为调用真实接口。
- AI 中心、Maintenance（SOP/ROI）、Lab（相关性）的设备下拉框原来各自写死 `Analyzer-001/002`，与真实设备无关。
  现在共用同一份设备数据。
- 前端 `devices` 数组原来只存设备编号，没有数据库 ID，导致无法用真实设备调用 AI 接口。现在保存 `dbId`、TAG 列表、位置。
- 后端没有"按条件搜索设备"和"选型"能力。

## 新功能

### 1. 设备搜索（AI 中心顶部的统一选择器）
- 输入即搜，支持编号、名称、型号、厂商、位置、IP、TAG、网关、资产编号，也支持自然语言，
  例如 `在线 GC 硫化氢`、`online GC that measures H2S`。
- 状态 / 组分 / 分析仪类型 / 协议词（中英文）被识别为**过滤条件**，其余词做 AND 子串匹配；界面会显示"识别为 …"，
  让你看到系统理解成了什么。
- 组分别名互通：`H2S` = `硫化氢` = `Hydrogen Sulfide`；`GC-H2`、`IR-1500` 这类设备编号不会被误判为组分/类型。
- 选中设备后，诊断/预测/健康/聊天都围绕它；TAG 下拉框来自该设备真实的 TAG。

### 2. 选型（新增 "Selection / 选型" 页签）
- 输入需求（自然语言和/或：需测组分、类型、协议、位置、仅在线、最低健康分），返回：
  - **设备推荐**：能否测量所需组分 > 是否为所需类型 > 综合得分（健康分、在线状态、数据就绪度、通讯链路、协议、位置，
    减去活动报警/未关闭工单的扣分）。每个候选带中英文理由（✓/✗）和缺失组分。
  - **型号推荐**：按该型号设备在本平台的实际表现（覆盖组分、平均健康、在线率、报警/工单、部署数量）排序。
- **局限**：`device_models` 表只有型号/厂商/类型/描述，没有量程、精度、价格。因此型号推荐是"本平台运行数据"层面的，
  **不是按厂商规格书选型**。界面上会显示这条说明。

### 3. 双向联动
- 其他菜单 → AI 中心：Devices 每行有 🤖 按钮（并有 "🤖 选型" 入口）；工单、资产、实验室样本里的设备可点击进入 AI 中心并预选该设备。
- AI 中心 → 其他菜单：选中设备后显示"足迹"按钮（设备/通讯 TAG·网关/近 24h 数据点/活动报警/未关闭工单/资产/实验室样本），
  点击跳转到对应菜单（Devices 会自动按该设备过滤；Data Analysis 的导入设备下拉、SOP/ROI/Lab 下拉自动选中该设备）。
- 诊断结果一键 **创建工单**（`POST /maintenance/work-orders`），创建后足迹里的工单数自动刷新；按钮点一次即锁定，防止重复建单。
- 聊天：可勾选"围绕所选设备对话"，此时 `device_id` 随消息发送；问"推荐能测 H2S 的在线设备""有哪些设备离线"等，
  后端会用同一套搜索/选型引擎把结果提供给 LLM。

## 新增/变更接口

| 接口 | 说明 |
|---|---|
| `GET /api/v1/ai/devices/search?q=&status=&analyzer_type=&component=&protocol=&limit=` | 搜索；返回 `parsed`（识别结果）、`total`、`items` |
| `POST /api/v1/ai/devices/select` | 选型；body：`query`、`components[]`、`analyzer_type`、`protocol`、`online_only`、`min_health`、`location`、`limit` |
| `GET /api/v1/ai/devices/{id}/context` | 设备在各菜单的足迹（用于跳转面板）；不存在返回 404 |
| `GET /api/v1/ai/devices/facets` | 平台上现有的组分/类型/协议/状态/型号，用于选型表单候选项 |
| `POST /api/v1/ai/chat` | 新增可选字段 `device_id` |

## 文件

- 新增：`app/services/device_selection.py`（纯逻辑：解析、搜索、选型打分，无 DB/框架依赖）、
  `app/services/device_search.py`（取数层，每张表一次分组查询）、`app/schemas/device_search.py`、`app/api/v1/ai_devices.py`
- 修改：`app/services/ai_context.py`（`gather_live_context` 支持 `device_id`，并接入搜索/选型）、`app/schemas/ai.py`、
  `app/api/v1/ai.py`、`app/api/v1/router.py`、`build_exe.spec`（新模块登记为 hiddenimports）、`frontend.html`、`README.md`
- 测试：`tests/test_device_selection_logic.py`（纯逻辑，53 项）、`tests/test_ai_devices.py`（接口与聊天联动集成测试）

## 行为说明（请留意）

- "Predict RUL" 按钮改名为 "Forecast/预测"：后端 `/ai/predict` 返回的是未来数值预测（含 95% 区间），并不是剩余寿命。
- **本地演示设备**（后端不可达时前端自带的 25 台）没有数据库 ID，诊断/预测/健康会输出明确标注"演示输出"的示例结果；
  **真实设备**请求失败时会如实显示错误，不会用假数据兜底。
- 选型需要后端；后端不可达时会提示，不做本地伪造。
- 前端新增内容对所有来自数据库/导入表格的文本做了 HTML 转义（设备列表、聊天里用户自己的消息同步修复）。
  其余未改动的旧渲染代码仍是直接拼接 `innerHTML`。
- 设备表的"型号"列仍显示 `Model #id`（`/devices` 接口只返回 `model_id`），属既有行为，未改动。

## 运行测试

```bash
pytest tests/test_device_selection_logic.py tests/test_ai_devices.py
pytest            # 全量，确认聊天上下文改动没有破坏原有用例
```
