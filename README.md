# 失控NPC v4

让NPC拥有记忆行为能对玩家的行为做出反应。
给游戏 NPC 装上"大脑"的本地服务：一个 FastAPI 进程负责角色扮演、记忆与动作决策，游戏侧的 mod 只管报到、转发对话和执行动作。模型不挑厂商，OpenAI 兼容接口即接即用。

## 它做什么

- **对话**：按角色卡（persona）生成台词，SSE 流式输出，逐字到端不卡首字
- **记忆**：记忆卡 + 相关度检索，回合开始按置顶和重要度预取，NPC 记得玩家说过的事
- **动作**：动作 = LLM 直接调用的工具。mod 报到声明能力清单（白名单），LLM 只能调用声明过的动作，由服务同步转发给 mod 执行，结果当场写进记忆
- **语音**：可选 TTS 输出通道（装 `edge-tts` 才有声，缺依赖自动降级不出帧）
- **控制台**：零构建本地 Web 控制台，管理角色卡、记忆卡、聊天记录与关系网

## 架构一览

```
游戏 mod ──POST /api/capabilities──▶ 报到：能力白名单 + 执行接口（之后请求带 mod 即续心跳）
         ──POST /api/talk────────▶ 对话：SSE 流（delta 台词 / action 记录 / audio 语音 / done）
         ◀──POST execute_url─────  动作执行（大脑同步请求 mod，结果当场写记忆卡）
         ──POST /api/action_result▶ 长动作结果回报（可选通道，按 call_id 对账）
本地浏览器 ──/console──────────▶ 调试面：角色卡 / 记忆 / 聊天记录 / 关系网
```

两条通道严格分离：台词只走 SSE `delta` 帧；流末尾的 `action` 帧只是"已执行动作"的记录（带 `ok` / `status` / `call_id`），mod 无需照它做任何事。

动作执行由**大脑**发起：`turn.py` 用 `httpx` POST 到该 mod 报到时给的 `execute_url`，payload 为 `{npc_id, action, params, mod, call_id}`；mod 在线状态按心跳判断，离线/没声明动作/没给执行接口时，动作工具不会进 LLM 的工具定义。

## 执行结果与「不确定」契约

一次动作调用的结果分四种，写不写记忆卡由结果决定：

| `status` | 含义 | 写记忆卡 | pending |
|---|---|---|---|
| `done` | mod 明确回了结果（成功/失败都算） | 是（`完成: …` / `没做成: …`） | 不登记 |
| `failed` | 连不上 mod / 响应非法（请求大概率没到） | 是（`没做成: …`） | 不登记 |
| `running` | mod 说还在做 | 否（等回报） | 登记 `state=running` |
| `unknown` | 同步等待超时（`NPC_EXECUTE_TIMEOUT`），**不代表没做成** | 否（等回报） | 登记 `state=unknown` |
| `deduped` | 同名同参的动作已在途，本次没下发给 mod | 否 | 不登记 |

- `call_id` 由服务端生成（`uuid4().hex`），随执行请求发给 mod，也出现在 `action` 帧里；mod 回报时原样带回。不复用 LLM 的 tool_call id（那个不保证唯一）。
- 回报匹配（`POST /api/action_result`）：
  1. 带 `call_id` 且命中登记 → 写卡；`note` 为空时用登记的 `params` 拼一句描述
  2. 带 `call_id` 但没命中（重复回报 / 已过期 / 未知）→ **不写卡**（同一次动作不能写两遍），仍返回 200，`matched=false`
  3. 不带 `call_id`（旧 mod）→ 按 `(npc_id, action)` 取最早一条登记（FIFO），**无论命中与否都写卡**——旧 mod 的回报不能丢
- 待定动作存活上限 `NPC_PENDING_TTL`（默认 120 秒）。`/api/talk` 开始与 `/api/action_result` 到达时各清理一次；TTL 到点仍没回报 → 写一条 `没做成: 一直没等到结果` 并移除登记。
- 重复动作保护：同 NPC、同名、同参数的动作还在 `running` / `unknown` → 不下发，回 `deduped`（LLM 重发新 `call_id` 时幂等拦不住，只能按名 + 参识别；参数不同 = 新指令）。
- 工具调用轮次上限 3 轮（记忆与动作共用）；用满后追加一个**不带工具**的收尾轮，避免"动作做了、台词一句没有"。

## SSE 帧类型

`POST /api/talk` 成功时返回 `text/event-stream`。每帧只有 `data:` 一行（JSON，UTF-8），**没有 event 名**：

| `type` | 载荷 | 何时出现 |
|---|---|---|
| `delta` | `{"type":"delta","text":"..."}` | 台词增量，可能多帧；LLM 不可用时是角色卡 `rules` 的兜底回复 |
| `action` | `{"type":"action","action":{name, params, ok, status, call_id}}` | 本回合已执行的动作记录，可能多帧；`deduped` 不出帧 |
| `audio` | `{"type":"audio","audio":"<base64>","voice":"..."}` | 仅 `voice=true`、装了 `edge-tts` 且合成成功时；缺依赖/超时不出帧 |
| `done` | `{"type":"done"}` | 总在最后 |

帧顺序：`delta`… → `action`… → `audio`? → `done`。请求字段非法、角色卡不存在等错误返回 **400 JSON**（不是 SSE）。

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

> key 也可以放在 `api_key.txt`（不进仓库）或控制台「模型」页里填（后者明文落 `store/llm_config.json`）；想更安全就删掉文件、只用环境变量。接口与日志只给掩码。

## 主要配置（环境变量）

| 变量 | 说明 | 默认 |
|---|---|---|
| `AGENT_MODEL` / `NPC_MODEL` | 模型名（`NPC_MODEL` 是旧名） | — |
| `NPC_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ZHIPU_API_KEY` | API key（旧名为兼容保留；也可放 `api_key.txt` 或控制台） | — |
| `NPC_BASE_URL` / `DEEPSEEK_BASE_URL` / `OPENAI_BASE_URL` | OpenAI 兼容端点；空 = 按模型名猜厂商 | — |
| `NPC_REASONING_EFFORT` | 思考档位：`off`/`disabled`/`none`/`low`/`medium`/`high`/`max`；空 = 不指定。按 DeepSeek 官方格式下发：`reasoning_effort` 与 `thinking` 都在请求体**顶层**（互不嵌套） | — |
| `NPC_LLM_RETRY` | LLM 瞬时故障重试开关（布尔词表见下） | 关 |
| `NPC_LLM_RETRY_DELAYS` | 每次补试前等待的秒数，逗号分隔 | `2,6` |
| `NPC_LLM_EXTRA_BODY` | 透传进请求体的 JSON 片段（各家方言自填；非法 JSON 只告警） | 空 |
| `NPC_MOD_HEARTBEAT_TIMEOUT` | mod 心跳超时（秒），超时视为离线 | 60 |
| `NPC_EXECUTE_TIMEOUT` | 动作执行同步等待上限（秒），超时记「不确定」 | 30 |
| `NPC_PENDING_TTL` | 待定动作存活上限（秒） | 120 |
| `NPC_CONTEXT_WINDOW` | 当前模型的上下文窗口（token）。**换模型要同步改**，压缩阈值按它算 | 1000000 |
| `NPC_SUMMARY_RATIO` | 提示用到窗口的这个比例就触发滚动压缩 | 0.4 |
| `NPC_SUMMARY_KEEP_RECENT` | 压缩后仍逐字保留的最近轮数 | 3 |
| `NPC_MEM_RECALL_MIN_REL` | recall 相关度门槛（低于门槛的条目不返回） | 0.2 |
| `NPC_RECENT_RESULTS` / `NPC_RECENT_MINUTES` | 近况层「最近动作结果」条数 / 时间窗（分钟） | 5 / 30 |
| `NPC_BUDGET_STATE` / `NPC_BUDGET_MEMORY` / `NPC_BUDGET_HISTORY` | 上下文各层 token 预算；`NPC_BUDGET_HISTORY` = 历史硬上限，未设时为窗口 × 0.7（只防摘要失败时撑爆上下文） | 800 / 600 / 窗口 × 0.7 |
| `NPC_TRACE_FILE` | 回合 trace 落盘（JSONL，只记长度不记原文） | 不落盘 |

布尔词表：`1` / `true` / `yes` / `on` 为开（大小写、首尾空白不敏感），其余一律关。数值项未设置/空/非法 → 回落默认值。除 `api_key.txt` 权限检测外，全部**每次现读环境变量**，改完不用重启（可热切）。

> 思考模式：请求带 `tools` 时，DeepSeek 官方要求把历史 assistant 的 `reasoning_content` 原样回传（缺了直接 400）。所以回合内的工具轮与历史消息都会带上思考内容，并计入 `NPC_BUDGET_HISTORY` 预算；非 DeepSeek 端点不回传这个字段。

## 游戏面 API（mod 契约）

| 端点 | 用途 |
|---|---|
| `POST /api/capabilities` | mod 报到：声明动作白名单 + 执行接口（后续请求带 `mod` 即刷新心跳） |
| `POST /api/talk` | 对话，SSE 流式输出 |
| `POST /api/action_result` | 长动作结果回报（写记忆卡，可选通道） |
| `POST /api/tts` | 语音合成（独立输出通道） |
| `GET /api/state`、`GET /api/npcs` | 状态查询（调试） |

### `POST /api/capabilities`

```json
{
  "mod": "sims4",
  "execute_url": "http://127.0.0.1:8766/execute",
  "actions": [
    {"name": "cook", "desc": "做一道菜", "params": {"dish": "菜名"}}
  ]
}
```

`mod`、`actions[].name`、`actions[].desc` 必填；`execute_url` 可选（**没给 = 该 mod 没有可执行动作**，动作工具不进 LLM 工具定义）。成功返回 `{ok, mod, actions:[动作名]}`。

### `POST /api/talk`

```json
{"npc_id": "cang", "message": "在吗", "observation": {"time": "傍晚"}, "mod": "sims4", "voice": false}
```

- `npc_id`、`message` 必填且非空；`observation` 任意 JSON/字符串（进【当前情况】块）；`mod` 可选（带上即刷新心跳）；`voice` 可选（`true` 才尝试合成语音）。
- 成功返回 SSE（帧类型见上）；响应头含 `X-Mod-Heartbeat-Timeout`（当前心跳阈值秒数）。
- 同一 NPC 的回合串行（进程内 `asyncio.Lock`），并发请求会排队。

### `POST /api/action_result`

```json
{"npc_id": "cang", "action": "cook", "ok": true, "note": "面煮好了", "call_id": "…", "mod": "sims4"}
```

`npc_id`、`action`、`ok`（布尔）必填；`note`、`call_id`、`mod` 可选。返回 `{ok, npc_id, entry, matched}`；`entry` 为 `null` 表示没写卡。

### `POST /api/tts`

`{"text": "…", "voice": "zh-CN-XiaoxiaoNeural", "npc_id": "cang"}` → `{audio: "<base64 或空串>", voice}`；未装 `edge-tts` → 503。

### 状态查询

`GET /api/state` 返回模型名、LLM 三件套状态（key 只给掩码）、思考档位、TTS 是否可用、各 mod 在线状态与心跳阈值、运行时长等。`GET /api/npcs` 返回每个 NPC 的记忆条数、聊天消息数、最近活动时间。

控制台端点（角色卡 / 记忆 / 聊天记录 / 关系网 / 模型设置）是独立的调试面，不属于 mod 契约。

## 上下文分层与预算

`context.py` 按层装配（纯函数，可单测）：

1. **system 固定层**：人设 + 规矩（`build_system_prompt`），整场不变，利于前缀缓存
2. **历史**：已有摘要（一条 assistant 消息）+ 未被摘要覆盖的全部聊天记录；提示用到 `NPC_CONTEXT_WINDOW` 的 40%（`NPC_SUMMARY_RATIO`）时压缩旧轮次、只留最近 3 轮（`NPC_SUMMARY_KEEP_RECENT`）
3. **最后一条 user 消息**：资料块 + `玩家说：{原话}`，资料块顺序为
   - 【当前情况】：游戏观测（受 `NPC_BUDGET_STATE` 约束）
   - 【相关记忆】：回合开始的确定性预取（独享 `NPC_BUDGET_MEMORY`）
   - 【最近发生】：最近动作结果 / 进行中 / 待确认（与观测**共享** `NPC_BUDGET_STATE`，拿观测剩下的）

没有内容的块整块省略；超预算按文本截断并留 `…（已截断）` 标记，同时给每层记一条 `context_section_truncated` 日志，绝不静默丢弃。【最近发生】的砍序是「最近结果 → 进行中 → 待确认」（组内从较旧的砍起），待确认最后才砍——没收到回应的最容易被重复下令。

整个提示（系统 + 状态 + 记忆 + 历史 + 玩家这句）用到 `NPC_CONTEXT_WINDOW` 的 40%（`NPC_SUMMARY_RATIO`）时，回合结束后台把旧轮次压成滚动摘要（不挡首字），只留最近 3 轮逐字；摘要只保脉络，重要事实走记忆卡。

## 记忆：预取与检索

- 写入口只有三个：`remember` 工具（含 `replaces` 更新）、动作结果写卡、人工手改记忆卡（控制台也是走这条通道）。
- **回合开始预取**：条目按「置顶 → 重要度 → 新旧」的顺序塞进记忆预算（`NPC_BUDGET_MEMORY`），到预算为止，放不下就停——整条进或整条不进，不半截截断。排除 `category=action`（动作结果由近况层负责）。不做相关性打分、不判断与当前这句话是否相关；预取只读卡，不改任何字段。
- `recall` 工具保留，用于深挖：相关度低于门槛（`NPC_MEM_RECALL_MIN_REL`，默认 0.2）的条目不返回，全不相关返回空；排序 = 相关度为主、重要度为辅（`相关度 × 1.0 + (importance / 9) × 0.3`，同分 `created_at` 新的在前），同义词族取自该 NPC 角色卡的 `entity_synonyms`（按请求显式传入，多 NPC 并发不串）。
- **`replaces` 更新旧记忆**：【相关记忆】与 recall 结果的每行带条目 id 的 6 位短编号（方括号里那串）。同一件事换了说法时，`remember` 用 `replaces` 填短编号把旧条目原地更新（保留 id / 创建时间 / `pinned`），不重复记；`replaces` 没有恰好唯一匹配到条目就当新记忆记下，不报错。工具描述里同时约束：只记玩家明说的话和确实发生过的事，不记猜测、推断或评价。

## 回合 trace

每回合结束输出一条 `turn_trace` 结构化日志（降级回合、断连兜底也发，只发一条）：

| 字段 | 含义 |
|---|---|
| `npc_id` | NPC id |
| `rounds` | 本回合实际发起的 LLM 轮数 |
| `llm_fallback` | 是否走了角色卡 `rules` 兜底 |
| `summarized` | 本回合是否启动了后台摘要（不代表已跑完） |
| `no_dialogue` | 是否一句台词都没产出 |
| `tools` | 每次工具调用 `{name, kind, ok, status, ms}` |
| `tokens` | `{system, history, state, memory, user, total}`（本地估算口径） |
| `summary_trigger` | `{prompt_tokens, limit}`：本回合提示 token 估算 vs 压缩阈值（窗口 × 比例），方便对照真实对话判断阈值是否合适 |
| `memory_prefetch` | `{hits, filtered}`：进块条数 / 被滤条数 |
| `first_token_ms` / `total_ms` | 首帧耗时 / 整回合耗时 |
| `message_len` / `reply_len` | 玩家输入与台词长度（**不记原文**） |

设置 `NPC_TRACE_FILE` 时同一条记录再以 JSONL 追加写入该文件；写失败只告警，不影响回合。

## 角色卡

一张角色卡 = 一个 NPC = `personas/{id}.json` 一个文件，加文件即加角色，改完存盘即生效，不用重启。字段（身份、性格、说话风格、语气锚点、忌讳、兜底台词、同义词族、音色、关系网）见 [personas/_角色卡说明.md](personas/_角色卡说明.md)，空白模板在 `personas/_模板.json`。文件名即游戏侧认的 `npc_id`；以 `_` 开头的文件是模板/说明，不算 NPC。

## 测试

```bash
pytest
```

覆盖回合循环、上下文分层与预算、记忆检索与 replaces 更新、记忆预取、滚动摘要、近况层、动作去重、pending / call_id 对账、回合 trace、控制台 API 等。

手测脚本（真实 HTTP、可打印可直接贴回来的 JSON 汇总）：

```bash
python scripts/manual_checks.py llm-down --runs 10    # 不配 LLM 的首帧延迟
python scripts/manual_checks.py disconnect            # 中途断开 SSE 后的服务端状态
python scripts/manual_checks.py uncertain --runs 3    # mock Mod 超时 → 不确定（需要真实 key）
```

已跑过的结果与填法见本地 `docs/manual_checks.md`（内部记录，不进公开仓库）。

## 目录结构

```
server.py       HTTP 服务：mod 契约端点 + 应用装配
turn.py         回合主体：LLM ↔ 工具多轮循环（产出 SSE 帧 + 回合 trace）
context.py      上下文分层装配（纯函数，可单测）
memory.py       记忆卡：条目 + 相关度检索
chatlog.py      持久聊天记录 + 滚动摘要
tools.py        remember / recall + 动作工具定义（白名单闸门）
pending.py      待定动作登记（长动作回报对账）
tts.py          语音合成
console_api.py  控制台端点（调试面）
core/           LLM 接入（OpenAI 兼容）、配置、角色卡编译、日志
personas/       角色卡（每 NPC 一个 JSON）
web/console/    本地控制台（零构建：index.html + app.js + styles.css）
scripts/        手测脚本（manual_checks.py）
docs/           手测记录等文档
tests/          pytest 测试
store/          运行时数据（聊天记录、记忆卡、摘要）—— 不进仓库
```

## License

[MIT](LICENSE)
