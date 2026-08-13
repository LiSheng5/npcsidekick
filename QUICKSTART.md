# NPCSidekick — 快速开始指南（引擎: NPCSidekick ）

## 环境要求

- Python 3.10+
- pip

## 安装

```bash
cd <仓库根目录>
pip install -r requirements.txt -r requirements-dev.txt
```

## 配置 API 密钥

**方式一**：环境变量
```bash
set OPENAI_API_KEY=your-key-here
# 或
set DEEPSEEK_API_KEY=your-key-here
# 或 (智谱 GLM)
set ZHIPU_API_KEY=your-key-here
```

**方式二**：在项目根目录创建 `api_key.txt`，写入密钥。

## 运行

```bash
# 交互模式（标准）
python main.py

# 交互模式（流式 Rich Live 显示）
python main.py --stream

# 单次查询
python main.py "现在几点？"

# 流式单次查询
python main.py --stream "解释 Python 闭包"

# 指定模型 (自动识别 Provider)
python main.py --model deepseek-v4-pro     # DeepSeek V4 Pro
python main.py --model deepseek-v4-flash   # DeepSeek V4 Flash (轻量)
python main.py --model gpt-5.6-sol         # OpenAI GPT-5.6 Sol (旗舰)
python main.py --model gpt-5.6-terra       # OpenAI GPT-5.6 Terra (均衡)
python main.py --model glm-5.2             # 智谱 GLM-5.2 (开源)
python main.py --model gpt-4               # OpenAI GPT-4
```

## 交互命令

在交互模式下支持：
- `exit` / `quit` — 退出
- `help` — 帮助
- `plan` — 查看当前计划
- `memory` — 查看记忆
- `tools` — 列出工具
- `history` — 对话历史
- `stream` — 切换流式模式
- `standard` — 切换标准模式

## API 使用

```python
from agent.orchestrator import AgentOrchestrator
import asyncio

orch = AgentOrchestrator()
orch.initialize()

# 同步查询
answer = orch.run("帮我分析 d:/NPCSidekick/config.py")
print(answer)

# 流式查询
async def main():
    async for event in orch.run_stream("现在几点？"):
        print(event)

asyncio.run(main())
```

## 运行测试

```bash
# 全部测试
pytest tests/ -v

# 单个文件
pytest tests/test_planner.py -v

# 关键词过滤
pytest tests/ -k "test_run_single" -v

# 覆盖率
pytest tests/ -v --cov=agent --cov-report=term-missing
```
