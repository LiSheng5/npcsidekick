# Claude 级 Agent v2.0 - 快速开始指南

## 🎯 30 秒快速上手

### 第一步：配置 API 密钥

编辑 `d:\ai\ai.py`，找到顶部配置部分：

```python
# API 配置
API_KEY = None  # ← 改为你的 API 密钥
BASE_URL = None  # 可选：自定义 API 端点
```

**或者** 设置环境变量：
```powershell
# PowerShell
$env:OPENAI_API_KEY="sk-xxxxxx"
$env:OPENAI_API_BASE="https://api.openai.com/v1"
```

### 第二步：运行 Agent

```powershell
cd d:\ai
python ai.py
```

### 第三步：开始对话

```
你：现在几点？
AI：[思考过程] → [工具调用] → [结果]
答案：当前时间是...

你：计算 3^2 + 4^2
AI：[分析问题] → [调用 calculator] → [返回结果]
答案：3^2 + 4^2 = 25

你：记住我叫小明
AI：[理解意图] → [调用 remember_fact] → [保存成功]
答案：我已记住你叫小明。
```

---

## 📚 核心命令

### 基础命令

| 命令 | 功能 |
|------|------|
| 任何问题 | Agent 会自动选择合适的工具 |
| `help` / `帮助` | 显示帮助信息 |
| `exit` / `quit` | 退出程序 |

### 高级命令

| 命令 | 功能 | 示例输出 |
|------|------|---------|
| `查看记忆` | 显示事实和历史 | 5 条事实 + 最后 10 条历史 |
| `查看执行状态` | 显示上一次执行的完整状态 | 思考过程、工具调用、决策理由 |
| `查看执行历史` | 显示所有执行记录 | 最后 5 条执行记录的摘要 |

---

## 🚀 典型使用场景

### 场景 1: 简单查询

```
你：现在几点？

执行流程：
1. [PLAN] Agent 识别出需要查询时间
2. [ACT] 调用 get_current_time 工具
3. [OBSERVE] 获取当前时间
4. [REFLECT] 返回答案

答案：现在是 2024-12-19 14:35:22
```

### 场景 2: 计算与记忆

```
你：计算 100 人中有多少秒
（假设这是个复杂问题需要多步推理）

执行流程：
1. [PLAN] 分析：需要理解"100 人中"的含义 → 可能是年？
2. [ACT] 调用 calculator: 365 * 24 * 60 * 60
3. [OBSERVE] 得到结果：31536000 秒
4. [REFLECT] 可以直接返回，或继续
5. [记忆] 用户也许会在后续询问

答案：一年有 31,536,000 秒
```

### 场景 3: 多工具流程

```
你：保存我学的 Python 知识并记住这是我的兴趣

执行流程：
1. [PLAN] 两个意图：
   - 保存笔记（使用 save_note）
   - 记住事实（使用 remember_fact）

2. [ACT] 执行两个工具调用：
   - save_note(note="我学的 Python 知识")
   - remember_fact(fact="用户对 Python 感兴趣")

3. [OBSERVE] 两个工具都成功

4. [REFLECT] 返回确认信息

答案：已保存笔记并记住你对 Python 的兴趣
```

---

## 🔍 理解执行状态

当执行 `查看执行状态` 时，你会看到：

```
============================================================
📊 执行状态详情
============================================================
输入: 计算 2+2*3
完成: 是
步数: 1

🧠 思考过程:
  [步 1] 用户要求计算表达式 2+2*3。这需要使用 calculator 工具...

🔧 工具调用:
  - calculator: {"expression": "2+2*3"}
    理由: 直接调用计算器工具执行数学表达式...

✅ 工具结果:
  - calculator: 8

⚙️ 决策日志:
  [步 1] 选择工具: calculator
      理由: 用户明确要求计算表达式，这是最直接的选择...

💬 最终答案: 2+2*3 = 8
============================================================
```

### 各部分说明

- **输入**: 你输入的问题
- **完成**: 是否成功完成任务
- **步数**: 使用了多少推理步骤（1-3）
- **思考过程**: Agent 的中间思考记录
- **工具调用**: 调用了哪些工具及其参数
- **工具结果**: 工具的执行结果
- **决策日志**: 为什么选择这个工具的理由
- **最终答案**: Agent 的最终回复

---

## 💾 数据存储

### 文件结构

```
d:\ai\
├── ai.py                    ← 主程序
├── memory.json              ← 持久化内存
├── notes.txt                ← 笔记存储
│
├── README_Agent_v2.md       ← 功能说明书
├── ARCHITECTURE.md          ← 架构设计文档
├── QUICKSTART.md            ← 本文件
│
├── demo_agent.py            ← 演示脚本
└── tests/                   ← 测试目录（待建立）
```

### memory.json 结构

```json
{
  "facts": ["事实 1", "事实 2", ...],
  "history": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."},
    ...
  ],
  "summary": "旧历史的摘要",
  "execution_log": [
    {
      "input": "用户问题",
      "step": 2,
      "thoughts": [...],
      "tool_calls": [...],
      "final_answer": "..."
    },
    ...
  ]
}
```

---

## ⚙️ 配置选项

在 `ai.py` 顶部修改这些值：

```python
# API 配置
API_KEY = None                              # 你的 API 密钥
BASE_URL = None                             # API 端点

# 模型配置
MODEL_NAME = "deepseek-chat"               # 使用的模型
MAX_STEPS = 3                              # 最多推理步骤
MAX_HISTORY = 100                          # 保留的历史条数
MAX_MEMORY_FACTS = 50                      # 保留的事实条数

# 在 orchestrate_agent 中
temperature = 0.2                          # 模型温度（低 = 确定性）
```

---

## 🐛 常见问题

### Q1: 为什么 Agent 有时不用工具？

**A**: 这是正常的！Agent 足够聪慧时会直接回答。

```
你：1+1 等于多少？
AI：直接回答 2 （不需要 calculator 工具）

你：计算 2^32
AI：调用 calculator 工具 （需要确保精确）
```

### Q2: 如何清空记忆？

**A**: 删除或重置 `memory.json`：

```powershell
# 清空记忆
Remove-Item d:\ai\memory.json

# 程序会自动创建新的 memory.json
```

### Q3: 如何添加新工具？

**A**: 见 [ARCHITECTURE.md](ARCHITECTURE.md) 中的扩展机制章节。

简要步骤：
1. 写工具函数
2. 添加到 `TOOL_FUNCTIONS` 字典
3. 添加到 `TOOLS` 列表（OpenAI 定义）

### Q4: 为什么有时候答案不准确？

**A**: 可能的原因：
- Model 选择了错误的工具 → 调整 SYSTEM_PROMPT
- 工具本身有限制 → 改进工具实现
- 参数理解有误 → Agent 在下一步会纠正

### Q5: 能不能让 Agent 并行执行工具？

**A**: 当前版本是顺序执行。框架已预留扩展接口，可以在 `orchestrate_agent` 中使用 `asyncio` 实现并行。

---

## 🎓 学习路径

### 初级（10 分钟）
1. 配置 API 密钥
2. 运行 `python ai.py`
3. 尝试几个简单问题
4. 执行 `查看记忆` 查看数据

### 中级（30 分钟）
1. 阅读 [README_Agent_v2.md](README_Agent_v2.md) 了解功能
2. 尝试复杂问题（多步推理）
3. 执行 `查看执行状态` 理解 Agent 思考过程
4. 学习如何添加笔记和记忆

### 高级（1 小时）
1. 阅读 [ARCHITECTURE.md](ARCHITECTURE.md) 了解内部实现
2. 查看代码中的 `AgentState`, `ToolPipeline`, `orchestrate_agent`
3. 尝试扩展：添加新工具、修改 Prompt
4. 运行 `demo_agent.py` 查看演示

---

## 📊 执行统计

运行 `查看执行历史` 可以看到：

```
📜 共 5 条执行记录:

记录 1:
  输入: 现在几点...
  步数: 1
  工具调用: 1 次
  答案: 当前时间是...

记录 2:
  输入: 计算 2+2...
  步数: 1
  工具调用: 1 次
  答案: 结果是 4...

...
```

---

## 🔗 相关资源

- [完整功能说明](README_Agent_v2.md) - 所有功能详解
- [架构设计文档](ARCHITECTURE.md) - 内部实现详情
- [演示脚本](demo_agent.py) - 运行代码示例

---

## ✅ 检查清单

- [ ] 设置了 API_KEY
- [ ] 运行过 `python ai.py`
- [ ] 成功执行过至少 3 个问题
- [ ] 运行过 `查看记忆`
- [ ] 运行过 `查看执行状态`
- [ ] 理解了 Plan-Act-Observe-Reflect 流程
- [ ] 读过 README_Agent_v2.md
- [ ] 读过 ARCHITECTURE.md

---

## 🎉 下一步

现在你已经掌握了 Claude 级 Agent v2.0！

**可以继续尝试**：
- 用 Agent 完成你的日常任务
- 通过笔记和记忆构建知识库
- 观察 Agent 的推理过程，改进 Prompt
- 为 Agent 添加更多工具

**有问题？**
- 查看代码中的注释
- 查看 `README_Agent_v2.md` 的常见问题
- 查看 `ARCHITECTURE.md` 的详细设计

**想要贡献或改进？**
- Fork 或修改代码
- 添加新的工具
- 改进 Prompt 和决策逻辑
- 添加新的功能（工具链、并行执行、等等）

---

**祝你使用愉快！🚀**

*Claude 级 Agent v2.0 - 生产级多工具编排 AI 系统*
