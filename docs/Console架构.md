# Web Console 架构

Console 是 Runtime 的**开发者工具层**：观察世界状态、编辑人设与记忆、试对话、配 Provider。
它与游戏端、CLI **并列消费同一个 Runtime**，不参与游戏接入协议 —— 核心关系不变：

```
Game ──HTTP──▶ NPCSidekick Runtime ◀──HTTP── Web Console
```

> 定位一句话：**给做游戏角色的人用的调试台**，不是给玩家用的界面。

---

## 1. 技术栈与运行方式

| 项 | 选型 |
|---|---|
| 技术栈 | Vite + React 18 + TypeScript |
| 关系图 | `@xyflow/react`（React Flow v12） |
| 构建 | `tsc --noEmit && vite build`（**类型校验是构建的一部分**） |

**开发**（`npm run dev`，5173）：`vite.config.ts` 把 `/api` 代理到 `http://127.0.0.1:8765`。
**生产**：`npm run build` 产出 `web/console/dist/`，由 `npc/server.py` 挂到 `/console/`
（`dist` 不进 Git，`npm run build` 产出）—— 从而保住"一条命令启动后打开
`127.0.0.1:8765` 就能用"。

---

## 2. 页面

7 个导航项（`src/App.tsx`）：

| 页面 | 职责 | 主要数据源 |
|---|---|---|
| **Graph**（首页） | 关系图；点节点出侧栏 Inspector | `/api/relationships`（抽象接口，空态要**明示"Runtime 未提供关系数据"**，不造假） |
| **Characters** | 人设列表 / 新建 / 编辑（`CharacterEditor` 子页：Personality、Routine、Rules） | `/api/npcs`、`/api/personas`（CRUD + 热加载） |
| **Live** | 运行中世界的实时观察：状态卡（位置/耐力/背包）+ 事件流 | `/api/state`（2s 轮询）、`/api/events/stream`（SSE） |
| **Memory** | 记忆条目的增删改 + 检索预览 + **记忆体检** | `/api/memory`、`/api/memory/report`、`/api/npcs/{pid}/memory-journal` |
| **Activity** | 事件历史翻阅与检索 | `/api/events`（分页） |
| **Playground** | 试一句话看怎么答 + 本轮调试信息（模式/延迟/思考/召回预览） | `/api/talk`、`/api/mode` |
| **Settings** | Provider 配置（OpenAI-compatible）+ Test Connection + 开关生效态 | `/api/settings/providers`、`/api/version` |

组件在 `src/components/`（`RelationshipGraph` / `NpcNode` / `NpcInspector` / `CharacterEditor`
/ `RoutineEditor` / `MemoryHealth` / `EventLine` / `StateBlock`），客户端在 `src/api/client.ts`，
类型在 `src/types.ts`，样式在 `src/styles.css`（纯 CSS + CSS 变量，无 UI 框架）。

---

## 3. 数据模型

| 模型 | 形状 | 唯一真相 |
|---|---|---|
| **Persona** | 必需 `id, identity, personality, speech_style, taboos[]`；可选 `name, desires{}, goals{}, rules{replies,fallback}, routine[], system_prompt_override, context_extra, voice_samples[]` | `npc/personas/*.json` —— **启动时覆盖记忆卡里的 persona 快照** |
| **Memory 条目** | `{id, content, importance 0-9, category, created_at, mtype?}` | 记忆卡 `npc/store/<id>_memory.json`（已 gitignore） |
| **World** | `{_tick, actors{id:{position,inventory,stamina}}, protagonist, locations{}, delivered{}, log[]}` | 见 `docs/世界声明键.md` |
| **Event** | `world.log` 文本行 → `events_archive.parse_log_line` 结构化；游标 = 绝对流位置（跨冷层归档恒有效） | — |

⚠️ **NPC 用的是 `npc/memory.py::NPCMemory`**，不是 `agent/memory/` 的 `MemoryManager`
（后者属通用 Agent 侧，NPC 不走）。

---

## 4. 红线（改 Console 时必须守）

| # | 红线 | 理由 |
|---|---|---|
| 1 | **只加字段，不改语义**（`/api/npcs` 等既有端点） | 游戏端也在消费同一批端点 |
| 2 | **UI 零游戏名词** | `state` 取值、`routine[].action`、关系类型、事件类型**全部按 API 返回值渲染**，不写死 |
| 3 | **编辑必须走后端既有写通道**（`NPC.remember()` / `npc.memory.*` → `npc.save()`） | 绕过它另写一套 = 违规 |
| 4 | **API Key 只回 masked**；`npc/config/` 与 `*.enc` 不进 Git、不入 persona JSON、不入前端 | 密钥泄漏 |
| 5 | **不造假**：数据没有就显示空态并说明原因 | 项目通用底线 |
| 6 | 删除动作 = `os.replace` 移入同级 `.trash/` | 本机 safe-delete 会 fail-closed 拦截真删除；顺带可反悔 |
| 7 | 热加载人设时同步清 `world["actors"]` 孤儿槽、并清 `_llm` / `_llm_review` 缓存 | 否则 tick 循环 KeyError / 换模型后缓存不失效 |
| 8 | `panels` 白名单要一致：后端 `_hud.panels` 声明什么，前端就用 `shown(key)` 判什么 | 纯对话世界声明 `["position","activity"]` 时不该漏出其它面板 |

---

## 5. 目录

```
web/console/
  src/pages/        GraphPage CharactersPage CharacterEditor LivePage
                    MemoryPage ActivityPage PlaygroundPage SettingsPage
  src/components/   RelationshipGraph NpcNode NpcInspector RoutineEditor
                    MemoryHealth EventLine StateBlock
  src/api/client.ts Console 只跟一个后端说话
  src/types.ts      与后端响应对应的类型（改后端响应记得同步）
  src/lib/          fmtInt / fmtPct / isoTime / relativeTime / stateColor…
  vite.config.ts    dev 代理 /api → 127.0.0.1:8765
  dist/             build 产物（gitignore；由 Runtime 挂到 /console/）
```

---

## 6. 相关文档

- 端点全量清单：`docs/API.md` §9
- 世界声明字段（`_hud` 等）：`docs/世界声明键.md`
- 游戏接入（Console 之外的正事）：`docs/游戏接入.md`、`engine-clients/common/PROTOCOL.md`
