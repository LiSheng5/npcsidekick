# NPC Runtime 审计（Phase 1 · 只读）

> 日期：2026-09-15｜执行：工友｜验收：主控
> 范围：**只读审计，未改动任何生产代码。**
> 方法：逐文件读码（`npc/` 6856 行 + `agent/` 5478 行）+ git 核对 + 全量 pytest 独立解析（junitxml，不依赖终端摘要）。
> 对象：把 brief「World NPC Runtime」的 12 个 Phase 逐条对照现有实现，判定 **已有 / 部分 / 真缺**，并给出最小改动路径。

---

## 0. 审计口径

### 0.1 一句话结论

**brief 的前提假设（"Agent framework prototype"）与项目实际状态不符。** 实测这是一个 **v3.2-dev 完整 NPC 运行时**：
自主 tick 已在跑、世界契约已定义、任务账本 + 三道门 + fail-closed 已成体系、记忆卡/反思/画像/管家四层记忆已落地、Web Console 7 页全实现。
**12 个 Phase 里 6 个已有、4 个部分、2 个真缺（Goal 真值层、Benchmark）。** 按 brief 字面执行会重造轮子，并违反 brief 自己写的"不要为了看起来很 AI 而增加复杂度"。

### 0.2 已拍板决策（2026-09-15 主控）

| 问题 | 裁决 |
|---|---|
| 推进节奏 | **先只做审计**，产出本文档后停下等拍板，再动代码 |
| 落点轨道 | **扩展现有 `world.py`**（不新建独立 WorldState 体系） |
| 自主行动执行者 | **引擎内置执行器**（文本世界自演化，mod 联机时接管） |
| Benchmark 口径 | **两层**：机制指标走 mock 全量，关键对比（有/无反思、有/无记忆）走真 API 少量 |

### 0.3 测试基线（独立证据 ✅）

命令：`python -m pytest tests -q --junitxml=D:/NPCSidekick/runtime_audit_junit.xml`（系统 python 3.13.14）

```
TOTAL tests=847  failures=0  errors=0  skipped=0      （耗时 49.5s）
```

用 **junitxml 独立解析**取数，不依赖终端摘要 —— 实测终端摘要在收尾时被 safe-delete shim 吞掉（项目老坑，本次复现）。
口径说明：**847 已包含工作区里未提交的 `tests/test_safety_gate.py` 加固改动**（详见 §附）。

**工作区状态**（`git status`，只读核对）：

| 项 | 状态 | 说明 |
|---|---|---|
| `tests/test_safety_gate.py` | `M`（未提交） | 2026-09-12 加固：原用例只写 `npc._llm = None`，但 `_get_llm()` 会懒加载重建客户端 → 环境里有 key 就真打网络，断言结果取决于环境（假绿/假红）。改为 `use_llm=False` 钉死前提。**是正确方向的修复，建议提交。** |
| `CLAUDE.zh.md` | `??`（未跟踪） | 未纳入版本控制 |
| `npc/store_godot/` | `??`（未跟踪） | Godot 接入线的运行时 store，未纳入 |
| git 同步 | ✅ 41 提交，`master...origin/master` 为 `0 0`（无 ahead/behind） | 本地与远端一致 |

---

## 1. 当前架构（实测）

### 1.1 双轨结构 —— 理解一切的前提

项目是**两条互不调用的轨**，命名相近、语义不同：

| 轨 | 位置 | 定位 | 决策入口 |
|---|---|---|---|
| **agent/（引擎轨）** | `agent/` 5478 行 | 通用 Agent 4 层框架：Planner → Executor → ToolRouter → Memory + Reflector + Provider。**一问一答语义**（`TaskPlan` / `Step`） | `agent/orchestrator.py::run()` / `run_stream()`，CLI `main.py` 用 |
| **npc/（运行时轨）** | `npc/` 6856 行 | NPC 运行时：自主 tick + 任务账本 + 记忆卡 + 安全闸 + HTTP 服务器 + Web Console API | `npc/scheduler.py::tick_round()`（自主）、`npc/talk_pipeline.py::talk()`（对话） |

**关键事实：`npc/` 不 import `agent/planner|executor|tools`。** 只复用 `agent.llm.client`（LLM 薄包装）、`agent.logging_config`、`agent.config_flags`。
→ NPC 的"规划"是 `npc/reviewer.py` 的 manifest 编译 + `npc/scheduler.py` 的规则步骤链，**不是** `agent/planner`。
→ 这正是 brief 第一原则"优先复用现有 Planner / Executor / Reflector"最容易踩空的地方：**按字面接进 `agent/planner` 会接错轨**（已由 0.2 裁决规避）。

### 1.2 一次 tick 里发生什么（自主循环）

```
server._tick_loop            (npc/server.py:192，每 NPC_TICK_INTERVAL 默认 3s 一帧)
 └─ tick_round(world, npcs)  (npc/scheduler.py:305)
     ├─ world["_tick"] += 1 + _regen_resources()      (资源向 _resource_caps 回补)
     ├─ for actor_id in interaction_priority(world):  (:324 —— 近主角者优先，保持互动连续性)
     │   └─ _tick_one(npc, world)                     (:245)
     │       ├─ 无 activity:
     │       │   ├─ pending_task 优先（玩家派活 > 自主日常），且 not _protocol_owns_pending()
     │       │   └─ 否则 _choose_routine_item()        (:190  加权随选取日常)
     │       ├─ _plan_steps() → 步骤链                  (:155  walk→gather×count→walk→deliver×count)
     │       ├─ 有 activity: pop(0) → _execute_step()   (:212)
     │       │   └─ world.apply_action()                (world.py:161 —— 唯一世界变更点，代码权威)
     │       ├─ 步败 → _blocked 冷却 5 tick + remember(EV_FAIL+desc, imp=4)
     │       └─ 步尽 → remember(EV_DONE+desc, imp=5)
     └─ 返回转换事件 {started/completed/failed} → server 侧 save()（零 I/O 设计）
```

**纯规则、零 LLM**（`scheduler.py:8` 自述：普通 NPC 轻路径，LLM 只留在对话层）。这已经是 brief「第四阶段 Tier 0」的实质实现。

### 1.3 一次对话里发生什么

```
talk(player_input)                                  (npc/talk_pipeline.py:96)
 ├─ ① 入站安检门 safety.scan()                       (:107  NPC_SAFETY_GATE；L1 → 占位替换，原文不落盘)
 ├─ ② 回忆快路径 _try_recall()                       (:206  命中"记得/之前/前两天" → 记忆卡逐字答，零 LLM)
 ├─ ③ 指令快路径 _try_task_command()                 (:351  reviewer.compile_task → book.guarded_book 三道门)
 ├─ ④ LLM 生成 _chat_with_review()                   (:181  生成 → B2 编译尝试落账 → 出站 review_dialogue
 │                                                          违诺拦截 → 重生成一次 → 仍违则规则回退)
 └─ sys_content = system_prompt + _build_context()   (:25)
      ├─ 【世界状态】   observe()
      ├─ 【行为日志】   _behavior_log()                (:67  从 world.log 反扫，防 confabulation 的事实源)
      ├─ 【你对玩家的了解】 画像文件
      ├─ 【记忆】       memory.retrieve(query, top_k=5) → format_for_context
      ├─ 【规则】      接地指令（只准复述已发生的事）
      ├─ 【欲望】+【目标】 persona.desires / persona.goals（带 progress/target）
      └─ 【自定义状态】 context_extra
```

---

## 2. brief 十问 · 逐条回答

### Q1. 当前 NPC 的"世界状态"存在哪里？

**`world` dict 是唯一世界状态源**（`npc/world.py:44::default_world()`），纯 JSON 可序列化/可编辑/可存档。

| 键 | 内容 |
|---|---|
| `actors` | `{id: {position, inventory, stamina}}` —— 每个 NPC 一个槽 |
| `locations` | `{名称: {desc, resources, exits}}` —— 4 地点图 + BFS 寻路（`:135::find_path`） |
| `protagonist` | 主角位置与名 |
| `delivered` / `recipes` / `_resource_caps` | 交付累计 / 合成配方 / 资源再生上限 |
| `log` | 世界事件日志（唯一事实流；冷层归档 `:250`，档位 `:290`，归档压缩 `:347`） |
| `_tick` / `_weather` / `_game_hour` | 扩展字段（下划线前缀 = 非契约语义） |

**持久化 = 每个 NPC 各存一份整 world 快照进自己的记忆卡**（`memory_card.py:113::save` → `_world_for_card()`，log 截断 500 条）。
→ **⚠️ 张力**：内存里多 NPC 共享同一 world 对象（`npc.py:89-91`，传入即引用），但盘上是**每 NPC 一份副本**，`load()` 时又以各自副本为准（`npc.py:158`）。**"共享世界 vs 各自快照"不对称，重启后一致性无定义、无测试**（见 TD2 / R4）。

### Q2. NPC 如何知道自己看到/知道了什么？

`observe(world, who)`（`world.py:98`）把世界转成**第一人称文本**：当前位置+描述 → 本地资源 → 可前往 → 背包 → 耐力体感（<30 / <60 两档话术）→ 天气 → 深夜 → 同地其它 NPC / 主角。
注入点：`_build_context` 的【世界状态】（`talk_pipeline.py:41-42`）。

- **只覆盖"当前位置"**，无视野/感知范围/遮挡概念；看不到别处的世界事实（他人库存、谁拿了什么）。
- 另有**事实源通道** `_behavior_log()`（`:67`）：从 `world.log` 反向扫最近 6 条互动 + 自主活动语言化摘要一行。这是防止 LLM 编造的关键设计（Day 2 实测教训：接地指令只能压制不能根除 confabulation）。
- 游戏端可经 `/api/talk` 的 context 直通同步 `_weather` / `_game_hour`（真实数据优先，未同步则按 tick 自推）。

### Q3. NPC 如何判断一个任务是否完成？

**三套并存，语义与权威各不相同**：

| 路径 | 判定者 | 现状 |
|---|---|---|
| 规则多步任务 `run_task`（`npc.py:193`） | 代码（每步 `apply_action` 返回 ok） | 真值判定、零 LLM，但**已脱离热路径**（仅 `/api/task` 手动派活用，审查报告 S3） |
| 自主日常 `_tick_one` | 代码（步骤链走完 = completed，任一步失败 = failed） | **真实世界反馈，不假装成功** ✅ |
| 玩家派活 + 协议门 | `LEDGER` 状态机，**销账靠外部 mod 回报** `settle()`（`taskloop.py:186`）；300s 未销账 → `reap_zombies`(:231) 判 `failed(timeout)` | 只有 `NPC_TASK_LOOP=1` 才走这条 |

→ **真缺口**：引擎自跑那条路（门关，默认）**只写记忆、不销账**；开与关是互斥分支（`_protocol_owns_pending`，`scheduler.py:43`）。
**不存在"引擎执行 + 引擎记账"的组合** —— 而这恰好是 0.2 裁决「引擎内置执行器」要做的事（见 Phase 5 建议）。

### Q4. NPC 如何产生自主任务？

**现状不是"任务"，是"活动"。** `_choose_routine_item`（`scheduler.py:190`）对 `persona["routine"]` 做**加权随机抽签**：

```
weight = 静态 weight × _dynamic_weight()            (scheduler.py:67)
         └─ 耐力(力竭×8 休息 / 干活×0.1) × 昼夜(夜间干活×0.4) × 天气(雨×0.5) × 库存缺口(delivered<5 ×1.6)
```

- **有优先级：没有。有 deadline：没有。有前置条件：没有。有跨 tick 目标保持：没有。** 是抽签，不是计划。
- **`persona.goals` / `persona.desires` 完全不参与这个抽签**（只在 prompt 里当文本）。
  → **这是"感知 → 记忆 → 形成目标 → 行动"闭环真正断掉的一环。**

### Q5. Memory 是如何进入 Planner 的？

**不进 Planner（`agent/`），进的是对话上下文。**
`self.memory.retrieve(player_input, top_k=5)`（`talk_pipeline.py:31`）→ `format_for_context()` → prompt 的【记忆】段。

检索打分（`npc/memory.py:281`）：

```
score = recency × _GW[0] + relevance × _GW[1] + importance × _GW[2]
      + 一跳关联加分（与 query 共现实体 × _ASSOCIATION_WEIGHT）
      + 可选 max(·, BM25)（NPC_BM25_RECALL）
      + 可选 RRF 融合（NPC_VECTOR_ANCHOR，倒数秩融语义路）
```

→ **自主决策路径（`scheduler`）一个字都不读记忆。** 记忆的消费者只有：对话 prompt、回忆快路径、画像素材。

### Q6. Reflection 是否真正影响下一次决策？

**路径存在，但影响的是"措辞"，不是"决策"** —— brief 第六阶段"必须发生可观察变化"目前**不成立**。

`maybe_reflect`（`memory_card.py:199`）：未反思条目重要性之和 ≥ 18 → LLM 归纳（或规则兜底）→ 写 `category="reflection"`（importance=8）→ 之后可被 `retrieve()` 捞到 → 进【记忆】段 → 影响 LLM 回复。

三个缺口：

1. 反思是**自然语言句子**，无 `lesson` / `confidence` / `scope` / `recommendation` 结构；
2. **不影响 `_choose_routine_item` 的权重** —— scheduler 不读记忆，所以"下次换个路走"物理上不可能发生；
3. **无"同任务 有/无反思"A/B 证据**（brief 明确要求的那组对比测试不存在）。

> 唯一**结构化**的类反思机制是**失败商议队列**（`taskloop.py:206::fail` → `_discussions` → `talk_pipeline.py:151::pop_discussions` 注入【未完成的事·主动提起】）。
> 但它是"下次见了玩家主动认账"（影响对话），**不是"改策略"**。

### Q7. Executor 执行动作后，如何反馈真实结果？

`apply_action(world, action, params, who) -> (world, ok, message)`（`world.py:161`）。

**结论：确定性世界引擎（brief 第十四阶段）已经存在，且比 brief 要求更严** —— 所有行动都由代码改世界，失败返回真实原因（`"无法从A前往B"` / `"材料不足: 需要木材×2（背包有0）"` / `"主角不在这里，无法交付"`）。LLM 无任何直写世界的路径（三道门 `book.guarded_book` + manifest 白名单 + fail-closed）。

**但缺结构化 ActionResult**（brief 要的 `success/status/observation/world_changes/error/duration`）：

| 字段 | 现状 |
|---|---|
| `success` | ✅ `ok: bool` |
| `error` | 🟡 与成功共用 `message: str`（人类可读中文），无机器可判的错误码 |
| `observation` | 🟡 调用方得另行 `observe()` |
| `world_changes` | ❌ 需 diff world 反推 |
| `duration` | ❌ 未记录 |

另注：`apply_action` **原地修改** `world`（非 copy-on-write），调用方必须使用返回的新引用。

### Q8. 多个 NPC 是否可以共享同一个 WorldState？

**能，且这是现状默认**（`npc.py:89-91`：传入 world 即引用；server 起一个 world 给全部 NPC）。`actor_of`（`world.py:86`）为每个 NPC 开槽，出生点可用 `_default_spawn` 声明。
`interaction_priority`（`scheduler.py:324`）保证多 NPC 交错推进且靠近主角者优先；`tick_round` 契约要求单线程串行调用。

→ **但如上 Q1 所述，持久化是"每 NPC 一份副本"**。重启加载时谁是权威无定义 → 见 TD2。

### Q9. NPC 是否能够拥有持久化人格和目标？

**人格 ✅ 真持久；目标 🟡 只有文本，是"装饰性持久化"。**

| 层 | 载体 | 状态 |
|---|---|---|
| 声明层 | `npc/personas/*.json`（`cang` / `ali` 等） | ✅ `PERSONA_FIELDS`（`persona.py:22`）11 个字段契约 |
| 运行时层 | 记忆卡里的 `persona` 段（随卡落盘） | ✅ `save()`/`load()` 往返 |
| 高层画像 | `npc/store/{id}_persona.md`（四层扫描，≤2000 字，`NPC_PERSONA` 开关） | ✅ 增量修订 + mtime 缓存 |

**目标**：`persona["goals"] = {"部落安稳过冬": {"progress": 0, "target": 1}}`
→ 只在 prompt 注入 `【目标】部落安稳过冬(0/1)`（`talk_pipeline.py:59-61`）。
→ **全仓没有任何一处写回 `progress`**（grep 证实）。**是给人看的标签，不是可推进的目标。**
**欲望**同理：`desires = {文本: 权重0~1}`，权重**不参与任何决策**。

### Q10. 当前哪些地方实际上仍然只是 demo 级逻辑？

按严重度排序：

| # | demo 级残留 | 证据 |
|---|---|---|
| 1 | **Goal / Desires 是装饰**：只进 prompt，无进度推进、不参与决策 | `talk_pipeline.py:56-61`；`progress` 无写回点 |
| 2 | **自主决策不读记忆**：scheduler 与 memory 零耦合 → "经验影响行为"整条链不存在 | `scheduler.py` 全文件不 import `memory` |
| 3 | **世界是 4 地点玩具图**（村庄/森林/矿洞/河边），通用层硬编码 `walk_to("村庄")` | `world.py:55-76`；`npc.py:258,259`（审查报告 S1，违反游戏无关红线） |
| 4 | **无世界存档**：世界状态按 NPC 各存一份副本，重启一致性无定义 | `memory_card.py:113/139` |
| 5 | `_reflect_rules` **双定义**（npc.py:67 覆盖了 :28 从 memory_card 的导入） | `npc.py:28` vs `npc.py:67` → 导入的死分支 |
| 6 | `_a_semantic_block` **封存方法仍在生产文件**（§22 退役，无调用点） | `npc.py:431`（审查报告 S4） |
| 7 | **`run_task` 脱离热路径**：craft 只在它里面，scheduler 的 routine 永不 craft | 审查报告 S3 |
| 8 | `npc/benchmark.py` **是 90 行 print 脚本**，不是 benchmark；无 `tests/e2e`、`tests/benchmark` | 实测目录不存在 |
| 9 | **长跑未验证**：无 30 分钟连续运行证据；记忆/向量锚/归档轮转在长跑下的行为只做过体积护栏 | — |
| 10 | **social 能力为 0**：`/api/relationships` 已就绪但 `world["_relationships"]` 永远为空 | `console_api.py:150-221`（诚实空态 `source="none"`，不造假） |

---

## 3. 覆盖矩阵：brief 12 Phase vs 现状

| Phase | brief 要求 | 现状 | 证据 / 差什么 |
|---|---|---|---|
| 1 代码审计 | 出 AUDIT 文档 | ✅ 本文档 | 另有 `docs/全面审查报告_2026-09-10.md`（全项目健康体检，视角不同，互补） |
| 2 WorldState | 一等公民、与 LLM 解耦 | 🟡 **80%** | `world.py` 已有；**缺** `entities` / `objects` / `relationships` 三个一等公民键 |
| 3 Goal / Motivation | 三级目标 + 队列 + 生命周期 + 6 字段 | 🟡 **20%** | `persona.goals` 有 `progress/target` 雏形且已注入 prompt；**缺** priority/status/deadline/prerequisites/source、**缺**队列、**缺**生命周期、**缺** progress 写回、**不参与决策** |
| 4 Autonomous Tick | 无玩家输入可跑 | ✅ **已有** | `server.py:192::_tick_loop` 每帧 `tick_round`；`_tick_one` 纯规则零 LLM |
| 4b decision tier | Tier0–3，多数 tick 不调 LLM | 🟡 **部分** | Tier0 已实现（规则 routine × 4 个动态因子：耐力/昼夜/天气/库存缺口）；Tier2/3 的 `LLMScheduler`（`scheduler.py:378`）已有可队列化，但默认 OFF |
| 5 Action → Consequence | ActionResult 结构体 | 🟡 **60%** | `apply_action` 返回 `(world, ok, msg)`；`TaskLedger` 全套（链式/僵尸账/商议）；**缺**结构化 ActionResult（见 Q7） |
| 6 Reflection → Replan | 必须可观察改变行为 + A/B 测试 | 🟡 **30%** | 反思机制、触发阈值、去重、指针推进都有；**缺**结构化 lesson、**缺**"影响决策"、**缺** A/B 证据 |
| 7 Memory | 四类记忆 + 多因子 relevance | 🟡 **75%** | 三分类 `MTYPES`（persona/episodic/instruction）+ 加权检索 + 一跳关联 + BM25 + 向量 RRF + 冷层归档 + 记忆管家；**缺** `goal_relevance` 因子（brief 5 个因子里 4 个已有） |
| 8 Personality | 结构化画像影响决策 | 🟡 **40%** | 有 persona 声明层 + `personality` 字符串 + 画像文件；**brief 说"不能只是 system prompt"，而现状恰恰就是 system prompt** —— 人格不参与任何决策 |
| 9 Relationship | RelationshipGraph | 🟡 **接缝已留，数据为 0** | `console_api.build_relationships` 读 `world["_relationships"]` + `/api/relationships` + 前端 GraphPage/NpcInspector 已消费；**缺**引擎侧产生与更新 |
| 10 Event System | WorldEvent 订阅式 | 🟡 **轮询式已有** | `world.log` + `/api/events` 游标 + 账本派发事件队列 + 归档；**非订阅式**（是否真需要订阅制存疑，见 Phase 10 建议） |
| 11 Small World | village 5 NPC / 4 地点 | 🟡 **等价物已有** | 文本世界参考实现（4 地点 + cang/ali）+ `examples/actions.json`；**缺** `examples/village/` |
| 12 Benchmark | e2e + 数字证明 | ❌ **真缺** | `npc/benchmark.py` 是 print 脚本；无 `tests/e2e`、`tests/benchmark` |
| 14 Deterministic Core | LLM 不写 WorldState | ✅ **已更严** | `guarded_book` 三道门 + 账本即承诺 + manifest 能力协商门 + fail-closed |
| 15 安全边界 | permission / risk_level / timeout | 🟡 **70%** | `safety.py`（L1/L2 安检门）+ `reviewer.py` manifest（`tier` + `approval=allow/ask/deny` + `params` 白名单）；**缺** `risk_level` 三级（现为 tier+approval 两维） |

**真缺只有 2 项**：Goal 真值层（Phase 3）、Benchmark（Phase 12）。
**"部分"的 8 项**里，真正撬动闭环的是 3 个：**决策读记忆（6）、人格参与决策（8）、关系有数据（9）**。

---

## 4. 已有能力（做得好的，别改坏）

| 能力 | 证据 |
|---|---|
| **LLM 只提议、代码决定执行**（铁律） | `book.guarded_book` 单入口；`npc.py:317::book` 是唯一 `pending_task` 创建点 |
| **确定性世界引擎** | `world.py:161::apply_action` 唯一世界变更点 |
| **三道门 + 能力协商 + fail-closed** | deny 档 → `review_task` 可行性 → manifest ∩ 活跃消费者动词；门坏 → 降级旧语义不卡对话 |
| **诚实边界** | 安检 L1 三不清除（原文不落盘）；`/api/relationships` 无数据 → 空 + `source="none"`，**不造假**；记忆不可用字段不编 |
| **防 confabulation 双保险** | 提示词接地指令 + `_behavior_log()` 世界日志直供事实源 |
| **反思卫生** | 阈值 18、噪音批跳过、产出去重、`archived` 退出反思候选（任务书 #06）；指针推进口径防"同一批反复检" |
| **记忆卡体积护栏** | 卡内 log 截断 500 条 + 归档轮转带绝对索引（游标兼容）+ 连续自主段压摘要（幂等） |
| **自主循环契约干净** | `tick_round` 纯函数零 I/O，save() 由调用方按转换点触发 |
| **成本控制** | 闲聊零 LLM 调用（快路径前置）；反思/画像走 `SCHED.invoke` 可队列化；`LLM 决策绕过` |
| **可观测** | `features.flags` 暴露 8 个开关生效态；Web Console 7 页全可达、24 个 API 全有后端路由 |

---

## 5. 缺失能力（真缺口，按"对闭环的杠杆"排序）

| # | 缺口 | 为什么它是杠杆 | 落点（已按 0.2 裁决） |
|---|---|---|---|
| **G1** | **决策层读记忆/目标** | 撬动 Phase 6 整条闭环。现在"记忆/反思/画像/管家"这半壁代码对**自主行为影响为零** | `scheduler.py:67::_dynamic_weight` 增加因子（纯数值，零 LLM） |
| **G2** | **Goal 真值层** | 三级目标 + 队列 + 生命周期，且必须被 G1 消费；否则又是装饰 | 新 `npc/goal.py`；`persona.goals` 降为初始种子（向后兼容） |
| **G3** | **ActionResult 结构化契约** | 目标进度推进、关系变化、benchmark 指标都从这里消费 | 在 `apply_action` **外面**包一层薄包装，不改旧签名 |
| **G4** | **关系数据（事件驱动）** | `_relationships` 槽位/API/UI 三件套已就绪，只缺"有人往里写" | `world.py` 加 `relationships`；由 G3 的后果驱动增量 |
| **G5** | **Benchmark 骨架** | brief 最终验收 I 项；也是"改完到底有没有变聪明"的唯一证据 | `tests/benchmark/` + `tests/e2e/`，两层口径 |
| **G6** | **世界存档单点** | 现在"共享内存 vs 每 NPC 一副本"，重启一致性无定义 | 需先定权威归属（见 §9 待拍板 4） |

---

## 6. 最大技术债

| # | 债 | 性质 | 影响 |
|---|---|---|---|
| **TD1** | **"记忆只进对话，不进决策"** | **架构级断层**（非 bug） | 记忆卡 + 反思 + 画像 + 管家约占 `npc/` 近半代码，对自主行为的影响是 **0**。brief 想建的闭环卡在"行动"这一环 |
| **TD2** | **持久化语义分裂** | 设计缺口 | world 在内存共享、在盘上每 NPC 一份副本；重启后一致性无定义、无测试（`memory_card.py:113/139` vs `npc.py:89-91`） |
| **TD3** | **双轨漂移** | 认知债 | `agent/` 与 `npc/` 各有一套"记忆/反思/执行"，命名相近语义不同（`agent` 的 Reflector ≠ `npc` 的 `maybe_reflect`）→ **brief 本身就接错了轨**，新人必踩 |
| **TD4** | **装饰性字段无守护** | 契约债 | `goals` / `desires` 有 schema 无机制，测试只能测"注入了文本"，测不出"没生效" |
| **TD5** | **死代码 / 遮蔽 / 硬编码** | 卫生债 | `_reflect_rules` 双定义；`_a_semantic_block` 封存未迁；`walk_to("村庄")` 游戏地名进通用层（S1/S3/S4） |

---

## 7. 风险

| # | 风险 | 对策 |
|---|---|---|
| R1 | **LLM 成本**：反思/画像用 `reasoning_effort=max`（`memory_card.py:277/303/404`），单次贵；benchmark 真跑需控量 | 两层口径已拍板：机制走 mock，仅 A/B 关键对比走真 API，并设调用上限 |
| R2 | **开关默认 OFF 的"暗路径"**：8 个 `NPC_*` 开关默认关闭；benchmark 若在 OFF 下跑，测的是旧行为 | benchmark 必须显式声明开关矩阵（每项指标标注开关态） |
| R3 | **协议划界回归**：改成"引擎内置执行器"要动 `_protocol_owns_pending` 划界 → 双记账（历史 B6 同类） | 先补回归锚再改（项目既有纪律：测试守护先行）；`test_ledger_boundary.py` 6 例可扩 |
| R4 | **长跑资源增长**：30 分钟连跑下记忆条目/向量锚/归档文件的增长未实测 | Phase 12 前先做一次长跑灌水观测（零 LLM，纯规则跑） |
| R5 | **游戏无关红线**：village demo 的角色/地点/物品词一旦进 `npc/` 即破线（S1 已是轻微违反） | 所有游戏词只允许出现在 `npc/personas/*.json` 与 `examples/village/` 声明里；引擎层零游戏词 |
| R6 | **账本是内存态**（`taskloop.py:99-106` 自述） | 重启丢账、靠重新派发写日志自愈 —— benchmark 的"长跑"语义要吃这一点 |

---

## 8. 推荐修改顺序（最小改动版）

原则：**不新建平行体系、不重造已有的 6 项、每个 Phase 都带向上兼容开关**（关 = 与旧版逐字节一致，沿用项目既有习惯）。

| Phase | 建议动作 | 改动面 |
|---|---|---|
| **1** | ✅ 本审计 | 0 生产代码 |
| **2 世界** | **扩 `world.py`**：加 `relationships`（G4）；`entities`/`objects` 作为 `locations` 的可选扩展键（声明驱动，缺省零差异） | `world.py` 加键与访问器 |
| **3 目标** | **新 `npc/goal.py`**：`Goal`（priority/status/deadline/progress/prerequisites/source）+ `GoalQueue` + 生命周期 `PENDING→ACTIVE→BLOCKED→COMPLETED→FAILED→ABANDONED`。**唯一写入口是代码**（LLM 只可提议）。`persona.goals` 降为初始种子 | 新模块 + `npc.py` 接线 |
| **4 自主 tick** | 只加"决策来源"：`_choose_routine_item` 的候选从 `routine` 扩为 `routine ∪ active_goals`，权重复用 `_dynamic_weight` + goal 的 priority/urgency。**Tier 分层沿用现状**（Tier0 = 现有规则，Tier2/3 = 现有 `SCHED`），不新造框架 | `scheduler.py` 局部 |
| **5 后果** | `apply_action` **外包一层** `ActionResult`（success/error_code/observation/world_changes/duration），旧签名不破。目标进度推进、关系增量都在此消费（G3） | 新薄包装 + 消费点 |
| **6 反思→重规划** | 给 reflection 条目加**可选** `scope` + `recommendation` 两字段（老条目缺省 → 不参与）；`_dynamic_weight` 读"scope 命中"的 lesson 调权重。**必带 A/B 测试**（brief 要求的同任务有/无反思对比） | `memory_card.py` + `scheduler.py` + 新测试 |
| **7 记忆** | 打分**只加一个因子** `goal_relevance`（开关控制，关时逐字节同分）。brief 5 因子中 4 个已有 | `memory.py` 一处 |
| **8 人格** | `personality` 从字符串升为**可选**结构化（risk_tolerance / sociability / curiosity / patience / loyalty 等），**影响 `_dynamic_weight` 的权重先验与系数** —— 不是 prompt。带 A/B（两种人格 → 不同决策） | `persona.py` + `scheduler.py` + 测试 |
| **9 关系** | `world["_relationships"]` 由**事件驱动**增量更新（一次交付/一次说话 → friendship/trust ±），`build_relationships` 已就绪。**先做 2–5 NPC 之间可重复验证的变化** | `world.py` + 事件消费点 |
| **10 事件** | **建议降级/不做**：现有 `world.log` + `/api/events` + 账本派发队列已够 benchmark 与 UI 用；订阅式暂无可证收益 | 0（请拍板） |
| **11 小世界** | `examples/village/`：**声明式 JSON**（5 persona + world 声明），引擎层零游戏词 | 新 `examples/village/` |
| **12 Benchmark** | `tests/benchmark/` + `tests/e2e/`：机制指标 mock 全量 + A/B 真 API 少量。**核心问题：加了 Memory / Reflection 之后 NPC 到底有没有变聪明** | 新测试目录 |

**最大的两处提醒**：
1. **Phase 6 与 Phase 4 是同一件事的两面** —— 只有先把"决策层读记忆"（G1）做出来，反思才可能改变行为。若按 brief 顺序把 Phase 6 做成"反思日志"，就是白做。
2. **Phase 10 建议砍掉**，Phase 2/5/7 只做"扩键/加薄层/加一个因子"，避免代码量膨胀（符合 brief 的"目标不是增加代码量"）。

---

## 9. 待你拍板

| # | 问题 | 我的建议 |
|---|---|---|
| 1 | **文档落点**：本审计按 brief 指定放 `docs/NPC_RUNTIME_AUDIT.md`；项目惯例是中文文件名 + `docs/NPC大脑架构.md` 编号章节。后续 3 份（ROADMAP / WORLD_MODEL / BEHAVIOR_BENCHMARK）怎么办？ | 审计这份保留（brief 指定），后续 3 份**并入 `NPC大脑架构.md` 编号章节**，避免"模块清单两处维护" |
| 2 | **Web Console 可观测**：新增 goal / relationship 后要不要最小可视化？ | API 先出口；`/api/relationships` 与 Graph 页已能消费，goal 可先复用 Live/Activity 页，**不做新页** |
| 3 | **`examples/village` 形态**：可跑参考世界 vs 教学模板？ | 可跑（能进 benchmark），零游戏词进引擎层 |
| 4 | **世界权威归属**：世界状态是"大脑权威"还是"游戏端权威、大脑只提议"？（影响 G6 与 Phase 5 设计） | 文本/单机 = 大脑权威；联机 = 游戏端权威、大脑只提议。**按有无活跃消费者切换**，复用现有 `_protocol_owns_pending` 语义 |
| 5 | **未提交文件处置**：`M tests/test_safety_gate.py`、`?? CLAUDE.zh.md`、`?? npc/store_godot/` | 见 §0.3。测试改动方向正确、建议提交；另两个未跟踪项需你说明去留。**我不擅动** |
| 6 | **Phase 10（事件系统）** | 建议降级/不做；若你要求照做，我照做 |

---

## 附：基线实测

```
命令：python -m pytest tests -q --junitxml=D:/NPCSidekick/runtime_audit_junit.xml
系统 python：3.13.14（C:\Users\Administrator\AppData\Local\Microsoft\WindowsApps\python）

TOTAL tests=847  failures=0  errors=0  skipped=0
time=49.5s
```

**证据链说明**：终端摘要在收尾时被 safe-delete shim 吞掉（输出只到 `[100%]` 后的 bulk-confirm 告警），
故取数一律走 junitxml 解析 —— 本次复现了项目既有的这条老坑，**不是测试失败**。

**与历史口径的对照**（此前出现过四个不同数字，本次已定准）：

| 来源 | 数字 | 判定 |
|---|---|---|
| 会话记忆 | 796 | 过期（任务书 #06 提交前） |
| 任务书 #06 提交信息 | 802 | 过期（#06 之后又加了测试） |
| `ARCHITECTURE.md` / `README.md` / `PROJECT_DELIVERY.md` | 841 | 过期（缺 2026-09-10 审查跟进的 6 例） |
| `docs/全面审查报告_2026-09-10.md` §6 | 847 | **一致 ✅** |
| **本次实测（junitxml）** | **847** | **权威基线** |

> 注：`tests/test_safety_gate.py` 的未提交加固改动已包含在 847 内（全绿）。若回滚该改动，
> 基线数字不变，但该用例在"环境里有 key"的机器上会变成假绿/假红 —— 与测的东西无关，建议保留。

### 本次未验证 / 需运行时确认

- **未跑真实联机端到端**（缺 GTA mod 侧），属外部依赖。
- **未做 30 分钟长跑**（brief 验收 J 项）—— 需运行时验证记忆/向量锚/归档文件的增长。
- **重启一致性未实测**（Q1/Q8 的"每 NPC 一份 world 副本"是否存在实际状态回退）—— 需构造
  多 NPC + 重启用例确认。**这是 TD2 的关键验证项，建议排在 Phase 2 之前做。**
- 本报告只读；唯一新增文件是本文档本身。

### 与既有审计文档的关系

| 文档 | 视角 | 关系 |
|---|---|---|
| `docs/全面审查报告_2026-09-10.md` | **全项目健康体检**（安全合规 / 测试基线 / 文档一致性 / 前端），含行动顺序 | 互补。它答"项目健不健康"，本文答"brief 要的 12 个 Phase 哪些已有" |
| **本文档** | **运行时能力审计**（世界状态 / 目标 / 决策 / 后果 / 记忆 / 人格 / 关系 / 事件） | 不重复其结论，仅引用 |

*本报告为只读审计，未修改任何生产代码。*

---

## 更新（2026-09-15 晚）

> 本文档是**快照**：上面 §0.3 与附录的 `847 / 49.5s` 是当时的实测数据，**保留原样存证，不回溯修改**；
> 现状以下表为准。

| 项 | 快照值（正文） | 现状（2026-09-15 晚实测） |
|---|---|---|
| 全量基线 | `847 passed / 49.5s` | **`857 passed / 0 failed / 0 errors / 0 skipped / 44.1s`**（junitxml 独立解析） |
| §9 第 5 问的 `M tests/test_safety_gate.py` | 未提交遗留 | ✅ 已提交 `d69c726`；剩 `?? CLAUDE.zh.md` / `?? npc/store_godot/` 待拍板 |
| §8 推荐顺序的"干净基线" | 建议 | ✅ 已完成（含下列 T-01） |
| §9 第 1 问（文档落点） | 待拍板 | ✅ **已拍板：并入 `docs/NPC大脑架构.md` 编号章节** → 已落 **§29**（能力边界与三条断层）+ **§30**（演进路线与待拍板）。本文档自此定位为**证据快照**，结论以 §29/§30 为准 |
| §9 第 2/3/4/6 问（可观测粒度 / village 形态 / 世界权威 / Phase 10） | 待拍板 | ⬜ 仍未定，见 `NPC大脑架构.md §30.3` |
| §3 / §5 的 Phase 12 Benchmark | 「❌ 真缺」（理由：`npc/benchmark.py` 是 90 行 print 脚本） | **口径不变**。补实测：该脚本实测 91 行、走 `run_task` 路径、**零测试覆盖**（`grep -rl benchmark tests/` 为空）→ P-11 应表述为"在该脚本之上指标化"，不是从零 |

### T-01 修复摘要（本文档未收录该编号，属 `待办与缺口清单_20260915.md` §3.1 的发现）

`_plan_steps` 对非 `rest`/`say` 项一律走 gather 链（`resource = item["resource"]`）→ craft / 自定义动作 /
缺 resource 的 gather 全部 `KeyError`。实测严重度**高于**原描述：异常冒到 `server._tick_loop:253` 的
`except` 会吞掉**整帧**（该帧落盘、反思、管家、黎明整理、`reap_zombies` 全部跳过），且**玩家单同样必炸**
（`_task_from_b2` 按清单参数产得出 `{"action":"craft","recipe":…}` → `pending_task` → 同一处）。

修法：① `_plan_steps` 变总函数（认不出 → `None`，不抛）② 计划失败也进冷却（否则每帧重选同一项 →
每帧写一条记忆 = 记忆卡刷屏）③ 失败文案按原因分开。回归锚
`tests/test_scheduler.py::TestPlanStepsIsTotal` 10 例（**先红后绿**：红时 10 例全停在
`npc/scheduler.py:163 KeyError`）。提交 `0b6dc49`。**T-04（craft 只在 `run_task`、routine 永不 craft）未动**，
仍属后续排期。
