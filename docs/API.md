# API（字段级契约）

大脑（本地 Python 服务器）对外只有四个端点，前两个 + 你自己的执行接口是 mod 必须实现/调用的，
第四个只有长动作才用。**所有改动都是加法**：新增字段全部可选，不认识新字段的旧 mod 照常工作。

| # | 端点 | 方向 | 谁调 |
|---|---|---|---|
| 1 | `POST /api/capabilities` | mod → 大脑 | 报到：声明能力白名单 + 执行接口 + 心跳 |
| 2 | `POST /api/talk` | 游戏侧 → 大脑 | 玩家说话（SSE 流） |
| 3 | `POST <execute_url>` | 大脑 → mod | 同步执行一个动作，**做完才回响应** |
| 4 | `POST /api/action_result` | mod → 大脑 | 可选：长动作异步回报 |

调试端点（`/api/state`、`/api/npcs`、`/api/tts`、控制台 `/console/`）**不属于 mod 契约**，mod 不需要调用。

---

## 1. `POST /api/capabilities` —— 报到 + 心跳

声明能做什么 = **白名单唯一闸门**：只有这里声明过的动作才会变成 LLM 的工具定义，模型不可能调用未声明的动作。

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `mod` | string | ✅ | mod 名（非空）。之后 `/api/talk`、`/execute`、`/api/action_result` 都用它对齐 |
| `actions` | array | ✅ | 非空数组，每项 `{name, desc, params}` |
| `actions[].name` | string | ✅ | 动作名（工具名）。`remember` / `recall` 是保留字：声明了也不会变成工具 |
| `actions[].desc` | string | ✅ | 给 LLM 看的一句话说明 |
| `actions[].params` | object | | `{参数名: 说明}`；参数一律按 **string** 进工具定义；缺省 = 无参 |
| `execute_url` | string | | 执行接口地址（见 §3）。**不填 = 该 mod 没有可执行的动作**，动作工具不进 LLM 工具定义 |

```bash
curl -X POST http://127.0.0.1:8765/api/capabilities -H "Content-Type: application/json" -d '{
  "mod": "mygame",
  "execute_url": "http://127.0.0.1:8766/execute",
  "actions": [
    {"name": "cook", "desc": "用厨房做饭", "params": {"dish": "菜名(字符串)"}},
    {"name": "goto", "desc": "走到某地",   "params": {"place": "地点名(字符串)"}}
  ]
}'
```

**响应 `200`**：`{"ok": true, "mod": "mygame", "actions": ["cook", "goto"]}`

**心跳**：报到后每次报到、每次 `/api/talk` 里捎带 `mod`、每次 `/api/action_result` 里捎带 `mod`
都算一次心跳。超过 `NPC_MOD_HEARTBEAT_TIMEOUT`（默认 60 秒）没心跳 = 该 mod 离线 →
大脑不再调它的任何动作（动作工具从工具定义里消失）。

**错误**：请求体不是 JSON 对象 / `mod` 缺失 / `actions` 空或不是数组 / 某一项缺 `name`、`desc`
/ `params` 不是对象 / `execute_url` 不是字符串 → `400`，响应体 `{"detail": "..."}`。
`GET /api/state` 的 `mods.<mod>` 能看到 `online` / `actions` / `execute_url` / `last_seen_s_ago`。

---

## 2. `POST /api/talk` —— 对话（SSE）

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `npc_id` | string | ✅ | 对应 `personas/{npc_id}.json` |
| `message` | string | ✅ | 玩家这一句 |
| `observation` | any | | 游戏侧观察（`{"summary": "...", "nearby": [...]}` 之类），进本轮上下文的「当前情况」块（用户消息前缀，受 `NPC_BUDGET_STATE` 预算约束） |
| `mod` | string | | 捎带即刷新该 mod 的心跳 |
| `voice` | bool | | `true` 才出 `audio` 帧（需装 edge-tts） |

**响应**：`200`，`Content-Type: text/event-stream`，帧格式 `data: {JSON}\n\n`：

| 帧 `type` | 字段 | 说明 |
|---|---|---|
| `delta` | `text` | 台词增量，只有角色说的话（逐字流） |
| `action` | `action: {name, params, ok, status, call_id}` | 流末尾每个"本回合已执行的动作"一帧（记录/展示用，**mod 不需要照它做任何事**） |
| `audio` | `audio`(base64) / `voice` | 仅 `voice: true` 且语音可用时，在 `done` 之前 |
| `done` | — | 流结束 |

- `action.ok`：`true` = 做成，`false` = 没做成 / 还没定（配合 `status` 看）。
- `action.status`：`done`（做完了）/ `running`（mod 说在做）/ `unknown`（大脑同步等待超时，不知道做没做）
  / `failed`（请求没到 mod 或响应非法）。
- `action.call_id`：大脑为这次调用生成的关联 id（32 位 hex），mod 回报时原样带回（见 §3、§4）。
- 响应头 `X-Mod-Heartbeat-Timeout` = 当前心跳超时秒数（调试用）。

**错误**：`npc_id` / `message` 缺失或空白、角色卡缺失或损坏 → `400`。

---

## 3. `POST <execute_url>` —— 大脑同步执行动作（mod 实现）

大脑在 `LLM ↔ 工具` 循环里调它，**做完才回响应**，结果当场写记忆卡。

**请求（大脑 → mod）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `npc_id` | string | 哪个 NPC 要做的 |
| `action` | string | 动作名（= 工具名） |
| `params` | object | 参数，值是字符串（按 `actions[].params` 的声明） |
| `mod` | string/null | 调用方（`/api/talk` 里带的 mod），没带为 `null` |
| `call_id` | string | **新增**：本次调用的关联 id（32 位 hex）。长动作回报时原样带回 |

**响应（mod → 大脑）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | bool | 做成了 `true` / 没做成 `false`（**失败必须用 `ok=false` 表达**） |
| `status` | string | `done`（默认，做完了）/ `running`（还在做，稍后回报）。**其他值一律按 `done` 处理** |
| `note` | string | 一句话结果（进记忆卡与给 LLM 的工具消息）。成功时可省略 |

**大脑侧语义**

| 情况 | 大脑怎么处理 |
|---|---|
| `status: done` | 把结果告诉 LLM 并**当场写卡**：`完成: <note>` / `没做成: <note 或 action>`；`action` 帧带 `ok/status` |
| `status: running` | **不写卡**，把这次调用按 `call_id` 登记为待定，等 §4 的回报 |
| 同步等待超时（`NPC_EXECUTE_TIMEOUT`） | 记 `status: unknown`：**不写卡**（mod 可能已经做了），同样登记为待定；LLM 被告知"没等到回应，不确定是否已经做成" |
| 连不上 / 响应不是 JSON 对象 | 记 `status: failed`：**当场写失败卡**（说明请求大概率没到 mod） |
| Mod 一直没回报 | 到 `NPC_PENDING_TTL` 写一条 `没做成: 一直没等到结果`（见 §4） |

---

## 4. `POST /api/action_result` —— 长动作异步回报（可选）

只有 `status: "running"`（或大脑超时未定）的动作才需要它；同步做完的动作大脑已经当场写卡。

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `npc_id` | string | ✅ | |
| `action` | string | ✅ | 动作名 |
| `ok` | bool | ✅ | 做成了 / 没做成 |
| `note` | string | | 一句话结果；空则用 `action` 名（命中登记时用登记的 `params` 补一句描述） |
| `call_id` | string | | **新增**：`/execute` 里拿到的那一个，原样带回 |
| `mod` | string | | 捎带即刷新该 mod 的心跳 |

**匹配规则（大脑侧）**

1. 带 `call_id` 且命中登记 → 销账并写卡，响应 `matched: true`。
2. 带 `call_id` 但**没命中**（重复回报 / 已过期 / 未知）→ 记日志、**不写卡**（同一次动作不能写两遍），
   仍返回 `200`，`matched: false`、`entry: null`。
3. **不带 `call_id`**（旧 mod）→ 先按 `(npc_id, action)` 销掉**最早**一条登记（FIFO）；
   **无论是否命中都照旧写卡** —— 旧 mod 的回报不能丢，此时 `matched` 只表示"是否销掉了登记"。

**响应 `200`**：原有字段一个不动，只多 `matched`：

```json
{"ok": true, "npc_id": "cang", "entry": {"id": "...", "content": "完成: 面煮好了", "importance": 6, "category": "action", "created_at": 1790260168.1}, "matched": true}
```

**错误**：`npc_id` / `action` 缺失、`ok` 不是布尔、`note` 不是字符串、`call_id` 不是字符串 → `400`。

```bash
# 长动作做完，mod 带 call_id 回报
curl -X POST http://127.0.0.1:8765/api/action_result -H "Content-Type: application/json" -d '{
  "npc_id": "cang", "action": "cook", "ok": true, "note": "面煮好了",
  "call_id": "9f2c8b1e4d5a47c3b0e6f7a8c9d0e1f2", "mod": "mygame"
}'
```

---

## 5. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NPC_MOD_HEARTBEAT_TIMEOUT` | `60` | mod 心跳超时（秒）：超时视离线，动作工具不再进 LLM 工具定义 |
| `NPC_EXECUTE_TIMEOUT` | `30` | 同步执行动作的等待上限（秒）：超时记 `unknown`，**不写失败卡**，转待定等回报 |
| `NPC_PENDING_TTL` | `120` | 待定动作存活上限（秒）：到点还没等到回报 → 写 `没做成: 一直没等到结果` |

三个都**现读可热切**（每次用的时候读环境变量，改完不用重启）；非法值回默认。
