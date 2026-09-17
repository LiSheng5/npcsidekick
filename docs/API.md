# API 参考 — NPCSidekick 端点契约

服务器监听 `http://127.0.0.1:8765`。游戏端只需要 `POST` 和 `GET`，全部返回 JSON（UTF-8）。

- **鉴权**：无 token。仅校验 `Origin` 必须来自 `127.0.0.1` / `localhost`（DNS-rebind 防护）。非本机 Origin → `403`
- **错误格式**：`{"detail": "..."}`；代码里实际用到的状态码：400（参数错）/ 403（Origin 拒绝）/
  404（没有该 NPC）/ 409（协议面冲突，如消费者重复报到）/ 422（校验失败）/ 429（限流）/ 500·503（内部故障）
- **编码**：请求体必须 UTF-8 JSON；中文直接传（`{"npc_id":"cang","message":"给我两根木材"}`）

---

## 1. `POST /api/talk` — 玩家对话（含指令）

| 项 | 值 |
|---|---|
| 请求体 | `{"npc_id": "cang", "message": "给我两根木材"}` |
| 响应 | `{"reply": "好，我这就去弄2个木材给你。"}` |
| 错误 | 400（message 为空）· 404（无此 npc_id） |

**行为**：
- 普通聊天 → LLM 生成回复（无 key 时规则话术兜底）
- **对话下指令**（"给我N个X"）→ 规则快路径接单，NPC **真的去执行**（采集→运回→交付），回复毫秒级
- 资源不足 → 诚实拒绝（"……木材现在弄不到了，采空了，等它长回来吧。"）
- 问过去的事（"你昨天干嘛了"）→ 记忆卡逐字回答（防幻觉快路径）

**游戏侧关键**：`message` 存入 `DialogueUI.last_messages`（需求感知），NPC 交付时按它判断"进背包还是进仓库"。

---

## 2. `GET /api/state` — 世界状态（游戏镜像轮询用）

| 项 | 值 |
|---|---|
| 响应 | 见下 |

```json
{
  "actors": {
    "cang": {
      "position": "森林",
      "inventory": {"木材": 1, "浆果": 0},
      "state": "working",
      "activity": "采集木材×2（剩 3 步）"
    },
    "ali": {"position": "村庄", "inventory": {}, "state": "resting", "activity": "休息3刻（剩 1 步）"}
  },
  "delivered": {"木材": 1173, "浆果": 1067, "石头": 0},
  "tick": 24036,
  "log_tail": ["cang 说: 柴火要挑干透的。", "ali 将 木材 交给了 主角"]
}
```

| 字段 | 说明 |
|---|---|
| `actors.<id>.state` | `idle` / `walking` / `working` / `resting` — 游戏按它表演 |
| `actors.<id>.activity` | 当前进行中的日常描述（空串 = 没事干） |
| `actors.<id>.position` | 大脑世界地点名（村庄/森林/河边/矿洞…） |
| `delivered` | 各资源**累计交付数**（从不清零） |
| `tick` | 世界帧计数（每 3 秒 +1） |
| `log_tail` | 最近 10 条事件（气泡差分用） |

**轮询建议**：2 秒一次（大脑 tick 3 秒）。**交付检测用 `delivered` 增量，不要用 log 文本去重**（同文本行会重复出现）。

---

## 3. `POST /api/task` — 直接派活（跳过对话）

| 项 | 值 |
|---|---|
| 请求体 | `{"npc_id": "cang", "resource": "木材", "count": 2}` |
| 响应 | `{"ok": true, "steps": ["gather(木材) → ✓ ...", "deliver(木材) → ✓ ..."]}` |
| 错误 | 404（无此 npc_id）· ok=false 表示失败（资源耗尽等，steps 里有原因） |

**注意**：`delivered` 是累计值——交付判定按"本次任务开始时的基线增量"计算，重复派发不会空手成功。

---

## 4. `GET /api/npcs` — 角色列表

```json
{"npcs": [{"id": "cang", "name": "苍"}, {"id": "ali", "name": "阿黎"}]}
```

## 5. `GET /api/npc?npc_id=cang` — 角色信息

```json
{
  "id": "cang", "name": "苍", "role": "部落的老猎手……",
  "personality": "沉稳、话少但靠谱",
  "speech": "话不多，句句落地",
  "taboos": "浪费食物、丢下同伴",
  "intro": "你走进了苍所在的地方。"
}
```

## 6. `GET /api/memory?npc_id=cang` — 记忆卡内容

```json
{"entries": [{"content": "完成：采集木材×2", "importance": 5, "category": "general"}]}
```

## 7. `GET/POST /api/mode` — 对话模式切换

- `GET` → `{"mode": "llm" | "rules", "requested": "llm"}`（`mode` = 实际生效，`requested` = 期望）
- `POST` body `{"mode": "rules"}` → 切到规则模式（零 LLM，全确定性，对话变呆但免费秒回）

## 8. `POST /api/tick` — 手动推一帧（测试/演示用）

body 可选 `{"seed": 42}` 保证确定性。响应 `{"tick": N, "events": {}}`。

---

## 接入模式速查（Godot 参考实现见游戏项目 `scripts/npc/Villager.gd`）

| 需求 | 调用 |
|---|---|
| 玩家说话/下指令 | `POST /api/talk` |
| 镜像 NPC 位置/状态（每 2s） | `GET /api/state` |
| 头顶气泡（新事件） | `log_tail` 差分：`"<id> 说: "` 开头的新行 |
| 交付动画（新到货） | `delivered` 计数增量（每 +1 触发一次） |
| 派活（旁路对话） | `POST /api/task` |
| 游戏启动自检 | `GET /api/state` 200 即在线，超时 3s 判定离线 → 拉起服务器 |

---

## 9. 完整端点索引（42 条 · 2026-09-17 实测）

> §1~§8 详述的是**游戏接入面**（最少三条：`/api/talk` `/api/state` `/api/task`）。
> **Console 面 14 条**属于开发者工具层，只服务本机 Web Console（`/console/`），游戏端不需要。
> **权威源 = 代码**：`npc/server.py`（26 条）+ `npc/console_api.py`（14 条）—— 本表与代码不一致时，以代码为准。

### 9.1 游戏面（`npc/server.py`，26 条）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/talk` | 玩家对话/下指令（详见 §1） |
| GET | `/api/state` | 世界状态镜像（详见 §2） |
| POST | `/api/task` | 直接派活，跳过对话（详见 §3） |
| GET | `/api/npcs` | NPC 列表（前端切换角色用） |
| GET | `/api/npc?npc_id=` | 单个人设（详见 §5） |
| GET | `/api/memory?npc_id=` | 记忆卡内容（详见 §6） |
| GET | `/api/memory/report` | 记忆体检报告（纯规则零 LLM，跨 NPC 全局体检；`top_n` 夹 1..100，非法值回落 10） |
| GET·POST | `/api/mode` | 对话模式：规则 ↔ LLM（详见 §7） |
| POST | `/api/tick` | 手动推一帧（测试/演示；body 可选 `seed`）（详见 §8） |
| POST | `/api/npc/register` | 动态注册（GTA 前置）：ped 随刷随出热注册，幂等（重复 → `existed`） |
| POST | `/api/npc/unregister` | 反注册（ped despawn）：移除 NPC + 世界槽；常驻层先落盘 |
| GET | `/api/personas` | 人设清单（全量 dict 列表） |
| POST | `/api/personas` | 新建人设 → 校验 → 写 `<personas>/<id>.json` |
| GET | `/api/events` | 结构化事件流增量（`since` 游标；**事件无时间戳，只有到达顺序**） |
| GET | `/api/events/stream` | SSE 增量事件流（轮询的升级替代，按需采用） |
| POST | `/api/memory` | 记忆回写（整表替换语义，生产客户端勿用） |
| GET | `/api/npcs/{pid}/memory-journal` | 该 NPC 整理审计流水（`{id}_report.jsonl`，读尾部 N 条；坏行跳过不 500；无文件返回 []；pid 不存在 404） |
| POST | `/api/consumer/hello` | 协议 v1 · 能力协商（M1）：消费者报到 + 心跳 + 声明可执行动词表 |
| POST | `/api/task_done` | 协议 v1 · 销账（M1）：mod 干完活回报 |
| GET | `/api/version` | 版本/特性握手：客户端启动探测一次，按特性降级 |
| GET | `/api/stats` | 观测端点：调用量/延迟/错误/SSE 连接数 |
| POST | `/api/tts` | 语音合成（可给任意文本配音） |
| GET·POST | `/api/approval` | 审批策略视图/运行时调整（带 `npc_id` = 按 NPC 粒度，缺省 = 全局） |
| GET·POST | `/api/manifest` | 动作清单：读取 / 游戏声明自己的动作（`{reset:true}` 恢复默认） |
| GET | `/` | 根路径 → Web Console `/console/`（dist 未构建时返回提示 JSON） |

### 9.2 Console 面（`npc/console_api.py`，14 条）

> 只服务本机 Web Console。本项目里**"删除"= 移进同级 `.trash/`**（可反悔），不是真删。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET·PUT·DELETE | `/api/personas/{pid}` | 单个人设读/改/删（改 = 校验 → 写盘 → **热加载进运行时**；删 = 文件进 `.trash` + 摘实例，记忆卡默认**保留**） |
| GET | `/api/actions` | 动作名建议（**不是白名单**，只是编辑器的输入提示） |
| GET | `/api/relationships` | 关系图数据（无数据 → 空 + `source="none"`，不造假） |
| GET·POST | `/api/npcs/{pid}/memory` | 单 NPC 记忆列表 / 新增（结果三态 `added` / `merged` / `rejected`） |
| PUT·DELETE | `/api/npcs/{pid}/memory/{mid}` | 单条记忆改 / 删 |
| GET·POST | `/api/settings/providers` | Provider 配置读写（密钥存 `npc/config/providers.enc`，只回 masked） |
| DELETE | `/api/settings/providers/{pid}` | 删 Provider |
| POST | `/api/settings/providers/{pid}/activate` | 激活指定 Provider（响应 `{"ok", "active", "providers"}`） |
| POST | `/api/llm/test` | 拨测：拿 `provider_id`（或临时 base_url/key/model）发一个最小请求 |
