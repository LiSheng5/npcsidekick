# Claude 级 Agent v2.0 架构设计文档

## 📑 目录
1. [系统概览](#系统概览)
2. [核心组件](#核心组件)
3. [执行流程](#执行流程)
4. [状态管理](#状态管理)
5. [工具系统](#工具系统)
6. [数据持久化](#数据持久化)
7. [扩展机制](#扩展机制)

---

## 系统概览

### 设计目标

✅ **生产级别**：支持复杂多步推理  
✅ **可追踪**：完整的执行日志和决策轨迹  
✅ **可恢复**：工具失败不会导致整体崩溃  
✅ **可扩展**：易于添加新工具和功能  
✅ **可理解**：用户能看到 Agent 的思考过程  

### 设计模式

采用 **Plan-Act-Observe-Reflect (PAOR)** 模式：

```
INPUT
  ↓
┌─ PLAN: 模型分析问题，决策工具
│   └─ 输出：思考过程 + 工具选择
│
├─ ACT: 执行选定的工具
│   └─ 输出：工具调用 + 参数
│
├─ OBSERVE: 收集和分析结果
│   └─ 输出：工具结果 + 状态更新
│
└─ REFLECT: 判断是否继续
    └─ 如果需要继续 → 返回 PLAN
       如果完成 → 返回最终答案

OUTPUT
```

---

## 核心组件

### 1. AgentState (状态容器)

**职责**: 管理整个执行周期的状态

```python
class AgentState:
    # 基本信息
    user_input: str          # 用户输入
    memory: dict             # 共享记忆对象
    step: int                # 当前步数
    
    # 执行轨迹
    thoughts: List[dict]         # 模型的思考过程
    tool_calls: List[dict]       # 工具调用记录
    tool_results: List[dict]     # 工具结果
    decision_log: List[dict]     # 决策理由
    execution_trace: List[dict]  # 完整轨迹
    
    # 状态标志
    final_answer: str    # 最终答案
    is_complete: bool    # 是否完成
```

**关键方法**:
- `add_thought(thought)`: 记录思考
- `add_tool_call(tool_name, arguments, reasoning)`: 记录工具调用和理由
- `add_tool_result(tool_name, result)`: 记录工具结果
- `to_dict()`: 序列化状态

**状态转移**:
```
Initial → PLAN(add_thought) → ACT(add_tool_call)
  ↓                                    ↓
Complete                         OBSERVE(add_tool_result)
                                         ↓
                              REFLECT(is_complete check)
```

### 2. ToolPipeline (工具流水线)

**职责**: 管理工具的注册、验证、执行和记录

```python
class ToolPipeline:
    registry: dict           # 工具函数映射
    execution_history: list  # 执行历史
```

**关键方法**:

#### validate_tool(tool_name) → bool
- 检查工具是否存在于注册表

#### check_prerequisites(tool_name, arguments) → (bool, str)
- 验证工具调用的前置条件
- 示例：calculator 工具检查表达式安全性
- 示例：检查必需参数

#### get_tool_dependencies(tool_name) → List[str]
- 获取工具的依赖列表
- 框架已预留接口，可在扩展时实现

#### execute(tool_name, arguments, memory, state) → result
- 执行工具的完整流程：
  1. 检查前置条件 `check_prerequisites()`
  2. 检查依赖 `get_tool_dependencies()`
  3. 执行工具 `handler(arguments, memory)`
  4. 记录执行历史
  5. 更新状态
  6. 持久化记录

#### get_execution_summary() → dict
- 返回执行统计：总调用数、使用的工具、详细历史

**工具执行流程**:
```
Tool Call Request
    ↓
Validate Tool (exists?)
    ↓
Check Prerequisites (params valid? expressions safe?)
    ↓
Check Dependencies (needed tools executed?)
    ↓
Execute Handler
    ↓
Record in History
    ↓
Update State
    ↓
Persist to Memory
    ↓
Return Result
```

### 3. orchestrate_agent (编排器)

**职责**: 实现 PAOR 循环的主流程

```python
def orchestrate_agent(client, state, pipeline, max_steps):
    """
    执行 Plan-Act-Observe-Reflect 循环
    """
    for step in range(1, max_steps + 1):
        # PLAN: 获取模型决策
        response = client.chat.completions.create(...)
        message = response.choices[0].message
        
        # 记录思考
        state.add_thought(message.content)
        
        # 检查是否有工具调用
        if not tool_calls:
            return message.content  # 直接返回答案
        
        # ACT: 执行工具
        for tool_call in tool_calls:
            # 记录决策理由
            state.add_tool_call(tool_name, arguments, reasoning)
            
            # OBSERVE: 执行并记录结果
            result = pipeline.execute(...)
            state.add_tool_result(tool_name, result)
            messages.append({"role": "tool", "content": result})
        
        # REFLECT: 继续循环或完成
    
    return final_answer
```

**循环控制**:
- 每次迭代都可能执行 0 个或多个工具
- 工具结果重新进入消息历史
- 模型在下次迭代中基于结果继续推理
- 达到 MAX_STEPS 时停止

### 4. chat_with_agent (会话管理)

**职责**: 封装编排器，提供统一接口

```python
def chat_with_agent(client, memory, user_input):
    # 创建状态容器
    state = AgentState(user_input, memory)
    
    # 创建工具流水线
    pipeline = ToolPipeline(TOOL_FUNCTIONS)
    
    # 执行编排
    answer = orchestrate_agent(client, state, pipeline, MAX_STEPS)
    
    # 记录执行日志
    memory["execution_log"].append(state.to_dict())
    
    return answer
```

**接口**:
- 输入：`client` (OpenAI 客户端), `memory` (共享记忆), `user_input` (用户输入)
- 输出：`str` (最终答案)
- 副作用：更新 `memory["execution_log"]`

---

## 执行流程

### 完整时序图

```
User                REPL              Agent             Pipeline         Memory
  │                  │                  │                  │               │
  ├─ 输入 query ────→│                  │                  │               │
  │                  │                  │                  │               │
  │                  ├─ chat_with_agent ─→ 创建 State      │               │
  │                  │                  │                  │               │
  │                  │                  ├─ orchestrate_agent ─→           │
  │                  │                  │                  │               │
  │                  │                  ├─ Step 1: PLAN    │               │
  │                  │                  │ (LLM 分析问题)   │               │
  │                  │                  ├─ 记录 thoughts   │               │
  │                  │                  │                  │               │
  │                  │                  ├─ Step 2: ACT     │               │
  │                  │                  ├─ 解析 tool_calls │               │
  │                  │                  ├─ 记录理由         │               │
  │                  │                  │                  │               │
  │                  │                  │ ├─ execute ─────→│               │
  │                  │                  │ │ (验证、执行、记录) │
  │                  │                  │ ├─ 收集结果 ←────│               │
  │                  │                  │                  │               │
  │                  │                  ├─ Step 3: OBSERVE │               │
  │                  │                  ├─ 记录 results    │               │
  │                  │                  │                  │               │
  │                  │                  ├─ Step 4: REFLECT │
  │                  │                  ├─ (继续或结束)     │
  │                  │                  │                  │
  │                  │                  ├─ 保存执行日志 ──→│
  │                  │                  │                  │               ├─ 持久化
  │                  │                  │←─ 返回答案 ──────│               │
  │                  │←─ 返回答案 ──────│                  │               │
  │←─ 显示答案 ──────│                  │                  │               │
  │                  │                  │                  │               │
  └─ 记录历史 ──────→│                  │                  │               │
                     │                  │                  │               │
```

### 特殊场景处理

#### 场景 1: 无需工具调用
```
User Input
  ↓
LLM 分析：不需要工具
  ↓
直接返回答案
```

#### 场景 2: 多个工具调用
```
User Input
  ↓
LLM 分析：需要多个工具
  ↓
同时执行所有工具 (或按依赖顺序)
  ↓
收集所有结果
  ↓
LLM 综合结果
  ↓
返回答案
```

#### 场景 3: 工具执行失败
```
User Input
  ↓
LLM 选择工具
  ↓
工具执行失败
  ↓
返回错误信息给 LLM
  ↓
LLM 可选择替代方案或返回错误
  ↓
返回答案
```

#### 场景 4: 达到最大步数
```
User Input
  ↓
Step 1: 执行
  ↓
Step 2: 执行
  ↓
Step 3: 执行
  ↓
达到 MAX_STEPS = 3
  ↓
返回 "已达到最大步数" 消息
```

---

## 状态管理

### 状态生命周期

```
创建
  ├─ AgentState(user_input, memory)
  │
进行中
  ├─ add_thought()      → 步骤 1+
  ├─ add_tool_call()    → 步骤 1+
  ├─ add_tool_result()  → 步骤 1+
  ├─ add_alternative_tools() → 可选
  │
完成
  ├─ state.is_complete = True
  ├─ state.final_answer = answer
  ├─ state.to_dict() → 序列化
  │
持久化
  └─ memory["execution_log"].append(state.to_dict())
```

### 执行追踪

执行追踪记录了所有事件的完整序列：

```python
execution_trace = [
    {
        "type": "thought",
        "step": 1,
        "content": "用户要求计算表达式，需要使用 calculator..."
    },
    {
        "type": "tool_call",
        "step": 1,
        "tool": "calculator",
        "arguments": {"expression": "2+2*3"},
        "reasoning": "直接调用计算器工具..."
    },
    {
        "type": "tool_result",
        "step": 1,
        "tool": "calculator",
        "result": 8
    },
    ...
]
```

### 决策日志

决策日志记录了每个决策的理由：

```python
decision_log = [
    {
        "step": 1,
        "decision": "选择工具: calculator",
        "reasoning": "用户明确要求计算表达式，calculator 是最直接的选择"
    },
    {
        "step": 2,
        "decision": "不继续执行",
        "reasoning": "已得到计算结果 8，无需进一步处理"
    }
]
```

---

## 工具系统

### 工具注册表结构

```python
TOOL_FUNCTIONS = {
    "get_current_time": tool_get_current_time,
    "calculator": tool_calculator,
    "save_note": tool_save_note,
    "list_notes": tool_list_notes,
    "remember_fact": tool_remember_fact,
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "执行数学计算",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式 (e.g., '2+2*3')"
                    }
                },
                "required": ["expression"]
            }
        }
    },
    # ... 更多工具
]
```

### 工具处理函数签名

```python
def tool_name(arguments: dict, memory: dict) -> Any:
    """
    Args:
        arguments: 解析后的工具参数
        memory: 共享记忆对象
    
    Returns:
        工具执行结果（应该是可序列化的）
    
    Raises:
        Exception: 任何执行错误
    """
    pass
```

### 工具执行安全检查

#### Calculator 工具的表达式检查
```python
def check_prerequisites(self, tool_name, arguments):
    if tool_name == "calculator":
        expr = arguments.get("expression", "")
        # 禁止危险操作
        forbidden = ["import", "exec", "eval", "__"]
        if any(kw in expr for kw in forbidden):
            return False, "不允许的表达式"
    return True, "验证通过"
```

### 工具依赖解析（框架预留）

```python
def get_tool_dependencies(self, tool_name):
    """
    示例：分析工具依赖
    """
    dependencies = {
        "analyze_result": ["calculator", "remember_fact"],
        "generate_report": ["remember_fact"],
    }
    return dependencies.get(tool_name, [])
```

---

## 数据持久化

### 内存结构 (memory.json)

```json
{
    "facts": [
        "用户名叫小明",
        "用户喜欢 Python",
        "..."
    ],
    "history": [
        {
            "role": "user",
            "content": "现在几点?"
        },
        {
            "role": "assistant",
            "content": "当前时间是..."
        },
        {
            "role": "tool",
            "content": "{\"status\": \"success\", ...}"
        },
        "..."
    ],
    "summary": "缓存的记忆摘要",
    "execution_log": [
        {
            "input": "计算 2+2",
            "step": 1,
            "thoughts": [...],
            "tool_calls": [...],
            "tool_results": [...],
            "decision_log": [...],
            "execution_trace": [...],
            "final_answer": "结果是 4"
        },
        "..."
    ]
}
```

### 持久化策略

**自动保存时机**:
- 每次会话后自动保存
- 内存大小超过阈值时触发压缩

**内存剪裁**:
```python
MAX_HISTORY = 100        # 保留最后 100 条历史
MAX_MEMORY_FACTS = 50    # 保留最后 50 条事实
MAX_EXECUTION_LOG = 100  # 保留最后 100 条执行记录
```

**摘要生成**:
- 自动为旧历史生成摘要
- 摘要保存到 `memory["summary"]`
- 减少模型上下文占用

---

## 扩展机制

### 1. 添加新工具

**步骤 1**: 实现工具函数
```python
def tool_my_new_tool(arguments, memory):
    """我的新工具"""
    param = arguments.get("param")
    # ... 工具逻辑
    return result
```

**步骤 2**: 添加到注册表
```python
TOOL_FUNCTIONS["my_new_tool"] = tool_my_new_tool
```

**步骤 3**: 添加 OpenAI 工具定义
```python
TOOLS.append({
    "type": "function",
    "function": {
        "name": "my_new_tool",
        "description": "描述",
        "parameters": {...}
    }
})
```

### 2. 自定义验证规则

```python
class CustomToolPipeline(ToolPipeline):
    def check_prerequisites(self, tool_name, arguments):
        # 调用父类检查
        valid, msg = super().check_prerequisites(tool_name, arguments)
        if not valid:
            return valid, msg
        
        # 自定义检查
        if tool_name == "my_new_tool":
            # ... 自定义逻辑
            pass
        
        return True, "验证通过"
```

### 3. 自定义 Prompt

在 `SYSTEM_PROMPT` 中定义工作流程和规则

### 4. 状态监听（Observer Pattern）

可以实现观察者来追踪状态变化：

```python
class StateObserver:
    def on_thought(self, state, thought): pass
    def on_tool_call(self, state, tool_call): pass
    def on_tool_result(self, state, result): pass
    def on_complete(self, state): pass
```

### 5. 工具链编排

未来可以实现自动工具链规划：

```python
class ToolChainPlanner:
    def plan_chain(self, user_input, available_tools):
        """规划工具执行序列"""
        # 返回：[(工具1, 参数), (工具2, 参数), ...]
        pass
```

---

## 性能特性

| 指标 | 值 | 说明 |
|------|-----|------|
| Max Steps | 3 | 最大推理步数 |
| Max History | 100 | 保留的历史条数 |
| Max Memory Facts | 50 | 保留的事实条数 |
| Temperature | 0.2 | 模型温度（工具调用） |
| Max Tokens | 2000 | 单次响应最大 token |

---

## 错误处理

### 错误分类

| 类型 | 处理 | 恢复 |
|------|------|------|
| 工具不存在 | 返回错误给 LLM | LLM 尝试替代方案 |
| 前置条件失败 | 返回错误给 LLM | LLM 修改参数重试 |
| 工具执行异常 | 捕获异常，返回错误 | LLM 考虑替代方案 |
| API 调用失败 | 抛出异常中断 | 用户重试 |

### 错误传播

```
Tool Exception
    ↓
Catch in pipeline.execute()
    ↓
Format as tool result: {"status": "error", "error": message}
    ↓
Send to LLM
    ↓
LLM Decides: Retry? Alternative? Give Up?
    ↓
State Reflects Decision
```

---

## 总结

Claude 级 Agent v2.0 通过以下设计达到生产级别：

✅ **模块化**: AgentState, ToolPipeline, orchestrator 各司其职  
✅ **可追踪**: 完整的执行日志、决策理由、状态轨迹  
✅ **易扩展**: 工具注册、自定义验证、Prompt 定制  
✅ **强健性**: 错误隔离、前置条件检查、优雅降级  
✅ **可观测**: 执行状态展示、统计信息、决策日志  

后续可以基于这个框架继续增强功能，如工具链规划、并行执行、成本追踪等。
