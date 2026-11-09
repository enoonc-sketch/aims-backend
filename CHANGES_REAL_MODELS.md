# 真实模型改造说明 (2026-09)

## 背景

`AI Model / Core Packs` 页面原来下载的"模型"其实都是几 KB 的规则/配置 JSON 文件，不是训练过的模型：
- 诊断 = 关键词字符串匹配
- 预测 = 取最近两个点做直线外推
- Chat/RAG = 词袋算法 + （可选）调用外部付费 LLM API，没有 AI_API_KEY 时只返回固定文案

这次改造把四块都换成真正能跑的模型，同时保留原有规则/配置作为兜底。

## 改了什么

### 1. 下载机制 (`app/services/ai_cores.py`, `app/models/ai.py`)
- `ai_core_packs` 表新增 `storage` / `file_path` / `sha256` 三个字段（见迁移
  `migrations/versions/002_ai_core_packs_binary_storage.py`）
- 大文件（LLM / embedding 的 GGUF 权重）改成**流式下载直接落盘**，不再整个读进内存塞进数据库
  JSON 字段；数据库只存路径、大小、sha256
- 小的规则/配置 JSON 包（诊断规则、健康评分权重、RAG 提示词）行为不变，仍然存数据库

### 2. 真实模型清单 (`cores/manifest.json`)
新增 4 个可下载的真实模型（都是 HuggingFace 上验证过存在的开源 GGUF 权重）：

| id | 内容 | 大小 | 用途 |
|---|---|---|---|
| `llm-qwen2.5-1.5b-instruct-q4` | Qwen2.5-1.5B-Instruct Q4_K_M | ~1.1GB | 本地对话 LLM，最快 |
| `llm-qwen2.5-3b-instruct-q4` | Qwen2.5-3B-Instruct Q4_K_M | ~1.9GB | 本地对话 LLM，均衡（默认） |
| `llm-qwen2.5-7b-instruct-q4` | Qwen2.5-7B-Instruct Q4_K_M | ~4.7GB | 本地对话 LLM，质量最好 |
| `embedding-bge-small-zh` | BGE-small-zh-v1.5 F16 | ~48MB | RAG 检索用中文 embedding |

三个 LLM 都已集成，运行时可以互相切换，不需要重启后端。

### 3. 本地推理 (`app/services/llm_local.py`, `app/services/model_registry.py`)
- 用 `llama-cpp-python` 跑下载好的 GGUF 模型（纯 CPU 可跑）
- `model_registry.py` 用一个 JSON 小文件（`cores/models/active.json`）记录当前激活的
  LLM / embedding，不用改数据库结构就能运行时切换
- 新增接口：
  - `GET /ai/models` — 查看当前激活模型 + 是否已加载
  - `POST /ai/models/active` — 切换激活模型 `{"kind": "llm", "core_id": "llm-qwen2.5-3b-instruct-q4"}`

### 4. Chat / RAG (`app/api/v1/ai.py`, `app/services/rag.py`, `app/api/v1/knowledge.py`)
调用优先级统一改成：**本地真模型 → 外部 API（如果配了 AI_API_KEY） → 原来的兜底文案/词袋算法**
- RAG 检索的 embedding 相似度，现在优先用真的 embedding 模型算余弦相似度，不再是纯词袋

### 5. 预测 (`app/services/forecasting.py`)
不再是"取最后两个点做直线"，改成：
- 数据点 ≥10 个：Holt 指数平滑（趋势型），用 `statsmodels`
- 数据点 3-9 个：全部历史点做最小二乘线性回归
- 数据点 <3 个：保留原来的兜底逻辑

### 6. 诊断 (`app/services/anomaly.py`)
新增一个真正训练出来的模型：针对每台设备的每个部件，用 `scikit-learn` 的
`IsolationForest` 在**该设备自己的历史数据**上做无监督异常检测训练（不需要你们目前没有的
标注故障数据）。训练结果会和规则表的诊断结论做融合，调高/调低置信度。
- `POST /ai/diagnosis/train?device_id=1&component_name=Methane&days=30` 训练/重新训练
- 模型文件存在 `cores/models/diagnosis/device{id}_{component}.joblib`

### 7. 前端 (`frontend.html`)
- `AI Model / Core Packs` 页面区分展示"AI Core (规则/配置)"、"Real Local LLM"、
  "Real Embedding Model" 四类，显示参数量/量化方式
- 大文件下载的前端超时从写死的 4 秒改成 30 分钟（真模型下载慢，别被前端提前掐掉）
- 已下载的 LLM/embedding 模型可以点"Set Active"切换

## 还没做 / 需要你知道的限制

1. **打包 EXE**：`llama-cpp-python` 是编译好的 C++ 扩展，PyInstaller 打包这类库通常需要在
   `build_exe.spec` 里手动加 `binaries`/`hiddenimports`，这次没有验证这一步，大概率需要你们
   本地跑一次打包踩一遍坑。
2. **诊断模型**：IsolationForest 是无监督异常检测，判断"这个读数是否反常"，不是"这是什么故障"
   的分类器——后者需要你们有标注好的故障数据（症状 -> 确认的根因）才能训练监督分类模型，目前
   系统没有采集这类标注数据。
3. 所有改动在这个沙盒环境里**没有真正跑起来验证过**（沙盒没有网络，装不了
   `llama-cpp-python`/`statsmodels`/`scikit-learn` 这些新依赖），只做了 Python 语法检查。
   建议拉到本地后：
   ```bash
   pip install -r requirements.txt --break-system-packages
   alembic upgrade head          # 或者直接删掉旧 sqlite 库重新跑，AUTO_INIT_DB=true 会自动建表
   uvicorn app.main:app --reload
   ```
   然后去 AI Center → 模型下载 页面点 Qwen2.5-3B 试跑一次下载 + 对话。
4. 3B/7B 模型在弱 CPU 上跑起来可能比较慢（几秒到十几秒一条回复），建议先用 1.5B 验证链路通了
   再考虑升级到 3B/7B。

## 追加：海外模型 (2026-09-28)

`cores/manifest.json` 新增 5 个非 Qwen 的本地 LLM（均为 Q4_K_M GGUF，走原有的下载/切换/推荐流程，无需改代码）：

| id | 模型 | 大小 | 备注 |
|---|---|---|---|
| `llm-llama3.2-1b-instruct-q4` | Meta Llama-3.2-1B-Instruct | ~0.8GB | 最轻，英文为主 |
| `llm-llama3.2-3b-instruct-q4` | Meta Llama-3.2-3B-Instruct | ~2.0GB | 均衡，英文好、中文一般 |
| `llm-gemma3-4b-it-q4` | Google Gemma-3-4B-IT | ~2.5GB | 多语言，中文相对好 |
| `llm-phi4-mini-instruct-q4` | Microsoft Phi-4-mini-Instruct | ~2.5GB | 推理/数学强，英文为主，MIT |
| `llm-mistral-7b-instruct-v0.3-q4` | Mistral-7B-Instruct-v0.3 | ~4.4GB | 需要更多内存，Apache-2.0 |

- 许可证：Llama 3.2 与 Gemma 各有自己的使用条款，商用前请确认；Phi-4-mini 为 MIT，Mistral 为 Apache-2.0。
- 未验证：沙盒无法下载权重，链接/文件名已通过 Hugging Face 页面核对，但没有实际下载+推理跑过。
- 默认激活模型仍是 Qwen2.5-0.5B（`model_registry.py`），可在 AI Center → 模型下载 里切换。

## 追加：动态小模型推荐 (2026-09-28)

`GET /ai/models/recommend` 现在默认按"小模型优先"推荐，每次请求都重新读取本机当前可用内存/CPU/GPU。

参数（均可选）：
- `prefer=small|quality`，默认 `small`。`quality` 保持旧行为（内存够就推最大的）。
- `lang=zh|en`，默认 `zh`。`zh` 时会把模型的中文能力算进排名。
- `max_params_b`，默认 `4`。小模型模式下只考虑不超过这个参数量（单位 B）的模型。

推荐逻辑（`app/services/model_recommend.py`）：
1. 候选 = 参数量不超过上限，且内存放得下、速度够、推理引擎版本够新；
2. 优先选内存"充足"的，其次才是"紧张"的；
3. 按 `score = 参数量(B) + zh_score × 0.8` 排序（`lang=en` 时只看参数量），已下载的在同分时优先；
4. 什么都放不下时，退回到引擎能加载的最小模型；
5. 返回里新增 `small_llm_ids`（前 3 名）、`llama_cpp_version`，每个模型带 `family / score / engine_ok / min_llama_cpp`。

manifest 里新增的字段（`cores/manifest.json`）：
- `family`：模型系列。
- `zh_score`（1-3）：我对各模型中文能力的粗略主观评级，不是基准测试结果，觉得不准直接改这个数。
- `min_llama_cpp`：该模型要求的最低 llama-cpp-python 版本。Gemma-3 和 Phi-4-mini 我保守设成了 `0.3.9`，
  这是估计值，没有在你的环境实测；版本过低时该模型标为"不可用"并且不会被推荐。

前端：AI Center → 模型下载 页的推荐面板改为请求小模型模式，引擎版本过低的模型会用红字标出需要的版本。

测试：`tests/test_model_recommend.py`。全量测试里有 3 个用例失败
（`test_default_admin_login`、`test_external_data` 两个），在未修改的原始项目里同样失败，与本次改动无关。

### 再追加：按系列去重 (`diversify`)
`/ai/models/recommend` 新增 `diversify`（默认 `true`）：`small_llm_ids` 里每个系列（Qwen / Llama / Gemma…）最多占一个名额，
避免前 3 名全是 Qwen 的不同尺寸。第 1 名不变，只影响备选；前端在备选行标 "备选 #2/#3"。`diversify=false` 恢复纯按分数排序。

### 再追加：优先海外模型开关 (`overseas_first`)
- `/ai/models/recommend?overseas_first=true`：所有能跑的非 Qwen 模型排在 Qwen 之前，Qwen 只作为备选；
  如果这台机器一个海外模型都放不下，仍会回退到 Qwen，不会返回空。默认 `false`，行为不变。
- manifest 每个 LLM 新增 `origin` 字段（`cn` / `overseas`），目前只有 Qwen 标为 `cn`。
- 前端推荐面板加了复选框"优先推荐海外模型"，选择记在浏览器 localStorage，刷新后保留。
