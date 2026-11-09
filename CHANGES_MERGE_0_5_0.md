# 0.5.0 — 三线融合

| 来源 | 内容 |
|---|---|
| chain（0.3.0 基线 + 闭环） | 网关入口 `/communication/ingest`、数据质量分级、报警→诊断→知识库→工单→维修结果→AI 反馈、`/alarms/{id}/chain` |
| 0.4.0 industrial core | `device_tags` / `communication_connections` / `tag_mappings` / `acquisition_events` / `data_quality_events`、导入预检、细粒度权限、`ai_model_versions` |
| 0.4.1 安全加固 | SECRET_KEY 注入、无默认 admin、无 demo JWT/SHA256、CORS 白名单、HTTPS、登录限流、refresh、审计、上传检查 |

## 合并方式
以 chain 为底，把 industrial 与加固叠上去。`devices.py` / `maintenance.py` / `measurements.py` / `frontend.html` 是双方都改过的文件，做了三方合并（无冲突）；其余文件按来源直接采用。

## 融合时发现并处理的冲突
1. **迁移编号撞车**：chain 与 industrial 都叫 `007`。现为线性链：`006 → 007_industrial_core → 008_chain_feedback_loop → 009_security_hardening`（已由测试锁定不分叉）。
2. **两份 `data_quality.py`**：chain 的是"读数评估器"，industrial 的是"单条采集事件规则检查"。合并为一个模块，两套 API 都保留。
3. **调度器降级被 chain 回退**：chain 基于 0.3.0，没有"APScheduler 缺失时降级"的修复，已恢复。
4. **chain 报告的安全隐患**（`_repair_default_admin` 会把自定义密码重置回 admin123）：随默认账号一起删除，无需再修。
5. **`test_default_admin_login.py`** 删除（测的是被禁止的行为）。

## 两条线的真正接缝（新增行为）
- `POST /industrial/acquisition-events`：原始事件照旧入库，**同时**送进 `data_chain.process_readings`（质量→报警→诊断→工单）。bad 质量不触发工艺报警；同 (设备, 组分, 时间) 不重复写入。响应新增 `chain` 字段。
- `POST /industrial/ai-feedback`：写 `ai_feedback` 的同时，若带 `diagnosis_id`，写入闭环用的 `ai_diagnosis_feedback`，从而参与置信度校准。诊断对错只接受工程师给的 `diagnosis_verdict`，不给就是 `unrated`。
- `industrial/models` 注册与 AI 反馈补上审计与鉴权（原先 `models` 接口没有获取当前用户）。

## 仍然是两套并行的东西（未强行合并，需要你决定）
- **TAG 两套模型**：chain 用 `device_components(gateway_id, tag_address)`，industrial 用 `device_tags + tag_mappings + communication_connections`。网关推送走前者，采集事件走后者，二者目前没有自动同步。建议以后选一套为准。
- **反馈两张表**：`ai_feedback`（industrial）与 `ai_diagnosis_feedback`（chain）；上面的桥接只是单向写入。

## 运维注意
- 网关/采集程序调用 `/communication/ingest` 需要用户令牌，而 access 令牌现在只有 15 分钟：请为采集程序建专用账号并使用 `/auth/refresh`。
- `scan-stale` 仍需外部定时调用（chain 未做内置调度，本次也没做）。
