# NPCSidekick — 架构设计文档（引擎: NPCSidekick v3.2）

## 系统概览

NPCSidekick 的引擎 **NPCSidekick**（NPCSidekick）是一个 **4 层 AI Agent 框架**。本文档描述引擎架构；NPC 层（感知/欲望/目标/游戏行动）已落地，见 `docs/NPC大脑架构.md`。

```
                              ┌─────────────────────────┐
                              │       AgentSettings      │
                              │    (可注入 dataclass)     │
                              └───────────┬─────────────┘
                                          │ 注入
  ┌───────────────────────────────────────┼───────────────────────────────────┐
  │                        AgentOrchestrator                                 │
  │                                                                          │
  │   User Input                                                            │
  │       │                                                                  │
  │       ▼                                                                  │
  │   ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐          │
  │   │ Planner  │───▶│ Executor │───▶│  Tool    │───▶│  Memory  │          │
  │   │  (L1)    │    │  (L2)    │    │  Router  │    │  (L4)    │          │
  │   │          │    │          │    │  (L3)    │    │          │          │
  │   │ 任务分解  │    │ 逐步执行  │    │ 统一路由  │    │ 记忆持久  │          │
  │   └──────────┘    └─────┬────┘    └─────┬────┘    └────┬─────┘          │
  │                         │               │              │                 │
  │                         ▼               ▼              ▼                 │
  │                    ┌──────────────────────────┐                          │
  │                    │       Reflector (反思)     │                         │
  │                    │  CONTINUE|RETRY|REPLAN     │                         │
  │                    │  |STOP|ASK_USER            │                         │
  │                    └──────────────────────────┘                          │
  └──────────────────────────────────────────────────────────────────────────┘
```

**数据流**: User Input → Planner.plan() → TaskPlan → [Loop: Executor → Reflector] → Synthesize → Answer

## 四层架构

### Layer 1 — Planner (规划层)

**位置**: `agent/planner/`

| 模块 | 职责 |
|------|------|
| `planner.py` | LLM 驱动的任务分解。检索上下文 → 推荐工具 → 构建 Prompt → 调用 LLM → 构建 TaskPlan |
| `task_plan.py` | 数据模型：`TaskPlan` / `Step` / `StepStatus` |
| `reflector.py` | 3 层反思：快速规则 (确定性) → LLM 深度分析 → 确定性回退 |

**规划流程**:
```
User Input
  → Memory.retrieve_for_planning()    # 检索相关记忆
  → Router.recommend_for_step()       # 关键词推荐工具
  → LLM.chat(tools=[emit_task_plan])  # 结构化输出 TaskPlan
  → TaskPlan (goal + steps + context)
```

**Step 数据结构**:
```
Step
  ├── step_id: int               # 步骤编号
  ├── description: str           # 步骤描述
  ├── tool: Optional[str]        # 推荐工具
  ├── tool_input: dict           # 工具参数
  ├── depends_on: List[int]      # 依赖步骤 ID
  ├── is_parallel: bool          # 是否可并行
  ├── success_criteria: str      # 成功标准
  ├── fallback: str              # 备选方案
  ├── status: StepStatus         # PENDING|IN_PROGRESS|SUCCESS|FAILED|REJECTED|TIMEOUT|SKIPPED|RETRYING
  ├── result: Optional[dict]     # 执行结果
  ├── error: Optional[str]       # 错误信息
  └── retry_count: int           # 已重试次数
```

**Replan 机制**: 当某步失败时，Planner 保留已完成步骤，仅对失败步骤生成替代方案。回退方案：跳过失败步骤，继续剩余步骤。

---

### Layer 2 — Executor (执行层)

**位置**: `agent/executor/`

| 模块 | 职责 |
|------|------|
| `executor.py` | 逐步执行 TaskPlan。创建 StepContext → LLM 工具选择 → 执行 → 重试循环 |
| `retry.py` | 重试策略：`SimpleRetry` / `ExponentialBackoff` / `NoRetry` |
| `step_context.py` | 单步隔离执行上下文 (StepContext)，不保留跨步可变状态 |

**执行流程**:
```
Executor.execute_step(step, plan, previous_results)
  │
  ├── 1. 设置 step.status = IN_PROGRESS
  ├── 2. 创建 StepContext (独立上下文)
  │
  ├── 3. [重试循环] for attempt in 1..max_retries:
  │     │
  │     ├── 智能路由:
  │     │   · 步骤有推荐工具 + 不需 LLM 决策 → 直接 dispatch
  │     │   · 否则 → LLM 选择合适的工具并执行
  │     │
  │     ├── 成功 → step.status = SUCCESS → 返回
  │     └── 失败 → 判断是否继续重试
  │           · REJECTED (安全检查) → 立即停止
  │           · 其他错误 → ExponentialBackoff 等待后重试
  │
  └── 4. 所有重试耗尽 → step.status = FAILED
```

**LLM 决策绕过**: 如果步骤已明确指定工具且描述不含"分析/判断/决定"等关键词，跳过 LLM，直接调用工具。减少不必要的 API 消耗。

---

### Layer 3 — Tool Router (工具路由层)

**位置**: `agent/tools/`

| 模块 | 职责 |
|------|------|
| `schema.py` | 核心协议：`ToolProtocol` / `ToolCall` / `ToolResult` / `ToolResultStatus` / `ToolSchema` |
| `registry.py` | 工具注册表 (单例模式)，管理所有工具的生命周期 |
| `router.py` | 统一路由：validate → dispatch → execute → record |
| `skill.py` | 组合工具抽象：`decompose → dispatch → synthesize` |
| `adapters/openai_adapter.py` | OpenAI Function Calling 格式转换 |

**14 个内置工具**:

| 类别 | 工具 | 读写 | 说明 |
|------|------|------|------|
| 文件 | `read_file` | r | 读取文件内容 |
| | `write_file` | w | 写入/创建文件 |
| | `list_dir` | r | 列出目录内容 |
| 代码 | `run_code` | w | 执行 Python 代码 |
| | `lint_code` | r | 静态检查代码 |
| 网络 | `web_search` | r | 搜索网络 |
| | `web_fetch` | r | 抓取网页内容 |
| 系统 | `get_time` | r | 获取当前时间 |
| | `calculator` | r | 数学计算 |
| 记忆 | `save_note` | w | 保存笔记 |
| | `list_notes` | r | 列出笔记 |
| | `remember_fact` | w | 存储事实 |
| | `search_memory` | r | 搜索记忆 |
| | `summarize_context` | r | 摘要上下文 |

**工具路由流程**:
```
ToolRouter.dispatch(call)
  ├── 1. 分配 call_id
  ├── 2. Registry.validate_call(tool) → 工具是否存在
  ├── 3. Tool.validate(call) → 前置验证 (参数校验、安全检查)
  ├── 4. Tool.execute(call) → 执行
  └── 5. 记录 ToolResult + duration_ms → 返回
```

**Skill (组合工具)**: 
```
Skill.execute(call)
  → decompose(task, call) → List[ToolCall]
  → [for each sub_call: dispatcher(sub_call)]
  → synthesize(results, call) → ToolResult
```
Skill 对 Router 是透明的 — Router 不知道内部是原子还是组合。调度器由 Orchestrator 在初始化时注入，避免循环依赖。

---

### Layer 4 — Memory (记忆层)

**位置**: `agent/memory/`

| 模块 | 职责 |
|------|------|
| `memory_manager.py` | 统一记忆 API，组合短期 + 长期 + 向量 + 检索 |
| `short_term.py` | 滑动窗口对话历史 (最多 200 条消息) |
| `long_term.py` | 持久化事实/知识/执行日志 (JSON 存储) |
| `retriever.py` | 语义搜索 (向量) → 关键词回退 |
| `vector_store.py` | ChromaDB 向量存储包装器 (chromadb 不可用时降级) |
| `compressor.py` | 渐进式摘要压缩 |
| `token_counter.py` | Token 计数 (估算) |

**压缩策略**:
- 触发阈值: 4000 tokens
- 压缩率: 40% (保留最近消息，旧消息压缩为摘要)
- 最少消息数: 20 条 (消息太少时不压缩)
- 压缩方式: LLM 生成对话摘要，替换旧消息

**检索策略** (三层回退):
1. 向量搜索 (ChromaDB 语义匹配，top-15，阈值 0.3)
2. 关键词匹配 (无向量结果时回退)
3. 最近消息 (无匹配时返回最近对话)

---

## 执行流程

### 同步模式 (`Orchestrator.run()`)

```
run(user_input)
  │
  ├── Phase 1: PLAN
  │   memory.add_message("user", input)
  │   → planner.plan(user_input) → TaskPlan
  │
  ├── Phase 2: EXECUTE (循环)
  │   while not plan.is_complete():
  │     ready = plan.get_ready_steps()     # 获取就绪步骤 (依赖已满足)
  │     for step in ready:
  │       step, result = executor.execute_step(step, plan, previous_results)
  │       decision = reflector.reflect(step, result, plan)
  │       │
  │       ├── CONTINUE → 下一步
  │       ├── RETRY    → step.status = PENDING (重新进入就绪队列)
  │       ├── REPLAN   → planner.replan() → 新的 TaskPlan
  │       ├── STOP     → 跳出循环
  │       └── ASK_USER → 返回询问信息
  │
  └── Phase 3: SYNTHESIZE
      _synthesize(plan)
        │
        ├── 单步成功 → 智能提取结果字段 (response/content/answer/summary/time/result)
        ├── 多步骤   → LLM 合成自然语言答案
        └── 回退     → 拼接步骤结果文本
```

### 流式模式 (`Orchestrator.run_stream()`)

```
run_stream(user_input) → AsyncGenerator[StreamEvent]
  │
  ├── Phase 1: PLAN
  │   yield StreamEvent.thinking("正在分析任务...")
  │   → planner.plan(user_input)
  │   yield StreamEvent.plan_ready(goal, steps_count, estimated_tools)
  │
  ├── Phase 2: EXECUTE (循环)
  │   while not plan.is_complete():
  │     for step in ready:
  │       yield StreamEvent.step_start(step_id, description, tool)
  │       → executor.execute_step()
  │       yield StreamEvent.tool_result(tool_name, ok, step_id)
  │       yield StreamEvent.step_done(step_id, success)
  │       → reflector.reflect()
  │       yield StreamEvent.reflection(decision, reason)
  │
  └── Phase 3: SYNTHESIZE (流式)
      yield StreamEvent(type=SYNTHESIS)
      → llm.stream(messages) → 逐 token 输出
      yield StreamEvent.text_delta(content)   # 每 token 一个事件
      yield StreamEvent.done(answer, duration_sec)

AgentDisplay (Rich Live) 消费事件流 → 3 面板布局 (header/body/footer)
```

---

## Provider 架构

**位置**: `agent/providers/`

```
ProviderProtocol (抽象接口)
  ├── chat(messages, tools, tool_choice) → LLMResponse
  ├── stream(messages, tools) → AsyncGenerator[LLMChunk]
  └── model_name: str

OpenAIProvider (单一实现)
  └── 通过不同 base_url 适配所有 OpenAI 兼容 API

create_provider(api_key, model_name, ...)  ← 工厂函数
  │
  ├── "deepseek-*"           → https://api.deepseek.com
  ├── "gpt-*" / "o1-*"~"o9-*" → https://api.openai.com/v1
  ├── "glm-*" / "zhipu-*"    → https://open.bigmodel.cn/api/paas/v4
  ├── "claude-*" (兼容代理)   → https://api.openai.com/v1
  └── unknown + base_url     → 自定义端点
```

**设计原则**: 模型名本身就是 Provider 信息。`deepseek-v4-pro` 必然在 `api.deepseek.com`，`gpt-4` 必然在 `api.openai.com`。不要求用户额外配置 provider 字段。

**支持的最新模型** (2026 年 7 月):

| 模型系列 | 模型 | Provider |
|----------|------|----------|
| DeepSeek V4 | `deepseek-v4-pro` (1.6T, 49B 激活), `deepseek-v4-flash` (284B, 13B 激活) | DeepSeek |
| DeepSeek | `deepseek-v4-pro` (推荐), `deepseek-v4-flash` | DeepSeek |
| GPT-5.6 | `gpt-5.6-sol` (旗舰), `gpt-5.6-terra` (均衡), `gpt-5.6-luna` (轻量) | OpenAI |
| GPT 旧版 | `gpt-5.5`, `gpt-4`, `gpt-4o`, `gpt-3.5-turbo` | OpenAI |
| o 系列 | `o1`, `o3`, `o4`, `o5`, `o6`, `o7`, `o8`, `o9` | OpenAI |
| GLM | `glm-5.2`, `glm-4-plus`, `glm-4` | 智谱 |
| Claude (代理) | `claude-opus-4-8`, `claude-sonnet-5` 等 | 兼容端点 |

---

## 配置系统

**位置**: `agent/settings.py` + `config.py`

```
AgentSettings (dataclass — 可注入, 可覆盖)
  ├── Paths
  │   ├── base_dir, memory_dir
  │   ├── short_term_file, long_term_file, notes_file
  │   └── api_key_file
  ├── LLM
  │   ├── model_name (默认: AGENT_MODEL 环境变量 | "deepseek-v4-pro")
  │   ├── provider_name ("auto" | "openai" | "deepseek")
  │   ├── api_key (DEEPSEEK_API_KEY > OPENAI_API_KEY > ZHIPU_API_KEY > api_key.txt)
  │   ├── base_url, temperature, max_tokens
  ├── Agent Limits
  │   ├── max_plan_steps: 20
  │   ├── max_retries: 3
  │   ├── step_timeout_sec: 60
  │   ├── max_history_items: 200
  │   └── max_long_term_items: 200
  ├── Reflection
  │   ├── reflection_enabled: true
  │   ├── reflection_use_llm: true
  │   └── reflection_depth: 1
  ├── Context Compression
  │   ├── compression_token_threshold: 4000
  │   ├── compression_ratio: 0.4
  │   └── compression_min_messages: 20
  └── Vector Store
      ├── vector_search_top_k: 15
      └── vector_similarity_threshold: 0.3
```

所有配置字段都有合理默认值。测试时注入不同 settings：

```python
settings = AgentSettings(max_retries=1, reflection_enabled=False)
planner = Planner(llm, memory, settings=settings)
```

---

## 流式显示架构

**位置**: `agent/display.py`

```
AgentDisplay (上下文管理器 — Rich Live)
  │
  └── render(StreamEvent)
      │
      ├── StreamEventType.THINKING     → "🤔 思考中..."
      ├── StreamEventType.PLAN_READY   → "📋 计划已生成"
      ├── StreamEventType.STEP_START   → "▶ Step N: 描述..."
      ├── StreamEventType.TOOL_RESULT  → "  ✓ / ✗ 工具结果"
      ├── StreamEventType.STEP_DONE    → "  ✅ / ❌ 步骤完成"
      ├── StreamEventType.REFLECTION   → "🔄 反思: 决策..."
      ├── StreamEventType.SYNTHESIS    → "💬 生成答案..."
      ├── StreamEventType.TEXT_DELTA   → 追加文本 (逐 token)
      ├── StreamEventType.DONE         → "✅ 完成 (Xs)"
      └── StreamEventType.ERROR        → "❌ 错误"
```

3 面板布局：
- **Header**: 迭代数、步骤数、耗时
- **Body**: 当前阶段 + 生成中的文本
- **Footer**: 状态栏

---

## 项目结构

```
NPCSidekick/
├── main.py                       # CLI 入口 (启动动画 + 交互/流式模式)
├── config.py                     # 全局配置 (兼容旧代码的属性访问)
├── agent/
│   ├── orchestrator.py           # ⭐ 主编排器 (run / run_stream / run_chat)
│   ├── display.py                # Rich Live 流式显示 (3 面板布局)
│   ├── settings.py               # AgentSettings (可注入 dataclass)
│   ├── logging_config.py         # structlog 配置
│   ├── planner/
│   │   ├── planner.py            # 任务规划 (LLM 驱动, function calling)
│   │   ├── task_plan.py          # TaskPlan / Step / StepStatus 数据模型
│   │   └── reflector.py          # 反思器 (3 层: 快速规则 → LLM → 回退)
│   ├── executor/
│   │   ├── executor.py           # 步骤执行器 (LLM 工具选择 + 重试循环)
│   │   ├── retry.py              # 重试策略 (SimpleRetry / ExponentialBackoff / NoRetry)
│   │   └── step_context.py       # StepContext (单步隔离上下文)
│   ├── tools/
│   │   ├── schema.py             # ToolProtocol / ToolCall / ToolResult / ToolSchema
│   │   ├── registry.py           # ToolRegistry (单例, 生命周期管理)
│   │   ├── router.py             # ToolRouter (validate → dispatch → record)
│   │   ├── skill.py              # Skill 抽象 + AnalyzeCodeSkill
│   │   ├── builtin/
│   │   │   ├── file_tools.py     # ReadFile / WriteFile / ListDir (3)
│   │   │   ├── code_tools.py     # RunCode / LintCode (2)
│   │   │   ├── web_tools.py      # WebSearch / WebFetch (2)
│   │   │   ├── system_tools.py   # GetTime / Calculator (2)
│   │   │   └── memory_tools.py   # SaveNote / ListNotes / RememberFact / SearchMemory / SummarizeContext (5)
│   │   └── adapters/
│   │       └── openai_adapter.py # OpenAI Function Calling 格式转换
│   ├── memory/
│   │   ├── memory_manager.py     # 统一记忆 API (组合所有子模块)
│   │   ├── short_term.py         # 滑动窗口对话历史
│   │   ├── long_term.py          # 持久化事实/知识/日志
│   │   ├── retriever.py          # 语义搜索 → 关键词回退
│   │   ├── vector_store.py       # ChromaDB 包装器 (可选依赖)
│   │   ├── compressor.py         # 渐进式摘要压缩
│   │   └── token_counter.py      # Token 计数估算
│   ├── providers/
│   │   ├── base.py               # ProviderProtocol 抽象
│   │   ├── openai_provider.py    # OpenAI 兼容实现 (httpx)
│   │   └── factory.py            # create_provider() 自动检测
│   └── llm/
│       ├── client.py             # LLMClient (薄包装)
│       └── types.py              # StreamEvent / LLMResponse / LLMChunk
└── tests/                        # 982 个测试 (pytest，全仓；2026-09-17)
    ├── test_planner.py
    ├── test_executor.py
    ├── test_router.py
    ├── test_orchestrator.py
    ├── test_orchestrator_stream.py
    ├── test_providers.py
    ├── test_memory_manager.py
    ├── test_memory_tools.py
    ├── test_code_tools.py
    ├── test_schema.py
    ├── test_skill.py
    ├── test_task_plan.py
    ├── test_llm_client.py
    ├── test_reflector.py
    └── conftest.py
```

---

## 关键设计决策

### 1. 单一 Provider 实现，多个 base_url

不创建 `DeepSeekProvider`、`OpenAIProvider`、`ZhipuProvider` 等子类。所有主流 LLM 提供商都兼容 OpenAI API 格式，只需不同的 `base_url`。`create_provider()` 工厂函数根据模型名自动推断。

### 2. Skill 的依赖注入

Skill 不直接 import ToolRouter (避免循环依赖)。改为 `set_dispatcher()` 注入：
```python
skill.set_dispatcher(router.dispatch)
# Skill.execute() → decompose() → dispatcher(sub_call) → synthesize()
```

### 3. 3 层反射

不直接调 LLM 做反思。先用确定性规则过滤：
1. **快速规则** — REJECTED → REPLAN, TIMEOUT → RETRY
2. **LLM 深度分析** — 复杂情况 (有 success_criteria 的成功 / 失败)
3. **确定性回退** — LLM 不可用时：检查重试次数 → 备选方案 → ASK_USER

### 4. 上下文压缩

不是简单截断 — 用 LLM 生成对话摘要，保留最近消息：
- 触发条件: 消息 > 20 条 且 token > 4000
- 压缩方式: LLM 摘要旧消息，保留最新消息
- 结果: 40% 压缩率

### 5. 内存自动迁移

MemoryManager 首次运行时自动检测：向量存储为空但 JSON 有数据 → 重建向量索引。确保切换环境后记忆不丢失。

---

## 测试

```bash
# 全部 982 个测试（2026-09-17）
pytest tests/ -v                    # ~64s

# 覆盖率
pytest tests/ -v --cov=agent --cov-report=term-missing

# 单模块
pytest tests/test_planner.py -v
pytest tests/test_orchestrator_stream.py -v

# 按关键词
pytest tests/ -k "stream" -v
```

测试覆盖：Planner / Executor / Router / Orchestrator (同步+流式) / Provider / Memory / Tools / Schema / Skill / TaskPlan / Reflector。

---

## 版本历史

| 版本 | 日期 | 主要变化 |
|------|------|----------|
| v3.1 | 2026-07 | 4 层架构稳定、14 工具、流式输出、多 Provider (DeepSeek/OpenAI/智谱)、上下文压缩、向量检索、272 tests（引擎层，历史快照） |

---

*最后更新: 2026-09-10*
