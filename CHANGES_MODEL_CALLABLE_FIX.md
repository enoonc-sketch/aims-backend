# 修复：本地模型"已下载但不可调用"

| # | 问题 | 现象 | 修复 | 位置 |
|---|------|------|------|------|
| 1 | 提示词超出上下文窗口 (n_ctx=4096)。聊天/RAG 会把设备、网关、报警、工单、知识库等实时数据全拼进 prompt，中文很容易超限；llama.cpp 抛 `exceed context window`，被吞掉后静默退回"离线模式"，且不记录原因 | 模型明明加载成功，聊天却始终是离线文案 | 调用前按 token 预算裁剪（保留开头指令 + 结尾问题，截掉中间）；超限再二次缩减重试；embedding 同样裁剪到 512 | `llm_local.chat_local / embed_local` |
| 2 | 推理失败不记录原因 | 界面看不出为什么不可用 | 失败原因写入 `unavailable_reason`，并卸载可能卡死的句柄下次重载 | `llm_local.py` |
| 3 | llama.cpp 上下文非线程安全，接口经线程池并发调用 | 并发请求可能崩溃/乱码 | 按 llm / embedding 分别加推理互斥锁 | `llm_local.py` |
| 4 | `unload()` 用 `pop(kind)`，缓存键实为 `kind:core_id` | 切换模型后旧模型不释放 | 按前缀匹配卸载 | `llm_local.unload` |
| 5 | 旧 SQLite 库缺 `ai_core_packs.storage/file_path/sha256` 列（`create_all` 不加列） | 模型列表/切换/聊天全部 500 | 启动时自动补列 | `db/init_db.py` |
| 6 | 前端把 401/5xx 都显示成"后端不可达"并随机给假回复；聊天超时仅 60s | 未登录/令牌过期看起来像"模型不可调用"；冷启动加载大模型易超时 | 显示真实 HTTP 错误与原因；超时放宽到 180s | `frontend.html` |

新增测试：`tests/test_llm_local_fit.py`（假 llama 句柄，无需模型/llama-cpp）。
