# Dagent v3.0 — 架构设计文档

## 系统概览

Dagent 是一个 4 层 AI Agent 框架，由 LLM 驱动的自主任务执行系统。

```
User Input → Planner.plan() → TaskPlan
           → [Loop]
             → Executor.execute_step() → (step, result)
             → Reflector.reflect() → CONTINUE | RETRY | REPLAN | STOP | ASK_USER
             → Memory.log_execution()
           → _synthesize() → answer

LLM 调用统一委托给 LLMClient → ProviderProtocol（OpenAI/DeepSeek/兼容 API）
```

## 核心组件

| 层 | 模块 | 职责 |
|----|------|------|
| **Planner** | `agent/planner/planner.py` | LLM 驱动的任务分解 → TaskPlan + Steps |
| | `agent/planner/task_plan.py` | 数据模型：TaskPlan / Step / StepStatus |
| | `agent/planner/reflector.py` | 3 层反射：快速规则 → LLM 深度分析 → 确定性回退 |
| **Executor** | `agent/executor/executor.py` | 逐步执行：LLM 工具选择 + 直接分发 + 重试循环 |
| | `agent/executor/retry.py` | 重试策略：SimpleRetry / ExponentialBackoff / NoRetry |
| | `agent/executor/step_context.py` | 单步隔离执行上下文 |
| **Tool Router** | `agent/tools/registry.py` | 工具注册表（单例） |
| | `agent/tools/router.py` | 工具路由：validate → execute → record |
| | `agent/tools/schema.py` | 核心协议：ToolProtocol / ToolCall / ToolResult / ToolSchema |
| | `agent/tools/skill.py` | 组合工具模式：decompose → dispatch → synthesize |
| **Memory** | `agent/memory/memory_manager.py` | 统一记忆 API |
| | `agent/memory/short_term.py` | 滑动窗口对话历史 |
| | `agent/memory/long_term.py` | 持久化事实/知识/执行日志 |
| | `agent/memory/retriever.py` | 语义搜索 → 关键词回退 |
| | `agent/memory/compressor.py` | 渐进式摘要压缩 |
| | `agent/memory/vector_store.py` | ChromaDB 向量存储包装器 |
| **Provider** | `agent/providers/base.py` | ProviderProtocol 抽象接口 |
| | `agent/providers/openai_provider.py` | OpenAI/DeepSeek 实现 |
| | `agent/providers/factory.py` | `create_provider()` 自动检测 |

## 执行流程

### 同步模式 (`run()`)
```
run(user_input)
  ├── Phase 1: Planner.plan() → TaskPlan
  ├── Phase 2: [Loop over ready steps]
  │     ├── Executor.execute_step() → (step, result)
  │     ├── Reflector.reflect() → CONTINUE | RETRY | REPLAN | STOP | ASK_USER
  │     └── Memory.log_execution()
  └── Phase 3: _synthesize() → answer (string)
```

### 流式模式 (`run_stream()`)
```
run_stream(user_input) → AsyncGenerator[StreamEvent]
  ├── Phase 1: StreamEvent.thinking() → StreamEvent.plan_ready()
  ├── Phase 2: StreamEvent.step_start() → StreamEvent.tool_result()
  │            → StreamEvent.step_done() → StreamEvent.reflection()
  └── Phase 3: StreamEvent(type=SYNTHESIS) → StreamEvent.done()

AgentDisplay (Rich Live) 消费事件流 → 3 面板布局 (header/body/footer)
```

## 数据模型

```
TaskPlan
  ├── task_id: str
  ├── original_query: str
  ├── goal: str
  ├── steps: List[Step]
  └── metadata: dict

Step
  ├── step_id: int
  ├── description: str
  ├── tool: Optional[str]
  ├── tool_args: dict
  ├── status: StepStatus (PENDING | IN_PROGRESS | SUCCESS | FAILED | REJECTED | TIMEOUT | SKIPPED)
  ├── result: Optional[dict]
  ├── error: Optional[str]
  └── retry_count: int
```

## Provider 架构

```
LLMClient (薄包装)
  → ProviderProtocol (抽象: chat + stream + model_name)
    → OpenAIProvider (单一实现，不同 base_url 对应不同提供商)

create_provider(api_key, model_name, ...)  ← 工厂
  "deepseek-*" → https://api.deepseek.com
  "gpt-*"/"o1-*"/"o3-*"/"o4-*" → https://api.openai.com/v1
  unknown + custom base_url → 该 URL
```

## 当前状态

- **版本**: v3.0
- **测试**: 148 个单元测试
- **Phase 1-3 已完成**: settings、structlog、streaming、Rich CLI、Skill、multi-provider
