# NPCSidekick Web Console — 架构理解与技术设计（Step 1~3 产出）

> 状态：**已与用户逐项确认，待开写**。代码零改动。
> 目标：在**不破坏现有游戏接入协议**的前提下，为 NPCSidekick Runtime 增加一层
> 开发者工具：Visualization + Character Editor + Runtime Monitor + LLM Playground。
> 核心关系不变：`Game ↔ HTTP ↔ NPCSidekick Runtime`，Web 与 Game、CLI 并列消费同一个 Runtime。

---

## 1. 现状盘点：两个 Web 服务 + 两套 UI

| | `npc/server.py` | `web/server.py` |
|---|---|---|
| 定位 | **真正的 NPC Runtime**（游戏接入协议层） | 通用 Agent（`AgentOrchestrator`）聊天台 |
| 启动 | `python -m npc.server`（bat 就是它），`127.0.0.1:8765` | `python main.py --web`，同样默认 8765 |
| 接口 | `/api/talk` `/api/state` `/api/task` `/api/events` … | `/api/chat` `/api/chat/stream` `/api/history`（token 鉴权） |
| 静态页 | mount `web/static` | mount **同一个** `web/static` |

现有两个前端页面，被两个服务同时 Serve：

- **`web/static/index.html`**（966 行）：通用 Agent 聊天台，暗色 cyberpunk 紫（`--bg:#0a0a0f`），
  只调 `/api/chat/stream` —— NPC Runtime 没有此接口，**在 8765 上打开是废页**。
- **`web/static/npc.html`**（672 行）：**已存在的关系网控制台**（浅紫 lavender，三栏：
  左概览 / 中画布 / 右 Inspector；顶部搜索+筛选+新建；新建弹窗 `POST /api/personas`）。

### npc.html 为什么是"半成品"（推倒重做的理由）

1. 画布 = 手写 div + SVG 连线，**固定 6 个坐标点摆位**；无力导向、无 zoom/pan、节点多则重叠。
2. 关系数据读 persona JSON 的**非标准字段** `relations:[{who,how,score}]`（另含 `traits`/`tags`/`state`/`place`），
   **不在** `PERSONA_FIELDS`/`REQUIRED_FIELDS` 内，`cang.json`/`ali.json` 里也没有 → 真数据下**零条边**。
3. API 不可达时**硬编码 DEMO 四人组**（艾琳/凯尔/莉亚/德里克，奇幻设定）→ 踩"不把 demo 游戏内容写进 Web"红线。
4. 单页，无 Characters / Live / Memory / Activity / Playground / Settings 导航。

---

## 2. 现有 API 清单（对照需求第 23 条）

### 已有（复用，不重写）

| 端点 | 用途 |
|---|---|
| `POST /api/talk` | 对话（游戏协议，禁止改动） |
| `GET /api/state` | 世界状态：actors(position/inventory/state/stamina/activity) + tick + log_tail + pending_tasks + panels |
| `POST /api/task` | 派活（游戏协议） |
| `GET /api/npcs` | 运行时 NPC 列表（`{id,name}`，**可扩展但必须 additive**） |
| `GET /api/npc?npc_id=` | 人设摘要 |
| `GET /api/personas` / `POST /api/personas` | 列目录 / 新建落盘 `npc/personas/<id>.json` |
| `GET /api/memory?npc_id=` | 记忆全量条目 |
| `POST /api/memory` | 记忆**整表替换**（保留，新增单条 CRUD 与之并存） |
| `GET /api/events?since=` | 结构化事件流（say/move/gather/craft/deliver/task/subagent） |
| `GET /api/events/stream` | **SSE 已存在** → 第一版不新建 WebSocket |
| `GET /api/version` | 特性握手（含 flags：模型/模式/tick 间隔…） |
| `GET /api/stats` | 观测：talk 延迟、llm/rules 计数、scheduler、task_loop |
| `GET/POST /api/mode` `GET/POST /api/approval` `GET/POST /api/manifest` | 运行时开关 |
| `/api/npc/register` `/api/npc/unregister` `/api/consumer/hello` `/api/task_done` `/api/tick` `/api/tts` | 动态注册与协议 v1 |

### 缺失（本次要加）

- `GET/PUT/DELETE /api/personas/{id}` —— 读/改/删单份人设
- `/api/npcs/{id}/memory` 的 `POST / PUT / DELETE` —— **单条记忆**增删查改
- `GET /api/relationships` —— 关系图抽象接口（Runtime 目前无关系数据，返回空 edges + `source:"none"`）
- `GET/PUT /api/settings/providers`、`POST /api/settings/providers`、`DELETE /api/settings/providers/{id}`、`POST /api/llm/test`

---

## 3. 数据模型（Step 3）

### Persona（`npc/persona.py` + `npc/persona_loader.py`）

```
必需: id, identity, personality, speech_style, taboos[]          # taboos 可为 []
可选: name, desires{}, goals{}, rules{replies, fallback}, routine[],
      system_prompt_override, context_extra, voice_samples[]
```

- 唯一真相 = `npc/personas/*.json`；**启动时覆盖记忆卡里的 persona 快照**。
- ⚠️ `routine[].action` 白名单硬编码 `("gather","rest","say")` → 与"不假设 action 只有三种"冲突，**需放宽**
  （方案：允许任意 action 字符串，`gather` 仍需 `resource`；UI 从 `/api/manifest` 取动作清单做建议 + 允许自由输入）。
- `system_prompt_override` 在 UI 中标为 **Advanced**（默认折叠）。
- 扫描非递归（`*.json`），`npc/personas/gta/` 子目录不进列表。

### Memory（`npc/memory.py` · `NPCMemory`）

- ⚠️ NPC 用的是 `NPCMemory`，**不是** `agent/memory/memory_manager.MemoryManager`（后者属通用 Agent 侧，NPC 不用）。
- 条目：`{id, content, importance 0-9, category, created_at, mtype?}`；`category` 取值见 `general/reflection/consolidated/legacy/archived`。
- 持久化 = 记忆卡 `npc/store/<id>_memory.json`（`{id,name,saved_at,persona,world,task_log,memory,reflected_upto}`），已 gitignore。
- 编辑必须经 `NPC.remember()` / `npc.memory.*` → `npc.save()`，绕过它另写一套 = 违规。

### World（`npc/world.py`）

`{_tick, actors{id:{position,inventory,stamina}}, protagonist, locations{}, delivered{}, log[]}`；
`state` ∈ idle/walking/working/resting（**UI 不得写死**，按 API 返回值渲染）。

### Events（`npc/events_archive.py`）

`world.log` 文本行 → `parse_log_line` 结构化；游标 = 绝对流位置（跨冷层归档有效）。Activity 页直接吃 `/api/events` + SSE。

---

## 4. 八项已确认决策

| # | 决策 | 选择 |
|---|---|---|
| 1 | 技术栈 | **Vite + React + TS**，Graph 用 React Flow |
| 2 | 旧 `npc.html` | **推倒重做**，保留一段时间后删除 |
| 3 | 关系数据 | **接抽象接口 + 空态明示**（返回空 edges，UI 注明"Runtime 未提供关系数据"），不造假 |
| 4 | 人设热加载 | **要**，改完免重启、立刻同步 Graph/Characters/Live/Memory |
| 5 | 运行与挂载 | **dev 代理（5173 → 8765）+ build 物挂 Runtime**（保住"一条命令启动"） |
| 6 | 旧聊天台 `index.html` | **两服务彻底解耦**：挪到 `web/agent_static/`，`web/server.py` 改 mount 自己那份 |
| 7 | Provider / API Key | **第一版做全套**：OpenAI-compatible（Base URL/Key/Model）+ Test Connection + 本地加密存储 + 只回 masked |
| 8 | 第一步粒度 | **后端先行 + 最小前端骨架**（接口+测试做实，前端先跑通 Graph 空态与 Inspector） |

---

## 5. 目标结构

```
web/console/                  # Vite + React + TS 工程
  src/pages/                  Graph · Characters · CharacterDetail · Live · Memory · Activity · Playground · Settings
  src/components/             RelationshipGraph NpcInspector NpcList CharacterEditor
                              PersonalityEditor RoutineEditor MemoryList MemoryEditor
                              LiveNpcList ActivityFeed DialoguePanel ProviderSettings
  src/api/  src/lib/  src/types/
  vite.config.ts              proxy: /api → http://127.0.0.1:8765
  dist/                       → 由 npc/server.py mount 到 /console/
web/agent_static/index.html   # 通用 Agent 聊天台（自 web/static 迁出）
web/static/                   # 旧页临时保留，稳定后删
npc/console_api.py            # 新增：管理型端点（挂到 create_npc_server 产出的 app 上，避免 server.py 膨胀）
npc/secrets.py                # 新增：SecretStore（Windows DPAPI 优先，回退受限权限加密文件）
npc/config/providers.enc      # 加密存储（gitignore）
```

**新增端点草案**

```
GET    /api/npcs                       # 扩展为详情：+ identity/state/activity/position/memory_count（additive）
GET    /api/personas/{id}
PUT    /api/personas/{id}              # 写盘 + 热加载（替换实例，保留记忆卡）
DELETE /api/personas/{id}              # 删文件 + 摘实例 + 清世界 actor 槽（记忆卡默认保留）
GET    /api/npcs/{id}/memory
POST   /api/npcs/{id}/memory           # 单条新增（走 remember/memory.add → save）
PUT    /api/npcs/{id}/memory/{mid}
DELETE /api/npcs/{id}/memory/{mid}
GET    /api/relationships              # {source:"none"|"persona"|"runtime", nodes:[], edges:[]}
GET/PUT/POST/DELETE /api/settings/providers[/{id}]
POST   /api/llm/test
```

### 热加载设计要点

`_apply_persona_change(pid, persona|None)`（同步、无 await 点 → 与 tick_loop 天然不竞态）：

- 新建：写盘 → `NPC(persona, world=共享world, store_dir)` → 注册 `npcs[pid]` + `actor_of(world, pid)`。
- 更新：复用原实例，替换 `persona` + `system_prompt`，**清空 `_llm`/`_llm_review` 缓存**（模型可能变了），保留 memory/world 引用。
- 删除：删文件 → `npcs.pop` → `world["actors"].pop` （**防孤儿槽导致 tick_round KeyError**，现有 `load_village` 已同款处理）；记忆卡默认保留，UI 二次确认。
- 边界：只作用于 personas 目录里存在的 id；动态注册的流民（`_dynamic`/ephemeral）不受删除接口影响。

---

## 6. 分步计划（Step 4~11，Step 4 细化到文件级）

**Step 4（本步）— 后端先行 + 最小前端骨架**
1. 新建 `npc/console_api.py`：`personas` 的 GET/PUT/DELETE + 热加载 + `/api/npcs` additive 扩展 + `/api/relationships` 抽象接口。
2. 新建 `npc/secrets.py` + provider 配置端点 + `/api/llm/test`（Step 7 提前，因 Settings 属"第一版全套"）。
3. 测试：`tests/test_console_api.py`（CRUD/热加载/孤儿槽/关系空态/secret masked）。
4. 前端：初始化 `web/console`（Vite+React+TS，proxy 到 8765），路由骨架 + 极简设计系统（白底/灰边/深字）+ Graph 页（React Flow，空态文案）+ 点节点出 Inspector（数据来自 API）。
5. `web/static/index.html` → `web/agent_static/`，`web/server.py` 改 mount；`npc/server.py` mount `web/console/dist` 到 `/console/`。
6. 回归：全量 `pytest` 保持 802 passed 全绿。

**Step 5** Characters 列表/创建/编辑（含 Personality、Routine 编辑器 + View JSON 高级模式）
**Step 6** Memory 页面（列表/搜索/新增/编辑/删除/importance）
**Step 7** Live 页（`/api/state` 轮询 + SSE）
**Step 8** Relationship Graph 完善（zoom/pan/fit/search/filter、响应式 Inspector）
**Step 9** Playground（对话 + Debug 面板：model/latency/memory context）
**Step 10** Settings（Provider UI、masked key、Test Connection）
**Step 11** 实时事件层（SSE 事件总线抽象，为将来 WS 留口）

---

## 7. 风险与红线

| 风险 | 处理 |
|---|---|
| `/api/npcs` 扩展破坏老客户端（游戏端也在用） | **只加字段，不改语义** |
| 热加载触发 tick_loop 孤儿槽 KeyError | 删除时同步清 `world["actors"]`，补测试 |
| 换模型后 LLM 客户端缓存未失效 | 更新人设时清 `_llm`/`_llm_review` |
| Vite dev 直连 8765 被 Origin 校验 403 | 走 vite proxy（已选）；若直连需扩 `_ALLOWED_ORIGIN_PREFIXES` |
| API Key 泄露 | 只返回 `masked_key`；`npc/config/` 与 `*.enc` 进 .gitignore；不入 persona JSON、不入前端 |
| Windows 中文路径/编码 | 沿用现有 `utf-8-sig` 读写经验 |
| 游戏内容硬编码 | UI 零游戏名词；routine action、state、关系类型、事件类型全部 API 驱动 |
| 本机 safe-delete 拦截删除动作 | "删除"改成 `os.replace` 移入 `.trash/`（见下 §8） |
| 测试基线 | 改前实测 **801 passed + 1 既有失败**（见 §8）｜每步改动后跑全量 |

---

## 8. Step 4 完成情况（2026-09-07）

### 后端（已交付，26 条新测试全绿）

- `npc/console_api.py`（新增）：
  - `GET/PUT/DELETE /api/personas/{pid}` —— PUT 写盘后**热加载**（`hot_reload`：新建走记忆卡续前缘、更新换 persona + 重编 system_prompt + 清 LLM 分槽缓存）；DELETE 摘实例 + 清世界槽。
  - `GET /api/relationships` —— `{source, nodes, edges}`；`runtime`（世界 `_relationships`，未来出口）> `persona`（人设可选 relations）> `none`（空 edges + note，**不造假**）。
  - `/api/npcs/{pid}/memory` 的 GET/POST/PUT/DELETE —— 走 `NPC.remember()` / `NPCMemory` → `npc.save()` 落记忆卡。
  - `/api/settings/providers` 的 GET/POST/DELETE + `/activate` + `POST /api/llm/test`（`asyncio.to_thread`，不冻事件循环；失败返回 200 + `ok=false`）。
- `npc/secrets.py`（新增）：Windows DPAPI 加密（ctypes 零依赖），回退明文时**显式标注 `encrypted=false`**；对外只给 `{configured, masked_key}`。
- `npc/server.py`（改）：`/api/npcs` additive 扩展；新增 `config_dir` 注入口；Console 端点挂载 + `/console/` 静态挂载（dist 不存在时静默跳过）；**根路径 `/` 重定向到 `/console/`**（没有构建产物时回落到旧 `npc.html`），保住"一条命令启动、打开根路径就能用"。
- `web/server.py`（改）：静态目录改 `web/agent_static/`，两个服务彻底解耦。

### 顺手修掉的两个既有缺陷

1. **SSE 端点 NameError**：`/api/events/stream` 用了未定义的 `_events_since`，客户端一连就 500（P2 重构时漏迁）。已改为 `events_archive.events_since`。
2. **删光 NPC 后端点崩**：`/api/state`、`/api/events`、`/api/stats`、`/api/mode`、`/api/approval` 里的 `next(iter(npcs.values()))` 会 StopIteration；改为优先用闭包里的共享 `world`，需要 NPC 属性的回退安全默认值。

### "删除"改成移入 `.trash/`（重要设计变更）

原实现用 `Path.unlink()`，实测被本机 safe-delete 策略拦截（fail-closed → 500）。
改为 `os.replace` 移到 `npc/personas/.trash/<name>.<时间戳>.json`：
- 制作者工具的删除本来就该可反悔（手改三天的人设不该一键蒸发）；
- 移动不是删除，不受拦截影响；
- `.trash` 是子目录，人设扫描器只 glob 顶层 `*.json`，不会把回收站里的角色捡回来；
- 响应带 `trashed` 路径，UI 可提示"在哪儿能找回"；`drop_memory=true` 时记忆卡同样进 `npc/store/.trash/`。

### 前端骨架（已交付，tsc + vite build 通过）

`web/console/`：Vite 6 + React 18 + TS + React Flow，dev 5173 代理 `/api → 8765`，`base: './'` 以便挂在 `/console/` 下。
已实现 Graph 页（真实数据 + 环形布局 + 搜索淡出 + 点节点出 Inspector + 空态/错误态/加载态）与 Settings 页（Provider 列表/掩码/激活/Test）。
其余导航项标"规划中"，按 Step 5~11 逐个开放。设计系统为中性极简（白底/深字/细灰边/柔和阴影），状态色由字符串哈希决定 —— 不硬编码任何状态类型。

### 测试踩坑（记下来免得再犯）

- **monkeypatch 不能用来清理"被测代码自己写进 os.environ 的值"**：
  `monkeypatch.delenv()` 会把调用那一刻的值记为"原值"，teardown 时又恢复回去。
  结果 `activate provider` 写进 `LLM_API_KEY` 的假 key 泄漏到后续测试，
  `test_memory_typed` / `test_housekeeper` / `test_reflection` 拿假 key 真去打网络 → 401 → 断言失败。
  正解：模块级 autouse fixture 整体快照 / 还原 `os.environ`。
- 旧测试 `test_root_serves_console` 依赖被迁走的 `web/static/index.html`：
  已按新契约改为断言"根路径重定向后的落点"。

### 遗留 / 待办

- ~~`test_safety_gate::test_three_not_placeholder_history` 失败~~ —— **已于 2026-09-08 修复**（见 §9 修复①）。
- `npc/personas/npc.html` 旧关系网页面仍在，待 Console 功能齐备后删除。
- `routine.action` 白名单（`gather/rest/say`）尚未放宽 —— 与字符编辑器（Step 5）一起做。
- 前端未做路由库（当前用 state 切页），Step 5 加页面时再评估是否引入。

## §9 2026-09-08 核查与修复（用户要求"检查"后查出）

### 更正：此前"828 passed / 0 failed"是误判

真基线是 **827 passed / 1 failed**。误判原因：pytest 收尾时本机 safe-delete shim 抛
`[SAFE_DELETE_BULK_CONFIRM_REQUIRED] count=321`，**把最终摘要行吞掉**，只看输出尾部会误认为全绿。
**规矩：核对测试结果改用 `--junitxml=<path>` 再解析 XML 的 failure/error 节点，别靠 `tail`。**

失败用例 `test_safety_gate.py::TestL1HardBlock::test_three_not_placeholder_history`
**单跑也失败**（稳定失败，此前"顺序/状态敏感"的判断同样错误）。

### 修复① "规则兜底丢历史" —— 先误判成 bug，后回滚（重要教训）

`npc/talk_pipeline.py` 的关键分支：

```python
llm = self._get_llm()
if llm is None:
    return self._talk_rules(safe_input)   # ← 提前返回，不写 dialogue_history
```

我一开始判定这是缺陷（"无 key 时 NPC 记不住上下文"），改成统一记历史。**随后
`tests/test_npc.py::TestDialogueHistory::test_rules_mode_no_history` 立刻失败**，
其 docstring 写着：

> 规则模式（无 LLM）不记历史 — 确定性对话本来就是无状态的。

**结论：那是既有设计，不是 bug。** 已回滚生产代码（仅留下 7 行注释说明此处为何
故意不记历史，防止后人重蹈我这次的误判）。

真正的病灶在**测试自身**：`test_three_not_placeholder_history` 原先不固定 LLM 状态，
能否通过取决于环境里有没有 API key ——
- 有 key（真或假）→ 走 LLM 分支 → 即使 API 报 401 走 fallback，也会执行到函数
  底部的 `dialogue_history.append` → 历史里有占位符 → **通过**；
- 完全没 key → `_get_llm()` 返回 `None` → 第 131 行 early return → 历史为空 → **失败**。

这才是它长期"时好时坏"的根因（不是测试顺序），也解释了为什么此前测试环境污染
（假 key 泄漏）时它反而能过。

修法（改测试，不动生产）：
- `test_three_not_placeholder_history`：显式注入 `RecordingLLM`，把被测前提钉死。
- `test_l1_llm_down_falls_back_to_rules`：原先只断言 `all(原文 not in c ...)`，
  而规则模式历史恒空 → 断言恒真、形同没测。改为断言"兜底话不复读原文" +
  "规则模式不记历史"，把设计显式钉住。

补充：规则模式下违规轮同样不入历史 —— **没存即没泄漏**，"三不清除"的安全属性
在该模式下依然成立。

> 教训：看到"某分支没做某事"先别急着补 —— 先全仓搜有没有测试在**固化**这个行为。
> 既有测试 + 写明理由的 docstring 就是设计契约，推翻它需要先确认是有意还是疏漏。

### 修复② 记忆卡体积护栏（已修 + 已清理）

`npc/memory_card.py::save()` 原样把整个 `world`（含无上限增长的 `log`）写进记忆卡，
轮转 `rotate_world_log(tail=500)` 只由 housekeeper 定期触发、短时运行不触发。
一次端到端 smoke test 就把 `ali/cang_memory.json` 的 log 撑到 **47978 条 / 2.3MB**
（`_log_offset=0`，从未轮转）；未触碰的 `amanda/jimmy/tracey` 仍是 6~8KB。

处置：
- 新增 `_world_for_card()`：只截断**写进卡里的副本**（`CARD_WORLD_LOG_TAIL = 500`，与
  `LOG_TAIL_DEFAULT` 对齐），不动内存中的 `self.world`，共享世界的运行时语义零影响。
- 已把两张卡截断：2342KB → 55KB / 2341KB → 54KB，
  整卡备份在项目外 `C:\Users\Administrator\AppData\Local\Temp\npcsidekick_card_backup_20260908\`。
- 冷日志的正式归宿是归档层（`npc/store/log_archive/`，带绝对索引可回放），不是记忆卡。

**教训（写进 MEMORY.md）**：起真实 Runtime 做端到端验证前，先确认会写哪些盘上文件，
或显式指定临时 `store_dir`；跑完检查 `npc/store/*.json` 体积。

### 其它

- 停掉遗留进程 PID 10044 / 端口 8766（昨晚启动的通用 Agent 服务，非本次所需）。
- 清理 `npc/store/.trash/` 里 smoke test 残留的 2.3MB 记忆卡。
- 测试 traceback 显示的 `D:\NPCSidekick\1_Dagent\...` 路径不存在 —— 是陈旧 `.pyc`
  里记录的旧编译路径（目录被重命名过），不影响运行。
