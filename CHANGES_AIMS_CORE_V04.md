# AIMS Backend 0.4.0 — Industrial Core补强

## 本次补充

### 1. 工业数据模型
- `communication_connections`：网关下的真实通信连接
- `device_tags`：独立TAG对象，设备与测量点解耦
- `tag_mappings`：TAG → 通讯连接 → 地址/寄存器/NodeID映射
- `acquisition_events`：原始采集事件、工程值、源时间、接收时间、质量码
- `data_quality_events`：越限、坏质量码、时间延迟等数据质量事件

### 2. 导入预检
新增 `POST /api/v1/import-validation/preview`
- 自动识别中英文列名
- 检查设备是否存在
- 检查TAG/组分是否存在
- 检查数值
- 检查时间戳
- 返回 `passed / warning / rejected`
- 不通过时不写入正式测量数据

### 3. AI闭环
- `ai_model_versions`：模型版本、artifact、sha256、指标、部署状态
- `ai_feedback`：诊断→工单→根因→纠正措施→验证结果

### 4. 权限
新增细粒度权限检查：
- device.read/write
- tag.read/write
- communication.read/write
- measurement.read/write/import
- alarm.ack/close
- maintenance.create/close
- knowledge.write
- ai.run/admin/feedback

### 5. 审计
工业核心写操作记录 `AuditLog`。

### 6. 生产安全
生产环境强制：
- SECRET_KEY 至少32字符且不能为默认值
- CORS 不允许 `*`
- 没有 bcrypt 时拒绝不安全密码哈希 fallback
- 没有 python-jose 时拒绝无签名 JWT fallback
- 根接口不再返回默认管理员口令

### 7. 稳定性
APScheduler 缺失时，外部数据自动刷新功能降级为 disabled，不再导致整个 API 写接口 500。

## 数据链

`Device → DeviceTag → CommunicationConnection → TagMapping → AcquisitionEvent → DataQualityEvent → Measurement/Alarm → AI Diagnosis → WorkOrder → AIFeedback`

## 数据库

新增 Alembic migration：`007_industrial_core`。

## 测试

当前测试结果：**290 passed, 1 skipped**。
