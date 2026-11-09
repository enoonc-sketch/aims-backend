# AI Studio：AI 生成 SOP/ROI、数据分析 AI 读取、知识库深度绑定、实验室由数据分析驱动的比对与 AI 认证

## 分工原则

**数字永远由后端从平台数据算出；LLM（如果配置了）只负责措辞，不负责产生任何数字。** 每次调用 LLM 后，
输出会与"喂给它的事实文本"做数字核对（`app/services/ai_generate.py::ungrounded_numbers`）：LLM 输出里如果出现了
事实文本中没有的数字/带单位规格（比如凭空写出"±5%"的公差），这段文本会被丢弃，改用数据模板文字，
并在响应的 `notes` 字段里如实说明为什么被丢弃。没有配置 LLM 时，所有五个功能都能用模板文字正常工作
（`generated_by: "template"`）。生成的 SOP 始终是**草稿**，需要人工审核。

## 1. 维护：SOP 和 ROI 由 AI 生成

- `POST /ai/sop/generate`：结合设备型号、平台上已有的规范 SOP（若有，原文保留，AI 只补充措辞）、当前活动报警、
  未关闭工单、最近一次 AI 诊断、备件库存、最近一次化验比对、知识库检索结果，生成一份带草稿标识的 SOP。
  没有规范 SOP 时会用通用步骤，并**明确标注是通用步骤**，提示对照厂家手册补全。可选 `save: true` 保存到
  `maintenance_sop` 表，版本号固定为 `AI-draft`，**永远不会覆盖或displaced人工审核过的规范版本**——
  查询时规范版本（version 不是 `AI-draft`）优先，后续再次生成也只会引用规范版本，不会在 AI 草稿基础上继续生成。
- `POST /ai/roi/generate`：从资产购置价值、维护记录、工单、报警计算维护成本占比、账面净值、故障性工单占比等
  成本侧指标。**ROI% 和回收期只有你提供 `annual_benefit`（年化收益）时才会计算**——平台不记录设备带来的收益，
  没有这个输入就只显示成本侧指标，并在 `data_gaps` 里说明。所有公式和假设都列在 `formulas`/`assumptions` 里。

## 2. 数据分析：AI 读取

- `POST /ai/analysis/read`：对给定序列做统计（均值/标准差）、线性趋势拟合（含 R²）、离群点（稳健 z-score）、
  卡死读数、缺失读数、成分总和检查（如 100% 气体组成）、序列间相关性，全部先算出结构化 `findings`，再生成一段
  简短解读。可以直接传 `series`（比如 Data Analysis 页面当前选中的曲线数据），也可以传 `device_id` + `hours`
  从服务器实时拉取该设备近期数据。Data Analysis 页面新增"AI Reading"页签，两种来源都可选。

## 3. 知识库：与 AI 深度绑定

- `POST /ai/knowledge/ask`：回答只使用知识库检索到的片段（和可选的设备实时数据），并用 `[n]` 标注引用来源。
  **知识库里没有相关内容时会如实说"没有找到"，不会编造答案。** 没有配置 LLM 时，直接引用知识库原文并标注来源
  （`generated_by: "extractive"`）。
- `POST /ai/diagnose` 的返回新增 `references` 字段：诊断结果自动带上与该故障相关的知识库片段。

## 4. 实验室：相关性/对比分析由数据分析数据决定，并加入 AI 认证

- **在线值不是手填的。** 新增 `POST /lab/analysis-compare`：对每一条化验样本，在其采样时刻前后 ±窗口分钟内，
  从该设备该组分**实际的测量数据**（即 Data Analysis 页面看到的数据）中取值（均值/中位数/最近点，可选），
  作为在线值，与化验值配对，计算回归、偏差、MAPE、95% 一致性界限等统计量。
- `POST /lab/certify`（**AI 认证**）：把上面的比对结果对照一组可配置判据（最少配对数、R²、平均绝对偏差、
  系统偏差及其显著性、随时间漂移的显著性）逐项判定，得出 `certified / conditional / failed / insufficient`，
  每项判据都有中英文说明和✓/✗，并保留判定留痕（`lab_ai_certifications` 表：判据、每项检查结果、统计量、
  文字说明、生成方式、有效期）。同时会刷新原有的 `lab_correlation_results` 表，使已有的 Lab → Correlation 视图
  也反映基于数据分析数据的结果。
  **这不是计量认可（CNAS/ISO 17025），也不是法定检定**，每次结果都会带上这条免责声明；默认阈值是内部假设值，
  应按你们自己的验收标准通过 `criteria` 覆盖。
- `GET /lab/certifications?latest=true`：各设备各组分最新认证结果一览，Lab 页面新增"Comparison"页签展示。

## 新增/变更接口

| 接口 | 说明 |
|---|---|
| `POST /api/v1/ai/sop/generate` | AI 生成 SOP 草稿；`save=true` 时保存为 `AI-draft` 版本 |
| `POST /api/v1/ai/roi/generate` | AI 生成 ROI/TCO 分析；提供 `annual_benefit` 才有 ROI%/回收期 |
| `POST /api/v1/ai/analysis/read` | AI 读取数据分析数据（服务器设备数据或内联 series） |
| `POST /api/v1/ai/knowledge/ask` | 知识库问答，带引用，找不到时如实说明 |
| `POST /api/v1/lab/analysis-compare` | 化验 vs 在线比对，在线值来自数据分析数据 |
| `POST /api/v1/lab/certify` | AI 认证：判据判定 + 留痕 + 免责声明 |
| `GET /api/v1/lab/certifications` | 认证历史/最新一览 |
| `POST /api/v1/ai/diagnose` | 返回新增 `references`（知识库引用） |

## 新增文件

- 纯逻辑（无 DB/框架依赖）：`app/services/analysis_reading.py`（统计/趋势/异常/成分和/相关性）、
  `app/services/roi_analysis.py`（成本侧指标与 ROI）、`app/services/lab_analysis.py`（化验配对、比对统计、认证判定）、
  `app/services/ai_generate.py`（提示词、离线模板、LLM 输出数字护栏）
- 取数层：`app/services/studio_data.py`；接口：`app/api/v1/ai_studio.py`；Schema：`app/schemas/ai_studio.py`
- 数据模型：`app/models/maintenance.py` 新增 `LabAICertification`（`lab_ai_certifications` 表）
- 测试：`tests/test_ai_studio_logic.py`（纯逻辑，72 项）、`tests/test_ai_studio.py`（接口集成测试）

## 行为说明（请留意）

- 认证判据阈值、ROI 的维护成本占比/故障工单占比预警阈值、数据分析的趋势/离群点/成分和判定阈值，
  **都是内部假设值**，不是行业标准，可在对应模块里覆盖。
- 成分总和检查会跳过完全没有数据的组分，并在结果里注明跳过了哪些，避免一个空 TAG 导致整个检查失真。
- ROI 的工单类型归类（校准/巡检/保养=预防性，维修/更换/故障=故障性）是启发式规则，写在 `assumptions` 里。
- LLM 数字护栏对"两位数以内的纯数字"（如步骤编号、引用编号 `[3]`）放行，但只要带单位或百分号就必须能在
  原始数据里找到同一数值同一单位，否则视为编造。
- 前端新增内容（AI 生成的 SOP 正文除外——它作为 `textContent` 插入，避免任何注入风险）对模型名、设备名、
  知识库标题等一律做 HTML 转义。

## 运行测试

```bash
pytest tests/test_ai_studio_logic.py tests/test_ai_studio.py tests/test_device_selection_logic.py tests/test_ai_devices.py
pytest            # 全量回归
```
