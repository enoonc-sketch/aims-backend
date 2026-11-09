# AI 选型 ↔ AI Studio 打通、跳转补全、一键工单、索引优化

在上两轮（AI 中心设备搜索/选型、AI Studio 五项功能）之上做的四项加强。

## 1. AI 选型评分纳入 AI 认证与 ROI 成本健康度

之前"设备选型"评分只看健康分/在线率/报警工单，看不到已经做过的化验认证结果和 ROI 成本情况。现在：

- **化验认证证据**：`app/services/device_search.py::load_profiles` 会取每台设备每个组分最新的 AI 认证记录
  （`lab_ai_certifications`），选型时如果设备**已经被认证过**且认证结果覆盖了需求里要求的组分，会据此加分/减分——
  `certified` 加满分、`conditional` 打五折、`failed` 记零分、过期的认证打三折。**从未认证过的设备不受影响**，
  不会因为"没做过认证"被扣分。
- **成本健康度**：同时用设备自己的资产 + 近 12 个月维护记录/工单，实时算一遍 `app/services/roi_analysis.py` 的
  成本侧指标（不需要年化收益输入），把"维护成本占比偏高""故障性工单偏多"这类信号折算成选型扣分；
  "接近折旧期末"只作为提示，不影响分数（因为一台老设备现在仍然可能是测这个组分的最佳选择）。
  **没有关联资产的设备不受影响**。
- 型号级推荐（`rank_models`）同样纳入：认证证据取该型号所有在用设备里**最好的一条**（不会被某一台设备的失败认证拖累
  整个型号）；成本扣分要求**过半数**在用设备都有同一个问题标记才会计入（一台设备有问题不会拖垮整个型号的推荐）。

新增/调整测试：`tests/test_device_selection_logic.py`（`TestCertificationSignal`/`TestCostHealthSignal`/
`TestModelRankingSignals`，17 项）、`tests/test_ai_devices.py`（两个真实 DB 集成测试）。

## 2. AI 中心 360 跳转面板补上认证与 ROI

`build_links()` 新增两项：
- **"化验认证"**：数量 = 有过认证记录（含失败）的组分数，详情列出 `组分:状态`；点击跳转到 Lab 页的"相关性/对比"
  页签并预选该设备。
- **"ROI / 维护成本健康度"**：数量 = 触发的标记数，详情是标记代码列表；点击跳转到 Maintenance 页的 ROI 页签
  并预选该设备。

## 3. 认证未通过/有条件通过时一键创建工单

Lab 的 AI 认证结果卡片，状态为 `failed` 或 `conditional` 时会出现"创建工单"按钮（已认证通过则不显示，因为不需要
动作），按钮点一次即锁定防止重复创建；工单类型固定为 `calibration`，优先级按状态区分（`failed`→高，
`conditional`→中）；创建成功后显示工单号和"打开维护页"链接（直接跳到批量导入/工单列表页签）。

## 4. `lab_ai_certifications` 索引与"最新记录"查询改为 SQL 级

- 模型和建表 SQL 都加了 `(device_id, component_name)` 复合索引。
- `app/services/studio_data.py` 新增 `latest_certification_ids()` / `latest_certifications_by_device()`
  ——用 `MAX(id) ... GROUP BY (device_id, component_name)` 取每个设备每个组分最新一条记录的 id，
  这两个函数同时服务于：`GET /lab/certifications?latest=true` 接口（原来是"取最近 500 条再在 Python 里去重"，
  数据量大时可能因为超过 500 条而漏掉某些设备/组分，现在改成正确的 SQL 查询）和上面第 1 点的选型评分。
  写法在 SQLite 和 Postgres 上都能跑（用的是可移植的子查询写法，不是 Postgres 专有的 `DISTINCT ON`）。

## 尚未处理（本轮范围之外）

上一轮回复里提到的其他几项（端到端测试、SOP 导出、认证到期提醒、批量认证）本轮没有做，仍然是已知缺口。

## 运行测试

```bash
pytest tests/test_device_selection_logic.py tests/test_ai_devices.py tests/test_ai_studio.py tests/test_ai_studio_logic.py
pytest   # 全量回归
```
