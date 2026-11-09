# 纵向打穿：设备 → TAG → 通讯 → 实时数据 → 数据质量 → 报警 → AI诊断 → 知识库 → 工单 → 维修结果 → AI反馈

## 改动前：这条链在哪里断
| 环节 | 现状 |
|---|---|
| 通讯 → 实时数据 | 通讯只能被 ping；`gateway_id`/`tag_address` 从未被用来解析任何一条数据，数据只能手工 POST/导入 |
| 数据质量 | `measurements.quality` 永远写 "good"，从不被计算，也没人读 |
| 报警 | 只查 `active`：报警被确认后下一个超限读数会再开一条报警+一张工单；报警时间是入库时刻而非读数时刻 |
| 报警 → AI诊断 | `AIDiagnosis` 没有 `alarm_id`，纯手动；诊断把坏数据也当证据；知识库引用不落库 |
| 知识库 → 工单 | 工单只有一行标题 |
| 工单 → 维修结果 → AI反馈 | `close` 只改状态；没有"修好没/根因/AI 对不对"，AI 永远不知道自己准不准；PATCH done 不写 closed_at |

## 改动后
- **通讯入口** `POST /communication/ingest`：按 (网关, tag_address) 解析到 TAG→设备+组分；未映射地址逐个返回；重传幂等；收到数据置网关/设备 online。
- **通讯中断** `POST /communication/scan-stale`：无状态，外部定时调用；开「通讯中断」报警（走同一条诊断→工单链），数据恢复自动关闭。
- **TAG 配置** `POST /devices/{id}/components`：逐个配置 TAG + 网关 + 地址；同网关下地址唯一。
- **数据质量** `services/data_quality.py`：good/uncertain/bad。**只有确定不可信的（NaN、未来时间戳、设备自报 bad）标 bad 并压制工艺报警；统计类判断（尖峰/冻结/超量程/过旧）只标 uncertain，不压制报警**——真实工艺突变在统计上就是"尖峰"，不能吞掉。连续 3 条 bad → 「数据质量」报警。
- **统一入口** `data_chain.process_readings`：单条/批量/文件导入/云数据同步/网关推送全部走它（质量评估→写库→TAG→报警→诊断→工单）。
- **报警→诊断→知识库→工单** `services/diagnosis_flow.py`：确定性、不依赖网络/LLM（入库路径不会因 AI 变慢或失败；诊断异常只记日志，报警和工单照建）。依据（KB 摘录/SOP/证据统计/校准说明）存入 `AIDiagnosis.evidence`，工单描述带上。`/ai/diagnose` 复用同一逻辑，新增可选 `alarm_id`。
- **维修结果→AI反馈** `POST /maintenance/work-orders/{id}/complete`：写维修记录（结果/根因）；fixed/no_fault_found→结单并关报警，partially_fixed/not_fixed→工单保持 in_progress；写 `AIDiagnosisFeedback`；修复成功的案例写回知识库。诊断对不对（`diagnosis_verdict`）**由工程师给，不给就是 unrated，系统不替人猜**。
- **学习回路**：同组分同类症状已有 ≥3 条已评价复盘时，置信度 = 50% 规则包 + 50% 历史准确率；样本不足不校准并在依据里说明。
- **全链路视图** `GET /alarms/{id}/chain`：十一环各自状态 + `break_points` 明确指出哪一环断了。
- 其他：`/alarms/{id}/diagnose`、`/ai/diagnoses`、`/ai/diagnoses/{id}/feedback`、`/ai/feedback/pending|stats`。
- 迁移：`007_chain_feedback_loop`（幂等）；SQLite 独立 EXE 由 `_ensure_sqlite_chain_columns` 补列。

## 行为变化（需要知道）
- `POST /measurements/batch` 响应新增质量统计与报警计数字段（原 `inserted` 保留）。
- 同一规则上报警未结案（active **或 acknowledged**）时不再开新报警。
- 报警的 `triggered_at` 现在是触发它的读数时间。
- bad 质量读数被排除在 诊断/预测/健康评分/异常模型训练 之外。
- 云数据同步改为按设备批量走 `process_readings`。

## 测试
`tests/test_chain_e2e.py` 16 项：网关推送一路走到 AI 反馈的完整闭环、"第二次诊断引用第一次写回的维修案例"、坏数据/存疑数据、确认后不重复开单、幂等重传、通讯中断与恢复、断链定位、置信度校准等。全量 319 通过。

## 未处理（不在这条链上，但你应该知道）
- `tests/test_default_admin_login.py::test_repair_fixes_only_the_known_broken_hash` 在改动前就失败：`_repair_default_admin` 的第二个条件（`last_login_at is None` 且哈希不匹配 admin123）会把**从未登录过但已自定义的管理员密码重置回 admin123**，与该测试和函数注释矛盾。这是个安全隐患，建议只保留"哈希等于已知坏值"这一个条件。
- 尚未做：调度器自动定时调用 `scan-stale`、诊断结果的向量化（案例写回知识库时未算 embedding，目前靠关键词/FTS 命中）、前端 `frontend.html` 未改动。
