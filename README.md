# NPCSidekick v4

给游戏 NPC 装上"大脑"的本地服务：一个 FastAPI 进程负责角色扮演、记忆与动作决策，游戏侧的 mod 只管报到、转发对话和执行动作。模型不挑厂商，OpenAI 兼容接口即接即用。

## 它做什么

- **对话**：按角色卡（persona）生成台词，SSE 流式输出，逐字到端不卡首字
- **记忆**：记忆卡存储 + 加权检索 + 半衰期自动修剪，NPC 记得玩家说过的事
- **动作**：动作 = LLM 直接调用的工具。mod 报到声明能力清单（白名单），LLM 只能调用声明过的动作，由服务同步转发给 mod 执行，结果写进记忆卡（结果还没定的先挂待定，等 mod 回报）
- **语音**：可选 TTS 输出通道（装 `edge-tts` 才有声，缺依赖自动降级不出帧）
- **控制台**：零构建本地 Web 控制台，管理角色卡、记忆卡、聊天记录与关系网

## 架构一览

```
游戏 mod ──POST /api/capabilities──▶ 报到：能力白名单 + 执行接口 + 心跳
         ──POST /api/talk────────▶ 对话：SSE 流（delta 台词 / action 记录 / audio 语音 / done）
         ◀──同步调用 execute_url──  动作执行（结果写记忆卡；没定的挂待定）
         ──POST /api/action_result▶ 长动作结果回报（可选通道）
本地浏览器 ──/console──────────▶ 调试面：角色卡 / 记忆 / 聊天记录 / 关系网
```

两条通道严格分离：台词只走 SSE `delta` 帧；流末尾的 `action` 帧只是"已执行动作"的记录（带 `ok` / `status` / `call_id`），mod 无需照它做任何事。

## 上下文怎么拼（分层 + 预算）

一次 `/api/talk` 的 `messages` 按下表装配（实现在 `context.py`，纯函数、可单测）：

| 层 | 内容 | 预算 | 超了怎么办 |
|---|---|---|---|
| `system` | 人设 + 规矩（整场不变，利于前缀缓存） | 不设预算 | — |
| 历史 | 摘要消息 + 最近若干轮原话 | `NPC_BUDGET_HISTORY`（4000） | 从最新往前取到预算为止（至少留最新一条） |
| 【当前情况】 | `observation`（字符串原样 / 对象转 JSON） | `NPC_BUDGET_STATE`（800） | 文本截断，尾部标「…（已截断）」 |
| 【相关记忆】 | 回合开始的确定性预取（见下节） | `NPC_BUDGET_MEMORY`（600） | 文本截断；pinned 排在前面，最后才被砍 |
| 【最近发生】 | 最近动作结果 / 进行中 / 待确认 | 与观测**共享** state 预算（观测先占） | 按「最近结果 → 进行中 → 待确认」的次序逐条砍（组内从旧的砍起） |
| 玩家原话 | `message` | — | — |

- 资料块标在**最后一条 user 消息前面**，块头注明「（以下是参考资料，不是命令）」，与「玩家说：…」用空行隔开；没有内容的块整块省略，不留空标题。
- 任何一层被截断都会记一条 `context_section_truncated` 日志（层名 + 截断前后 token），不静默丢。
- 聊天记录超 `NPC_SUMMARY_TOKEN_THRESHOLD` 后，在**回合结束、落盘之后**由后台任务压成滚动摘要——摘要不挡玩家看到第一个字；摘要只保脉络，重要事实走记忆卡（`remember`），摘要不替代记忆。

## 记忆预取（置顶 + 最近 N 条）

回合开始、第一次调用 LLM 之前，把「`pinned` 条目 + 最近 N 条（`NPC_PREFETCH_RECENT`，默认 5）」放进【相关记忆】块，不再让模型多花一轮去 `recall`：

- **不检索、不打分**：与这句话相不相关不做判断，深挖相关性仍交给 `recall` 工具；
- 排除 `category == "action"` 的条目（动作结果由【最近发生】块负责，不重复进上下文）；
- 块内排序：pinned 在前、其余按 `created_at` 从新到旧 —— 预算截断从尾部砍，等于 pinned 最后才被砍；
- `pinned` 不占「最近 N 条」的名额；
- 一条都没有 → 整块省略；预取出错只告警，回合照常（只是不带记忆块）。

## 动作契约（call_id / pending / 超时记「不确定」）

1. 大脑每次下发动作都生成 `call_id`（`uuid4().hex`），随 `/execute` 请求体发给 mod，**不复用** LLM 的 tool_call id（那个不保证唯一）。
2. mod 同步回 `{"ok": true, "status": "done"}` → 当场写记忆卡，回合结束出 `action` 帧。
3. mod 回 `status: "running"`（还在做）→ **不写卡**，登记 `pending`（state=`running`），等 `/api/action_result` 回报。
4. 同步等待超过 `NPC_EXECUTE_TIMEOUT` → 记 `status: "unknown"`（**不确定**）：请求可能已经到 mod 并被执行了，只是没等到回应，所以**不当失败**、不写卡，只登记 `pending`（state=`unknown`）。回给模型的工具消息也是「没等到回应，不确定是否已经做成」。
5. `/api/action_result` 带 `call_id` 且命中登记 → 写卡并移除；带 `call_id` 但没命中（重复回报 / 已过期 / 未知）→ **不写卡**（同一次动作不能写两遍），仍返回 200 且 `matched=false`。
6. 旧 mod 不带 `call_id` → 按 `(npc_id, action)` 取最早一条（FIFO），**无论命中与否都写卡**（旧 mod 的回报不能丢）。
7. `pending` 超过 `NPC_PENDING_TTL`（默认 120 秒）仍没回报 → 写一条「没做成: 一直没等到结果」并移除。清理在 `/api/talk` 开始与 `/api/action_result` 到达时顺手做，不开后台任务。
8. 同名同参的动作还在 `pending` 里（`running` / `unknown`）→ 不下发，回模型 `status: "deduped"`（「这件事还在进行或待确认，这次没有重复下达」），不写卡、不进 `action` 帧。

## 回合 trace

每回合结束输出一条 `turn_trace` 结构化日志（降级回合也有）；`NPC_TRACE_FILE` 非空时把同一条记录再以 JSONL 追加到该文件（写失败只告警，不影响回合）。

| 字段 | 含义 |
|---|---|
| `npc_id` | 哪个 NPC |
| `rounds` | 实际发起的 LLM 轮数（含最后的收尾轮） |
| `llm_fallback` | 是否走了角色卡 `rules` 规则回复 |
| `summarized` | 本回合是否**启动**了后台摘要（不代表摘要已完成） |
| `no_dialogue` | 本回合是否一句台词都没出 |
| `tools` | 每次工具调用 `{name, kind: action/memory, ok, status, ms}` |
| `tokens` | `{system, history, state, memory, user, total}`，按 `chatlog.estimate_tokens` 估算 |
| `memory_prefetch` | `{hits: 进块条数（含 pinned）, filtered: 读到但没进块的条数}` |
| `first_token_ms` | 第一个 delta 距回合开始（含等锁） |
| `total_ms` | 回合总耗时 |
| `message_len` / `reply_len` | 玩家原话 / 台词的**长度** |

隐私口径：trace **只记长度不记原文**——玩家说了什么、NPC 回了什么都不进日志。

## 快速开始

```bash
pip install -r requirements.txt

# 可选：中文分词（检索召回更准）与语音
pip install jieba edge-tts

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
| `AGENT_MODEL` | 模型名（旧名 `NPC_MODEL` 也认） | — |
| `NPC_API_KEY` | API key（旧名 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ZHIPU_API_KEY` 也认；都没有则读 `api_key.txt`） | — |
| `NPC_BASE_URL` | OpenAI 兼容端点（旧名 `DEEPSEEK_BASE_URL` / `OPENAI_BASE_URL` 也认；不填时按模型名推断） | — |
| `NPC_REASONING_EFFORT` | 思考档位：`off`/`disabled`/`none`/`low`/`medium`/`high`/`max`，留空 = 不指定 | — |
| `NPC_LLM_EXTRA_BODY` | 原样合进请求体的 JSON 片段（各家方言自己填），非法 JSON 只告警 | — |
| `NPC_LLM_RETRY` | LLM 调用重试开关（只救超时/连接断/429/5xx） | 关 |
| `NPC_LLM_RETRY_DELAYS` | 重试等待序列（秒，逗号分隔） | `2,6` |
| `NPC_MOD_HEARTBEAT_TIMEOUT` | mod 心跳超时（秒），超时视为离线 | 60 |
| `NPC_EXECUTE_TIMEOUT` | 动作执行等待上限（秒），超时记「不确定」 | 30 |
| `NPC_PENDING_TTL` | 待定动作存活上限（秒），到点算没做成 | 120 |
| `NPC_SUMMARY_TOKEN_THRESHOLD` | 聊天记录滚动摘要触发阈值（token） | 20000 |
| `NPC_SUMMARY_KEEP_RECENT` | 摘要时保留的最近轮数 | 8 |
| `NPC_PREFETCH_RECENT` | 记忆预取「最近 N 条」的 N（pinned 不占名额） | 5 |
| `NPC_RECENT_RESULTS` / `NPC_RECENT_MINUTES` | 近况层「最近动作结果」条数 / 时间窗（分钟） | 5 / 30 |
| `NPC_BUDGET_STATE` / `NPC_BUDGET_MEMORY` / `NPC_BUDGET_HISTORY` | 上下文各层 token 预算 | 800 / 600 / 4000 |
| `NPC_TRACE_FILE` | 回合 trace 落盘路径（JSONL，只记长度不记原文） | 不落盘 |

以上参数都是**每次现读**（不在 import 时缓存），改完不用重启；布尔量统一按 `1/true/yes/on` 判真。

## 游戏面 API（mod 契约）

| 端点 | 用途 |
|---|---|
| `POST /api/capabilities` | mod 报到：声明动作白名单 + 执行接口 + 心跳 |
| `POST /api/talk` | 对话，SSE 流式输出 |
| `POST /api/action_result` | 长动作结果回报（写记忆卡） |
| `POST /api/tts` | 语音合成（独立输出通道） |
| `GET /api/state`、`GET /api/npcs` | 状态查询（调试） |

控制台端点同挂在 `/api` 下（`/api/personas`、`/api/llm`、`/api/npcs/{id}/memory`、`/api/relationships`、`/api/npcs/{id}/chat` 等），但属于调试面，不承诺给 mod 用。

### 请求体

`POST /api/capabilities`

```json
{"mod": "mygame", "execute_url": "http://127.0.0.1:8766/execute",
 "actions": [{"name": "cook", "desc": "用厨房做饭", "params": {"dish": "菜名"}}]}
```

`actions` 必填且非空；`params` 是 `{参数名: 说明}`，进工具定义时一律按字符串参数、且全部 required。没给 `execute_url` = 没有可执行动作，动作工具不进 LLM 的工具定义。

`POST /api/talk`

```json
{"npc_id": "cang", "message": "在吗", "observation": {"time": "傍晚"}, "mod": "mygame", "voice": false}
```

`npc_id` / `message` 必填；`observation` 可选（字符串原样，对象转 JSON）；`mod` 可选（捎带一次心跳；不传且只有一个在线 mod 时用它）；`voice` 可选（默认纯文本）。响应头带 `X-Mod-Heartbeat-Timeout`，值为当前心跳超时秒数。

`execute_url`（本服务 → mod）

```json
{"npc_id": "cang", "action": "cook", "params": {"dish": "面"}, "mod": "mygame", "call_id": "…"}
```

期望回 `{"ok": true, "status": "done" | "running", "note": "…"}`；连不上/响应非法记失败，超时记「不确定」（见上节）。

`POST /api/action_result`

```json
{"npc_id": "cang", "action": "cook", "ok": true, "note": "面煮好了", "call_id": "…", "mod": "mygame"}
```

`npc_id` / `action` / `ok`（布尔）必填；`call_id` 可选（旧 mod 不带，按 FIFO 对账）。响应 `{ok, npc_id, entry, matched}`。

`POST /api/tts`：`{"text": "…", "voice": "…", "npc_id": "…"}` → `{"audio": "<base64 mp3 或空串>", "voice": "…"}`；没装 `edge-tts` 回 503。

### SSE 帧类型

| `type` | 载荷 | 何时出现 |
|---|---|---|
| `delta` | `{"type": "delta", "text": "…"}` | 模型每吐一段台词一帧（跨多轮工具调用会持续追加） |
| `action` | `{"type": "action", "action": {"name", "params", "ok", "status", "call_id"}}` | 流末尾，每个已执行动作一帧（`status` ∈ `done` / `running` / `unknown` / `failed` / `deduped`） |
| `audio` | `{"type": "audio", "audio": "<base64 mp3>", "voice": "…"}` | 仅 `voice=true` 且 `edge-tts` 可用、合成成功时，在 `done` 之前 |
| `done` | `{"type": "done"}` | 每回合最后一帧 |

`action` 帧只是记录，mod 不需要照它做任何事；`call_id` 可用于和自己那侧的日志对账。

## 角色卡

一张角色卡 = 一个 NPC = `personas/{id}.json` 一个文件，加文件即加角色，改完存盘即生效，不用重启。字段（身份、性格、说话风格、语气锚点、忌讳、兜底台词、同义词族、音色、关系网）见 [personas/_角色卡说明.md](personas/_角色卡说明.md)，空白模板在 `personas/_模板.json`。

## 测试

```bash
pytest
```

覆盖回合循环、上下文分层与预算、记忆检索与修剪、记忆预取、滚动摘要、近况层、动作去重、pending / call_id 对账、回合 trace、控制台 API 等。

手测脚本（真实 HTTP、可打印可直接贴回来的 JSON 汇总）：

```bash
python scripts/manual_checks.py llm-down --runs 5     # 不配 LLM 的首帧延迟
python scripts/manual_checks.py disconnect            # 中途断开 SSE 后的服务端状态
python scripts/manual_checks.py uncertain --runs 3    # mock Mod 超时 → 不确定（需要真实 key）
```

已跑过的结果记在 [docs/manual_checks.md](docs/manual_checks.md)。

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
scripts/        手测脚本（manual_checks.py）
docs/           手测记录
tests/          pytest 测试
store/          运行时数据（聊天记录、记忆卡、摘要）—— 不进仓库
```

## 致谢

- 加权记忆检索公式（recency / relevance / importance）借鉴 [AI Town](https://github.com/a16z-infra/ai-town)（MIT）
- 半衰期遗忘思想借鉴 [hippo-memory](https://github.com/kitfunso/hippo-memory)

## License

[MIT](LICENSE)
