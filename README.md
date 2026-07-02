<p align="center">
  <img src="https://img.shields.io/badge/version-1.0-9b59b6?style=flat-square" alt="version">
  <img src="https://img.shields.io/badge/python-3.10+-purple?style=flat-square" alt="python">
  <img src="https://img.shields.io/badge/tests-272%20passed-brightgreen?style=flat-square" alt="tests">
  <img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="license">
  <img src="https://img.shields.io/badge/lines-5%2C011-purple?style=flat-square" alt="lines">
</p>

<h1 align="center"> 奇天 v1.0</h1>

<p align="center"><b>可上九天揽月，可下五洋捉鳖</b></p>

<p align="center">一个智能助手，愿为你效劳。</p>

<p align="center">
  读写文件 · 运行代码 · 搜索网络 · 系统工具 · 持久记忆 · 流式输出
</p>

---

##  什么是奇天？

**奇天** 是一个 4 层 AI Agent 框架，从零构建，不依赖 LangChain、AutoGPT 等第三方 Agent 库。用最少的抽象，做最多的事。

核心理念：**规划 → 执行 → 路由 → 记忆**，每层独立且可替换。

```
用户输入
  │
  ▼
┌──────────────────────────────────────┐
│  Layer 1: Planner                    │
│  理解意图 → 拆解任务 → 生成执行计划    │
│  · 支持依赖关系 (A 完成后再 B)        │
│  · 反思机制 (执行后自我纠正)           │
└──────────────┬───────────────────────┘
               │
               ▼
┌──────────────────────────────────────┐
│  Layer 2: Executor                   │
│  按计划逐步执行，管理重试和超时          │
│  · 最多 3 次重试                      │
│  · 每步 60s 超时                      │
│  · 支持 Skill (组合工具)               │
└──────────────┬───────────────────────┘
               │
               ▼
┌──────────────────────────────────────┐
│  Layer 3: Tool Router                │
│  14 个原子工具，按类别路由              │
│  · 文件 (读写/编辑/搜索)               │
│  · 代码 (执行/检查/格式化)             │
│  · 网络 (搜索/抓取)                    │
│  · 系统 (命令/进程/时间)               │
│  · 记忆 (存储/检索/笔记)               │
└──────────────┬───────────────────────┘
               │
               ▼
┌──────────────────────────────────────┐
│  Layer 4: Memory                     │
│  短期记忆 + 长期记忆 + 向量检索         │
│  · 自动压缩 (超 4000 tokens)           │
│  · Chroma 向量搜索                     │
│  · 会话持久化                          │
└──────────────┬───────────────────────┘
               │
               ▼
            回答
```

## 特性

| 特性 | 说明 |
|------|------|
|  **14 个工具** | 文件、代码、网络、系统、记忆 — 覆盖日常所有操作 |
|  **任务计划** | 自动拆解复杂任务，支持步骤依赖和反思纠正 |
|  **流式输出** | Rich Live 实时显示：计划 → 工具调用 → 结果 → 回答 |
|  **多层记忆** | 短期 (对话) + 长期 (知识) + Chroma 向量搜索 |
|  **自动路由** | 模型名自动识别 Provider (DeepSeek / OpenAI / 智谱) |
|  **安全确认** | 写操作需要用户确认，读操作直接执行 |
|  **重试容错** | 失败自动重试 3 次，不中断整体流程 |
|  **零依赖** | 不依赖 LangChain、AutoGPT 等框架，只用 httpx + Rich |

## 支持模型

奇天自动识别模型名并路由到对应 API 端点：

| 模型 | Provider | 说明 |
|------|----------|------|
| `deepseek-v4-pro` | DeepSeek | V4 Pro (1.6T, 49B 激活) |
| `deepseek-v4-flash` | DeepSeek | V4 Flash (284B, 13B 激活) |
| `deepseek-chat` | DeepSeek | DeepSeek Chat |
| `gpt-5.6-sol` | OpenAI | GPT-5.6 Sol (旗舰) |
| `gpt-5.6-terra` | OpenAI | GPT-5.6 Terra (均衡) |
| `gpt-5.6-luna` | OpenAI | GPT-5.6 Luna (轻量) |
| `glm-5.2` | 智谱 GLM | GLM-5.2 (开源) |
| `gpt-5.5` / `gpt-4` | OpenAI | 旧版 GPT 系列 |

所有 Provider 使用 OpenAI 兼容 API，无需额外适配。

## 快速开始

### 1. 安装

```bash
git clone https://github.com/your/奇天.git
cd 奇天
pip install -r requirements.txt -r requirements-dev.txt
```

### 2. 配置 API Key

```bash
# 方式一：环境变量
set DEEPSEEK_API_KEY=sk-your-key-here

# 方式二：项目根目录创建 api_key.txt，写入密钥
```

### 3. 运行

```bash
# 交互模式
python main.py

# 流式模式 (推荐 — 实时看到 Agent 工作过程)
python main.py --stream

# 单次查询
python main.py "帮我分析 D:\project\main.py"

# 指定模型
python main.py --model glm-5.2 --stream
```

### 4. 交互命令

```
plan / 查看计划     — 查看当前任务计划
memory / 查看记忆   — 查看记忆摘要
tools / 查看工具    — 列出所有可用工具
history / 查看历史  — 查看执行记录
stream              — 切换到流式模式
standard            — 切换回标准模式
```

##  项目结构

```
奇天/
├── main.py                    # CLI 入口 (含启动动画)
├── config.py                  # 全局配置
├── agent/
│   ├── orchestrator.py        # 编排器 (核心调度)
│   ├── display.py             # Rich Live 流式显示
│   ├── settings.py            # 可注入配置 (dataclass)
│   ├── planner/
│   │   ├── planner.py         # 计划生成器
│   │   ├── reflector.py       # 反思纠正
│   │   └── task_plan.py       # 任务计划数据结构
│   ├── executor/
│   │   ├── executor.py        # 步骤执行器
│   │   ├── retry.py           # 重试策略
│   │   └── step_context.py    # 步骤上下文
│   ├── tools/
│   │   ├── registry.py        # 工具注册表
│   │   ├── router.py          # 工具路由器
│   │   ├── schema.py          # ToolProtocol / ToolCall / ToolResult
│   │   ├── skill.py           # 组合工具抽象
│   │   ├── builtin/
│   │   │   ├── file_tools.py  # 读写/编辑/搜索文件
│   │   │   ├── code_tools.py  # 执行/检查/格式化代码
│   │   │   ├── web_tools.py   # 网络搜索/抓取
│   │   │   ├── system_tools.py# 系统命令/进程/时间
│   │   │   └── memory_tools.py# 记忆存储/检索/笔记
│   │   └── adapters/          # 工具适配器 (扩展用)
│   ├── memory/
│   │   ├── memory_manager.py  # 记忆管理器
│   │   ├── short_term.py      # 短期记忆 (对话)
│   │   ├── long_term.py       # 长期记忆 (知识)
│   │   ├── vector_store.py    # Chroma 向量存储
│   │   ├── compressor.py      # 上下文压缩
│   │   └── retriever.py       # 记忆检索
│   ├── providers/
│   │   ├── base.py            # ProviderProtocol
│   │   ├── openai_provider.py # OpenAI 兼容 API
│   │   └── factory.py         # 自动路由工厂
│   ├── llm/                   # LLM 客户端
│   └── logging_config.py      # 日志配置
└── tests/                     # 272 个测试
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

## 一些测试

```bash
# 全部测试
pytest tests/ -v

# 覆盖率
pytest tests/ -v --cov=agent --cov-report=term-missing

# 单个模块
pytest tests/test_planner.py -v
```

## 一些小小的设计原则

1. **最小依赖** — 不引入 LangChain 等重型框架。Agent 的核心逻辑不到 500 行，一眼就能看完。
2. **可测试性** — 所有组件通过 dataclass 注入配置，测试可覆盖任意组合参数。
3. **渐进增强** — 从最简单的 `deepseek-chat` 开始跑通，再逐层加 Planner、Reflector、Memory。
4. **安全第一** — 写操作默认需要确认，读操作直接执行，取消时优雅退出。

##  License

MIT — 随便用，随便改。详见 [LICENSE](LICENSE)。

##  致谢

奇天从零构建，但深受以下项目的启发：

| 项目 | 启发 |
|------|------|
| [AutoGPT](https://github.com/Significant-Gravitas/AutoGPT) | 任务规划与工具调用的结合方式 |
| [LangChain](https://github.com/langchain-ai/langchain) | Tool 抽象协议与 LLM 调用模式 |
| [CrewAI](https://github.com/crewAIInc/crewAI) | 多 Agent 协作与角色分工 |
| [AutoGLM](https://github.com/THUDM/AutoGLM) | 反思机制 (Reflection) 的设计思路 |
| [Rich](https://github.com/Textualize/rich) | 终端 UI 的可能性 (我们的流式显示基于它) |
| [Chroma](https://github.com/chroma-core/chroma) | 向量记忆的灵感和向量存储后端 |
| [DeepSeek](https://www.deepseek.com/) | 提供高性能、低成本的模型 API |

> **声明**: 感谢开源社区让一个 16 岁的 builder 能站在巨人的肩膀上。

---

<p align="center">
  <sub>Made with ☀️ by a 16-year-old builder who believes AGI starts with clean code.</sub>
</p>
