# Dagent — Python AI Agent 框架

## 项目概述

Dagent 是一个 4 层架构的 AI Agent 框架，由 LLM 驱动的自主任务执行系统。

- **项目路径**: `d:\ai\Dagent\`
- **Python 版本**: 3.13+
- **LLM 后端**: DeepSeek API（兼容 OpenAI 格式）

## 架构

```
用户输入 → Orchestrator（主编排器）
              ├── Planner（任务规划）    → TaskPlan
              ├── Executor（逐步执行）   → StepContext
              ├── Reflector（反射校正）  → ReflectionDecision
              └── Memory（记忆管理）     → 短时/长时记忆
```

## 目录结构

```
Dagent/
├── config.py                          # 全局配置（API Key、路径、限制参数）
├── agent/
│   ├── __init__.py                    # 入口：导出 AgentOrchestrator
│   ├── orchestrator.py                # 主编排器 — 连接所有 4 层
│   ├── planner/
│   │   ├── planner.py                 # 任务分解 → 生成 TaskPlan + Steps
│   │   ├── task_plan.py               # TaskPlan / Step / StepStatus 数据结构
│   │   └── reflector.py              # 执行后反思 → continue/retry/replan/stop
│   ├── executor/
│   │   ├── executor.py               # 逐步执行 TaskPlan
│   │   ├── retry.py                  # 重试策略（指数退避）
│   │   └── step_context.py           # 单步执行上下文
│   ├── tools/
│   │   ├── registry.py               # 工具注册表（单例）
│   │   ├── router.py                 # 工具路由器（按名称/类别分发）
│   │   ├── schema.py                 # ToolProtocol / ToolSchema / ToolCall 定义
│   │   ├── adapters/
│   │   │   └── openai_adapter.py     # OpenAI 兼容 function calling 适配器
│   │   └── builtin/
│   │       ├── code_tools.py         # 代码执行工具
│   │       ├── file_tools.py         # 文件操作工具
│   │       ├── memory_tools.py       # 记忆管理工具
│   │       ├── system_tools.py       # 系统操作工具
│   │       └── web_tools.py          # 网络请求工具
│   ├── memory/
│   │   ├── memory_manager.py         # 记忆管理器（统一接口）
│   │   ├── short_term.py             # 短时记忆（最近 N 条，最多 200）
│   │   ├── long_term.py              # 长时记忆（持久化到 JSON）
│   │   ├── retriever.py              # 记忆检索（相关性排序）
│   │   └── store/                    # 持久化存储目录（已 gitignore）
│   └── llm/
│       └── client.py                 # LLM 客户端（DeepSeek/OpenAI 兼容）
```

## 使用方式

```python
from agent import AgentOrchestrator

orch = AgentOrchestrator()
orch.initialize()                      # 注册所有内置工具
answer = orch.run("帮我找出 login 函数的 bug")
print(answer)
```

## 配置

所有配置在 `config.py` 中，支持环境变量覆盖：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `MODEL_NAME` | `deepseek-chat` | 模型名称 |
| `API_KEY` | 环境变量 `DEEPSEEK_API_KEY` | API 密钥 |
| `BASE_URL` | `https://api.deepseek.com` | API 地址 |
| `MAX_PLAN_STEPS` | 20 | 计划最大步骤数 |
| `MAX_RETRIES` | 3 | 单步最大重试 |
| `REFLECTION_ENABLED` | True | 启用反射自校正 |

## 当前状态

- [x] 基础架构搭建完成（4 层全部实现）
- [x] 5 个内置工具（code/file/memory/system/web）
- [x] OpenAI 兼容适配器
- [x] 记忆系统（短时 + 长时）
- [x] 反射/自校正机制
- [ ] 测试用例
- [ ] 实际运行验证
- [ ] 错误处理完善

## 开发约定

- import 使用 `from __future__ import annotations`（延迟注解求值）
- 类型注解完整（mypy 兼容）
- 注释用中文，代码/标识符用英文
- `config.py` 是唯一配置入口，不要在代码中硬编码配置
- 每次功能变更后 git commit
