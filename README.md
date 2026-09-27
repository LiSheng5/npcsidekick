# NPCSidekick v4

[简体中文](./README.md) | [English](./README.en.md)

通用游戏 AI NPC 系统的大脑（本地 Python 服务器）——任何能发 HTTP 的游戏（通过 mod）都能接入。

**核心原则：大脑提议，游戏执行。** LLM 只提议动作（结构化字段，与对话文字完全分离），执行归游戏；
唯一闸门是 mod 报到声明的**能力白名单**——大脑只能提议 mod 说过自己能做的事。

## 特性

- 🧠 **记忆卡**：确定性遗忘（强度按半衰期衰减），可钉住豁免，零 LLM 成本
- 🔌 **厂商无关**：只走 OpenAI 兼容端点——DeepSeek / OpenAI / 千问 / Kimi / GLM / Ollama / OpenRouter 换三个环境变量即接
- 🎮 **能力白名单**：mod 报到声明能做什么，大脑就只能提议什么
- 🖥️ **零构建控制台**：真流式试对话、记忆卡管理、关系网图、模拟 mod 报到（不用进游戏就能联调）
- 🗣️ 可选语音合成（edge-tts）、滚动摘要、多人格（`personas/` 加一个 JSON 就多一个 NPC）

## 快速开始

```bash
pip install -r requirements.txt

# 配大脑三件套（key 也可放工程根 api_key.txt，或在控制台「模型」页直接填）
export AGENT_MODEL=deepseek-flash
export NPC_API_KEY=sk-xxx
export NPC_BASE_URL=https://api.deepseek.com

python -m uvicorn server:app --host 127.0.0.1 --port 8765
```

浏览器打开 **http://127.0.0.1:8765/console/** 即是调试控制台。三件套没配齐也能起服务——此时对话回退角色卡里的 `rules` 回复、不提议动作，控制台会用红字写明缺哪几项。服务器只绑 `127.0.0.1`，不出机器。

## 四个端点

```bash
# 1) mod 报到：声明能力白名单 + 心跳
curl -X POST http://127.0.0.1:8765/api/capabilities -H "Content-Type: application/json" \
  -d '{"mod":"sims4","actions":[{"name":"cook","desc":"用厨房做饭","params":{"dish":"菜名(字符串)"}}]}'

# 2) 对话（SSE：delta / action / audio / done 帧）
curl -X POST http://127.0.0.1:8765/api/talk -H "Content-Type: application/json" \
  -d '{"npc_id":"cang","message":"你能帮我做顿饭吗","mod":"sims4",
       "observation":{"summary":"玩家在厨房，刚下班，有点饿"}}'

# 3) 动作结果回报（确定性写记忆卡："完成: …" / "没做成: …"）
curl -X POST http://127.0.0.1:8765/api/action_result -H "Content-Type: application/json" \
  -d '{"npc_id":"cang","action":"cook","ok":true,"note":"面煮好了，玩家吃了说不错"}'

# 4) 状态查询
curl http://127.0.0.1:8765/api/state
```

## 文档

| 文档 | 内容 |
|---|---|
| [配置详解.md](./配置详解.md) | 换模型厂商全表 / API Key 保护 / 全部环境变量 / 记忆卡格式 |

## 测试

```bash
python -m pytest tests -q
```

测试全零网络：LLM 一律用 `tests/conftest.py` 里的 `FakeProvider` 注入。

## License

MIT
