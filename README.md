# NPCSidekick v4

给游戏 NPC 装上"大脑"的本地服务：一个 FastAPI 进程负责角色扮演、记忆与动作决策，游戏侧的 mod 只管报到、转发对话和执行动作。模型不挑厂商，OpenAI 兼容接口即接即用。

## 它做什么

- **对话**：按角色卡（persona）生成台词，SSE 流式输出，逐字到端不卡首字
- **记忆**：记忆卡存储 + 加权检索 + 半衰期自动修剪，NPC 记得玩家说过的事
- **动作**：动作 = LLM 直接调用的工具。mod 报到声明能力清单（白名单），LLM 只能调用声明过的动作，由服务同步转发给 mod 执行，结果当场写进记忆
- **语音**：可选 TTS 输出通道（装 `edge-tts` 才有声，缺依赖自动降级不出帧）
- **控制台**：零构建本地 Web 控制台，管理角色卡、记忆卡、聊天记录与关系网

## 架构一览

```
游戏 mod ──POST /api/capabilities──▶ 报到：能力白名单 + 执行接口 + 心跳
         ──POST /api/talk────────▶ 对话：SSE 流（delta 台词 / action 记录 / audio 语音 / done）
         ◀──同步调用 execute_url──  动作执行（结果当场写记忆卡）
         ──POST /api/action_result▶ 长动作结果回报（可选通道）
本地浏览器 ──/console──────────▶ 调试面：角色卡 / 记忆 / 聊天记录 / 关系网
```

两条通道严格分离：台词只走 SSE `delta` 帧；流末尾的 `action` 帧只是"已执行动作"的记录，mod 无需照它做任何事。

上下文按层装配（`context.py`）：固定层（人设 + 规矩，利于前缀缓存）→ 【当前情况】→【相关记忆】（回合开始确定性预取）→【最近发生】（近况层）→ 玩家原话，每层有 token 预算与截断标记。聊天记录超阈值后由 LLM 压成滚动摘要，摘要只保脉络，重要事实走记忆卡。

## 快速开始

```bash
pip install -r requirements.txt

# 配置 LLM 三件套（不预设厂商，OpenAI 兼容即可）
set AGENT_MODEL=your-model-name
set NPC_API_KEY=sk-...
set NPC_BASE_URL=https://your-endpoint/v1

uvicorn server:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/> 即进入控制台（重定向到 `/console/`）。

> key 也可以放在 `api_key.txt`（不进仓库）或控制台里填；想更安全就删掉文件、只用环境变量。

## 主要配置（环境变量）

| 变量 | 说明 | 默认 |
|---|---|---|
| `AGENT_MODEL` | 模型名 | — |
| `NPC_API_KEY` | API key | — |
| `NPC_BASE_URL` | OpenAI 兼容端点 | — |
| `NPC_REASONING_EFFORT` | 推理强度 | — |
| `NPC_MOD_HEARTBEAT_TIMEOUT` | mod 心跳超时（秒），超时视为离线 | 60 |
| `NPC_EXECUTE_TIMEOUT` | 动作执行等待上限（秒） | 30 |
| `NPC_PENDING_TTL` | 待定动作存活上限（秒） | 120 |
| `NPC_SUMMARY_TOKEN_THRESHOLD` | 聊天记录滚动摘要触发阈值（token） | 20000 |
| `NPC_SUMMARY_KEEP_RECENT` | 摘要时保留的最近轮数 | 8 |
| `NPC_PREFETCH_RECENT` | 记忆预取"最近 N 条" | 5 |
| `NPC_RECENT_RESULTS` / `NPC_RECENT_MINUTES` | 近况层"最近动作结果"条数 / 时间窗 | 5 / 30 |
| `NPC_BUDGET_STATE` / `NPC_BUDGET_MEMORY` / `NPC_BUDGET_HISTORY` | 上下文各层 token 预算 | 800 / 600 / 4000 |
| `NPC_LLM_RETRY` | LLM 调用重试开关（默认关） | 关 |
| `NPC_TRACE_FILE` | 回合 trace 落盘（JSONL，只记长度不记原文） | 不落盘 |

大部分参数现读环境变量，可热切，改完不用重启。

## 游戏面 API（mod 契约）

| 端点 | 用途 |
|---|---|
| `POST /api/capabilities` | mod 报到：声明动作白名单 + 执行接口 + 心跳 |
| `POST /api/talk` | 对话，SSE 流式输出 |
| `POST /api/action_result` | 长动作结果回报（写记忆卡） |
| `POST /api/tts` | 语音合成（独立输出通道） |
| `GET /api/state`、`GET /api/npcs` | 状态查询（调试） |

控制台端点（角色卡 / 记忆 / 聊天记录等）是独立的调试面，不属于 mod 契约。

## 角色卡

一张角色卡 = 一个 NPC = `personas/{id}.json` 一个文件，加文件即加角色，改完存盘即生效，不用重启。字段（身份、性格、说话风格、语气锚点、忌讳、兜底台词、同义词族、音色、关系网）见 [personas/_角色卡说明.md](personas/_角色卡说明.md)，空白模板在 `personas/_模板.json`。

## 测试

```bash
pytest
```

覆盖回合循环、记忆检索与修剪、上下文装配、滚动摘要、动作去重、pending、控制台 API 等。

## 目录结构

```
server.py       HTTP 服务：mod 契约端点 + 应用装配
turn.py         回合主体：LLM ↔ 工具多轮循环（产出 SSE 帧）
context.py      上下文分层装配（纯函数，可单测）
memory.py       记忆卡：条目 + 加权检索 + 半衰期修剪
chatlog.py      持久聊天记录 + 滚动摘要
tools.py        remember / recall + 动作工具定义（白名单闸门）
pending.py      待定动作登记（长动作回报对账）
tts.py          语音合成
console_api.py  控制台端点（调试面）
core/           LLM 接入（OpenAI 兼容）、配置、角色卡编译、日志
personas/       角色卡（每 NPC 一个 JSON）
web/console/    本地控制台（零构建：index.html + app.js + styles.css）
tests/          pytest 测试
store/          运行时数据（聊天记录、记忆卡、摘要）—— 不进仓库
```

## 致谢

- 加权记忆检索公式（recency / relevance / importance）借鉴 [AI Town](https://github.com/a16z-infra/ai-town)（MIT）
- 半衰期遗忘思想借鉴 [hippo-memory](https://github.com/kitfunso/hippo-memory)

## License

[MIT](LICENSE)
