# API 参考 — NPCSidekick 端点契约

服务器监听 `http://127.0.0.1:8765`。游戏端只需要 `POST` 和 `GET`，全部返回 JSON（UTF-8）。

- **鉴权**：无 token。仅校验 `Origin` 必须来自 `127.0.0.1` / `localhost`（DNS-rebind 防护）。非本机 Origin → `403`
- **错误格式**：`{"detail": "..."}`，HTTP 状态码 400（参数错）/ 404（没有该 NPC）/ 403（Origin 拒绝）
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
