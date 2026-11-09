# 数据导入 & AI 中心增强说明 (2026-09)

这次改动覆盖用户流程里的四块：**导入数据 → AI 中心诊断 / 预测 / 健康评分**，
在原有实现基础上补齐了一个真正的缺口（服务端文件导入），并增强了另外三块的能力。

## 1. 数据导入 — 新增 `POST /measurements/import`

之前只有 `POST /measurements/batch`（要求调用方自己把 Excel/CSV 解析成 JSON 数组），
`measurement_imports` 表和 `Measurement.import_id` 字段其实已经建好了，但从来没有一个
接口真正写入过它们 —— 也就是说"文件导入"这块此前是有模型没有 API 的空壳。

新增接口：

```
POST /api/v1/measurements/import
Content-Type: multipart/form-data
  device_id: int
  file: .csv / .xlsx / .xls
  time_column: 可选，手动指定时间列名
```

- 自动识别时间列（`time/timestamp/date/时间/采集时间/日期` 等）
- 自动识别两种常见格式：
  - **宽表**：一列时间 + 每个组分一列数值（如 `Time, Nitrogen, Methane, ...`）
  - **长表**：显式的 `Component/Value`（或 `组分/数值`）两列，一行一个读数
- 批量写入 `measurements`，并记录一条 `measurement_imports`（文件名/行数/列数/状态），
  返回识别出的列、插入行数、格式、告警信息（如有多少行时间戳解析失败被跳过）
- 导入后的数据立刻可被 AI 中心（诊断 / 预测 / 健康评分）读取，因为它们本来就是直接查
  `measurements` 表，无需额外接线

新增依赖：`pandas`、`openpyxl`（已加入 `requirements.txt` / `requirements-exe.txt`）。
测试见 `tests/test_measurements_import.py`（宽表/长表/xlsx/非法文件/找不到设备/时间列解析失败）。

## 2. AI 诊断 — 症状可自动识别 + 规则包扩充

**之前**：`POST /ai/diagnose` 的 `symptom` 是必填字段，需要人先看数据、判断是漂移还是
噪声，再手动传给接口 —— 相当于"AI"只做了匹配这一步。

**现在**：`symptom` 改为可选。不传时，`app/services/symptom_detect.py` 会直接用该组
分最近 24 小时的读数做统计判断（前后半段均值偏移 → 判定漂移方向；标准差/均值比例过
高 → 判定噪声），推断出症状关键词再交给规则包匹配，响应里新增 `symptom_auto_detected`
字段说明是否为自动推断。传入显式 `symptom` 时行为与之前完全一致，不影响现有调用方。

规则包 (`cores/ai/diagnosis_rules.json`，已同步到 `cores/vendor/ai/`) 从 7 条规则扩
到 14 条，新增 CO2、H2S、Nitrogen、重组分(C5+/C6+)、采样压力、温度、校准偏移等常见
故障场景，并新增 `severity` 字段（`high` 严重级别的诊断结果会在文案前加
`[HIGH SEVERITY]` 标记）。

## 3. AI 预测 — 置信区间 + 趋势标签

`app/services/forecasting.py` 原来只返回预测值本身。现在同时返回：

- **95% 置信区间**（`lower`/`upper`，随预测步数增大而变宽，体现"越往后越不确定"）：
  基于拟合模型的样本内残差标准差估计
- **趋势标签** `trend`: `"rising" | "falling" | "stable"`（预测起点/终点相对均值的
  变化幅度小于 2% 记为 stable，避免把噪声当趋势）

`PredictionPoint` 新增 `lower`/`upper`，`PredictionResult` 新增 `trend`，均为可选字段，
不影响老客户端。

## 4. 健康评分 — 引入数据可用性 + 训练模型异常度

`GET /ai/health/{device_id}` 之前只看设备状态 + 活跃告警两项；`health_weights.json`
里其实已经定义了 `low_data_availability` 惩罚项，但代码从未使用过。现在：

- **数据可用性**：过去 24 小时该设备的测量点数低于 `min_expected_points_24h`
  （默认 4）时扣分 —— 一台已经停止上报数据的分析仪，本身就是健康信号，不需要等告警
  触发
- **训练模型异常度**：对每个已经用 `/ai/diagnosis/train` 训练过异常检测模型的组分，
  复用同一个 IsolationForest 模型对其最近读数打分，异常比例越高扣分越多（新增
  `trained_anomaly_detected` 权重），并在 `factors.anomalous_components` 里列出具
  体是哪些组分触发的

`health_weights.json` 同步更新（已同步到 vendor 副本），新增 `min_expected_points_24h`
配置项和对应的权重说明。

## 兼容性

以上改动都是新增可选字段 / 新增可选参数，没有修改任何现有接口的必填参数或删除字段，
现有前端/调用方不需要跟着改。

## 未验证部分

本次改动在沙箱环境中做了能验证的部分：JSON 规则包语法、导入解析逻辑（用真实 pandas
构造宽表/长表/中文表头样例跑通）、预测置信区间与趋势判断的数值行为、症状自动识别的
边界情况。但沙箱没有网络，装不了 `fastapi`/`sqlalchemy`/`statsmodels` 等完整依赖，
所以没能跑通 `tests/` 目录下依赖完整 FastAPI 应用的集成测试（包括新增的
`test_measurements_import.py` 和 `test_ai.py` 里新增的两个用例）。建议在有完整依赖
的开发环境里跑一次 `pytest` 确认。
