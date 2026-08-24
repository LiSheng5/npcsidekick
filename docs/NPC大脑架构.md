# NPC 大脑架构（三角色 · 记忆三阶段 · 防篡改 · 语音）

> 最后更新：2026-08-25。记录 NPC 层（`npc/` 目录）的完整架构，覆盖：三角色决策、落账口、记忆系统三阶段（反思/关联/遗忘）、防篡改+沙箱分级、审批策略配置化、语音系统、模型可插拔、上下文压缩、Codex 设计吸收（§9）、六项强化：动态资源词典/中文分词/多世界隔离/SSE 推送/清单解析/接入 SDK（§14）、调试台三件套（§15）。

## 1. 三角色架构

普通 NPC 的"说/想/做"拆成三个角色（模块化双角色 + 按场景触发审查）：

| 角色 | 职责 | 实现 |
|---|---|---|
| **B1 对话** | LLM 生成回复文本（表象） | `NPC.talk()` 的 LLM 路径 |
| **B2 编译** | 把玩家诉求编译成任务单（唯一落账口） | `npc/reviewer.py::compile_task()` |
| **A 审查** | 合规 + 可行性拦截（按场景触发） | `npc/reviewer.py::review_task()` / `review_dialogue()` |

**落账口铁律**：承诺成立 = 指令通过 A 审查并被 B2 推进活动队列（`NPC.book()`），不是嘴上那番话。LLM 答应去办事但任务未落账 → A 审查拦截重生成。

**按场景触发**：`should_review("command")` 接单必审；`should_review("dialogue")` 只在回复含承诺词 / 高风险话题时深审。

参考：论文《The LLM Proposes, the Executive Disposes》(arXiv 2608.04066) —— 确定性执行器拥有承诺存储，LLM 只能提 proposal。

## 2. 记忆系统三阶段（轻量海马体）

| 阶段 | 文件 | 能力 |
|---|---|---|
| ① 反思归纳 | `npc.py::maybe_reflect()` | 未反思记忆重要性之和 ≥ 12 → LLM/规则归纳成高层结论（`category=reflection`） |
| ② 关联检索 | `npc/memory.py` | 同义词召回（柴↔木材）+ 一跳关联（HippoRAG 思想轻量版，不依赖 embedding）；相关度打分用 jieba 分词（`_tokenize()`，未装 jieba 自动回退空格切分） |
| ③ 遗忘合并 | `npc/memory.py::consolidate()` | 重复主题合并成一条 + 半衰期(72h)弱旧修剪；反思/高重要度(≥8)保留 |

触发：server tick 循环定期跑（反思每 20 帧 ≈60s，合并每 60 帧 ≈3min）。

参考：Generative Agents（反思）、HippoRAG 2（关联）、hippo-memory（遗忘合并）。

## 3. 防篡改 + 沙箱权限分级

**防篡改基础**（不变）:
- LLM 在 NPC 路径**零文件能力**（B1 只出文本，B2 只出白名单游戏动作）
- `review_task()` 动作白名单 `ALLOWED_TASK_ACTIONS`（gather/craft/deliver/move）+ 禁止 `file/path/write/read/exec/...` 字段
- 服务器仅绑 `127.0.0.1` + Origin 校验
- 测试证明：`tests/test_security.py`

**沙箱权限分级**（2026-08-21 对照 Codex 三级权限模型: read-only / workspace-write / danger-full-access）:

| 等级 | Codex 对照 | 动作 | 说明 |
|---|---|---|---|
| L1 | read-only | `gather` 采资源 | 只影响自己背包 → 规则直审 |
| L2 | workspace-write | `craft` / `move` | 改变自身持有/位置 → 规则直审 |
| L3 | danger-full-access | `deliver` 交付 | 改变玩家背包+世界交付量 → **A 深度审查** |

- `ACTION_LEVELS` 定义分级；`action_level()` 查询（未知动作=最高级拒绝）
- **L3 深审**（`needs_deep_review` + `review_dialogue`）: 回复含交付承诺词（给你/送到/交到你手上）且在 auto 策略下，落账任务必须 `action=="deliver"`；落账是采集却承诺交付 → 拦截"承诺交付但落账任务不是交付动作"
- **审批策略可配置**（`APPROVAL_POLICY_VALUES`）: `auto`(默认,安全) / `on-failure`(宽松) / `never`(仅测试)；由环境变量 `NPC_APPROVAL_POLICY` 配置
- 承诺未落账仍拦截（核心防线，与审批策略无关）

### 3.1 事件协议化（Codex core/界面协议化）

> 2026-08-21 改造，对照 Codex 的「界面协议化」核心思想：**界面只消费事件，不做状态差分** ——
> 游戏端/网页端不再自己 diff log/delivered，而是订阅统一事件流驱动气泡/走路/交付动画。

**事件端点** `/api/events?since=<游标>`:
- 把 `world.log` 文本行解析为结构化事件（`server.py::_parse_log_line`），正则覆盖全部 5 种日志格式
- 事件类型:

| type | 来源日志示例 | 字段 |
|---|---|---|
| `say` | 苍 说: 你好 | npc, text |
| `move` | 苍 前往 森林 | npc, dest |
| `gather` | 苍 采集了 1 个木材 | npc, resource |
| `craft` | 苍 制作了 木石工具 | npc, product |
| `deliver` | 苍 将 木材 交给了 主角 | npc, resource, to |

**游标增量**（Codex 式增量订阅）:
- 客户端持有 `log_count` 作游标，下次传 `since=log_count` 只拿新增事件 → 天然断点续传
- 游标边界自动收拢（负数 → 0，超界 → 当前长度），不崩溃
- 返回 `{events, log_count, tick, delivered}`，`delivered` 快照供交付动画直接取用

**示例客户端**:
- `engine-clients/common/python_client.py`：`events(since)` + `poll_events()` 轮询示例
- `engine-clients/common/fetch.js`：`pollEvents(since)` + `pollLoop()` 轮询示例

**测试**: `tests/test_npc_server.py::TestEventsApi`（空日志 / 推帧结构化 / 游标增量 / 负数钳制）

### 3.2 approval 策略配置化（Codex /approvals 对照）

> 2026-08-21 改造，对照 Codex 的 `/approvals` 三态机制：审批从「全局二元档位」升级为「动作级三态 + 运行时调整」。

**三态决定**（`reviewer.py::approve_action`）:

| 决定 | 含义 | 对照 Codex |
|---|---|---|
| `allow` | 自动放行（白名单/可行性底线仍生效） | always allow |
| `ask` | 需要 A 审查（+承诺落账深审） | ask each time |
| `deny` | 直接拒绝，连审查都不走、不落账 | always deny |

**动作级默认策略**（对照沙箱分级）:

| 动作 | 等级 | 默认 | 理由 |
|---|---|---|---|
| `gather` | L1 read-only | allow | 只影响自己背包 |
| `craft` / `move` | L2 workspace-write | ask | 改变自身持有/位置 |
| `deliver` | L3 danger-full-access | ask | 改变玩家/世界 + 承诺落账深审 |

**运行时调整**（对齐 Codex /approvals）:
- 环境变量 `NPC_APPROVAL_<ACTION>=allow|ask|deny`（如 `NPC_APPROVAL_DELIVER=deny` 禁用交付）
- API `/api/approval`：GET 查当前生效表；POST `{action, decision}` 会话内热更新
- **按 NPC 粒度**：`/api/approval` 带 `npc_id` → 只改该 NPC（苍可禁交付、阿黎照常），每个 NPC 持自己的 `approval_overrides` 表
- 优先级：**实例覆盖(NPC) > 会话内动态覆盖 > 环境变量 > 默认表**；全局档位 never/on-failure 时全 allow

**测试**: `tests/test_reviewer.py::TestApproveAction` / `TestTaskCommandDeny`（deny 不落账）+ `tests/test_npc_server.py::TestApprovalApi`

### 3.3 动作清单化（manifest，跨游戏动作集）

> 2026-08-21 改造：动作集不再写死进大脑。每游戏声明一份「动作清单 manifest」，大脑的白名单 / 审批表 / 影响等级全部由清单驱动 —— **换游戏时大脑零改动**。

**为什么**：原来 `ALLOWED_TASK_ACTIONS = ("gather", "craft", "deliver", "move")` 写死，换游戏动作不同（巡逻/攻击/卖货…）大脑直接不认。清单化后，动作集 = 该游戏清单内容，`reviewer.py` 只认清单里的动作。

**清单字段**（`reviewer.py::load_manifest`）:

| 字段 | 取值 | 说明 |
|---|---|---|
| `tier` | 1 / 2 / 3 | 影响等级（~ 沙箱 read-only / workspace-write / danger-full-access） |
| `approval` | allow / ask / deny | 该动作默认审批（可被 env / API 覆盖） |
| `params` | 字符串数组 | 参数名，供校验/描述用 |
| `desc` | 字符串 | 人话说明 |

**默认清单**（不加载 = 本游戏动作，行为与原版完全一致）: `gather`(L1/allow) · `craft`(L2/ask) · `move`(L2/ask) · `deliver`(L3/ask)

**三种接入方式**（三选一）:
1. 启动参数：`python -m npc.server --manifest examples/actions.json`
2. 环境变量：`NPC_MANIFEST=examples/actions.json`
3. 运行时热更：`POST /api/manifest {"actions": {...}}`；`GET /api/manifest` 查当前生效清单；`{"reset": true}` 恢复默认

**安全校验**（`load_manifest`）: `tier` 必须 1/2/3、`approval` 必须 allow/ask/deny；非法清单抛错且**原清单保持不变**（不会半套生效）。A 审查只认清单里的动作，防文件/路径注入的兜底（`_FORBIDDEN_TASK_TOKENS`）不受影响。

**每游戏接入步骤**（开箱即用）:
1. 复制 `examples/actions.json` → 改成自己游戏的动作（动作名 + tier + approval + params + desc）
2. 启动时加载（`--manifest` / `NPC_MANIFEST` / `POST /api/manifest` 三选一）
3. 游戏端写 `TaskExecutor`：把语义任务 `{action, params}` 翻译成引擎调用，执行结果经 `/api/events` 回传大脑（大脑据此更新记忆、继续下一步规划）

**换游戏示例（塔防）**:
```json
{"actions": {
  "patrol": {"tier": 2, "approval": "ask",   "params": ["waypoint"], "desc": "巡逻"},
  "attack": {"tier": 3, "approval": "ask",   "params": ["target"],   "desc": "攻击敌人"},
  "repair": {"tier": 2, "approval": "allow", "params": ["tower"],    "desc": "修塔"}
}}
```

**测试**: 全量 `pytest` 519 passed；清单加载/校验/重置冒烟见 `reviewer.py::load_manifest`，端点见 `server.py::/api/manifest`。

## 4. 语音系统（edge-tts）

- `npc/tts.py`：免费微软 TTS 包装；音色按 `persona["voice"]` 或默认表（苍=沉稳男声 / 阿黎=清亮女声）
- `/api/talk` 带 `voice:true` → 返回 `{reply, audio(base64 mp3)}`
- `/api/tts` 独立端点：游戏可给任意文本配音（本地对话表 / 头顶气泡）
- 5s 合成超时，失败/未装 → 纯文本保底，不卡对话
- **游戏端**（`scripts/npc/DialogueUI.gd`）：请求带 `voice:true` → 解码 base64 → `AudioStreamMP3` → 播放；调试打印只显示 `body_len`（避免刷 base64）

## 5. 模型可插拔

- 模型名由环境变量配置：`NPC_DIALOGUE_MODEL` / `NPC_REVIEW_MODEL`（默认 `deepseek-v4-flash`；按模型名自动路由 base_url，deepseek → DeepSeek、glm → 智谱）
- **双槽修复（2026-08-23）**：`NPC._get_llm(role)` 此前单槽缓存——第一个调用者的
  模型被 dialogue/review 两角色共用，`NPC_REVIEW_MODEL` 配了也不生效。现按角色
  各缓存一份（B1 对话用对话档；A 审查与反思归纳共用 review 档），两层可以真的配不同模型。
  测试：`tests/test_llm_roles.py`
- **现状（2026-08-21）**：本地模型（Ollama）与智谱已弃用；默认走 DeepSeek，
  需要有效 key（环境变量 `DEEPSEEK_API_KEY` 或项目根 `api_key.txt`）才走 LLM，否则自动规则模式（本地话术）即时响应
- 未来如需接本地模型，见 `docs/本地模型.md`（已标注弃用，仅供参考）

## 6. 测试

```bash
python -B -m pytest tests/test_npc.py tests/test_npc_memory.py tests/test_npc_server.py   tests/test_npc_tools.py tests/test_npc_world.py tests/test_reviewer.py   tests/test_reflection.py tests/test_association.py tests/test_consolidate.py   tests/test_security.py tests/test_tts.py   tests/test_resource_lexicon.py tests/test_memory_tokenize.py tests/test_server_multiworld.py -q -p no:cacheprovider
# → 全量套件 588 passed（2026-08-23，`python -m pytest tests -q`；上行 NPC 子集为其中一部分）
```

## 7. 文件清单

| 文件 | 说明 |
|---|---|
| `npc/reviewer.py` | B2 编译 + A 审查（白名单 + 沙箱分级 + 审批策略） |
| `npc/tts.py` | edge-tts 语音合成 |
| `npc/npc.py` | 三角色接入 + 落账口 `book()` + 反思 `maybe_reflect()` |
| `npc/memory.py` | 同义召回 + 一跳关联 + 遗忘合并 + jieba 分词（可选依赖） |
| `npc/server.py` | `/api/talk`(voice) + `/api/tts` + `/api/events`(事件协议) + `/api/events/stream`(SSE 推送) + `/api/approval`(审批策略) + `/api/manifest`(动作清单) + 多世界隔离(`--world-id`，409 守卫) + 后台反思/合并触发 |
| `agent/memory/compressor.py` | 上下文压缩（交接单式 + 内容分级，见 §8） |
| `agent/memory/short_term.py` | 短时记忆 + 入口截流（超长消息保留头尾） |
| `examples/actions.json` | 游戏动作清单模板（每游戏一份：`actions` 动作表 + `resources` 资源别名词典；见 §3.3 / §14） |

## 8. 上下文压缩（对照 Codex Context Compaction）

> 2026-08-21 改造，依据 OpenAI Codex Harness 开源（github.com/openai/codex, Apache-2.0）的上下文管理设计。
> 背景：Codex 官方数据 —— 仅改 harness 的「保留推理 + 上下文压缩」策略，同一模型 ARC-AGI-3 从 13.3% → 38.3%，输出 token 省 6 倍。社区指出的最大坑是「电话游戏退化」（反复压缩 → 每次丢一点中间判断）与「工具输出占 79% 却 0% 存活」。

### 8.1 四类改进

| 改进 | 文件 | 说明 |
|---|---|---|
| ① 入口截流 | `short_term.py::_entry_truncate` + `compressor.py::_middle_truncate` | 单条超长消息（工具输出/大文本）进入历史时就保留头尾、裁掉中间，避免无关细节吃满窗口 |
| ② 结构化交接单 | `compressor.py::COMPRESSION_SYSTEM_PROMPT` | 压缩结果分节（目标 / 已做 / **决策** / 待办 / 事实），而非平铺要点 —— 显式保住 NPC 对玩家的承诺 |
| ③ 保留推理 | `compressor.py::compress()` | 压缩前注入 `current_task` / `player_request` / `npc_commitment` 状态给 LLM，防止「电话游戏退化」 |
| ④ 内容分级 | `compressor.py` 提取逻辑 | 解析时跳过 `## 分节` 标题；删除时按消息价值分层，关键决策已入长时记忆后才删旧消息 |

### 8.2 与记忆三阶段的关系

- 本压缩器属于**短时记忆 → 长时记忆**的迁移通道（`MemoryManager.maybe_compress()` 触发，token 超阈值 + 消息数达标）
- 与 §2 记忆三阶段互补：三阶段管**单条记忆的归纳/召回/遗忘**，本节管**对话历史的整体压缩保真**
- 对 NPC 场景的关键价值：玩家请求 NPC 帮忙的「承诺」会被归入交接单的「决策」节入库，配合三角色架构（B2 编译 → `book()` 落账），承诺跨压缩不丢失

### 8.3 触发与测试

- 触发：`MemoryManager.maybe_compress()` —— `estimated_tokens > COMPRESSION_TOKEN_THRESHOLD` 且消息数 ≥ `COMPRESSION_MIN_MESSAGES`
- 冒烟验证：入口截流生效 / 交接单 5 分节 / NPC 承诺入库、分节标题不误存 / 状态注入 prompt 均通过
- 全套测试：`504 passed`（2026-08-21）

## 9. Codex 开源参考与吸收清单

> 2026-08-21。本项目在 NPC 层借鉴 OpenAI Codex 开源仓库（github.com/openai/codex）的设计，
> 吸收点分散在 §3 / §3.1 / §3.2 / §8，本节做总览。

**开源协议：Apache-2.0（不是 MIT）**
- Codex CLI / Harness / SDK（TypeScript/Python）在 Apache-2.0 下开源：允许复制、修改、合并、商用、再分发
- 义务：保留 LICENSE 文件；修改过的文件加显著改动声明（NOTICE）
- 注意：开源的是客户端 / Harness 代码，背后模型（GPT-5-Codex 等）与云服务**不开源**

**吸收点总览**

| # | Codex 设计 | 项目落地 | 章节 |
|---|---|---|---|
| 1 | Context Compaction（保留推理 + 上下文压缩） | 交接单式压缩 + 入口截流 + 保留推理 + 内容分级 | §8 |
| 2 | 沙箱三级权限（read-only / workspace-write / danger-full-access） | 动作分级 L1/L2/L3 + 白名单 + 承诺落账深审 | §3 |
| 3 | core/界面协议化（界面只消费事件） | `/api/events` 结构化事件流 + since 游标增量 | §3.1 |
| 4 | approval 策略（/approvals 三态 + 运行时调整） | allow/ask/deny 三态 + 动作级表 + 按 NPC 粒度 + 环境变量/API 配置 | §3.2 |

**来源**：github.com/openai/codex（Apache-2.0）；官方博客 Codex Harness 开源公告。

## 10. 思考可视化（thinking_text 透传链路）

> 2026-08-22。玩家在对话框选"直出/思考/深度"档位 → 模型思考内容随回复一起回到游戏端，
> 以折叠标签展示。规则回退（本地话术）时无思考内容，整个区域自动隐藏。

**请求-响应链路**

```
游戏 DialogueUI(档位按钮 off/high/max)
  → POST /api/talk {message, npc_id, thinking}
  → npc.py talk(reasoning=) → _chat_with_review() 返回 (回复, 思考)
  → LLMClient.chat(reasoning_effort=) → OpenAIProvider._thinking_body() 按通道翻译
  → 响应 message.reasoning 捕获 → LLMResponse.reasoning
  → /api/talk 响应 {reply, thinking_text(截断2000)}
  → DialogueUI._show_thinking_text() 折叠面板(默认收起,点击展开)
```

**_thinking_body 各家翻译（关键坑）**

| 通道 | 请求格式 | 思考回传字段 |
|---|---|---|
| OpenRouter | `reasoning: {effort: low/medium/high, exclude: false}` | `message.reasoning`（经 model_extra 兜底） |
| DeepSeek/OpenAI | `thinking: {type: enabled, reasoning_effort: 值}` | `message.reasoning_content` |
| Zhipu GLM(<5.2) | `thinking: {type: enabled}`（仅开关） | — |

- OpenRouter **必须显式 `exclude: false`** 才回传思考，且 effort 只支持 low/medium/high
  ——游戏档位 `max` 归一为 `high`；`off` 不发参数（无显式关闭）
- provider 捕获顺序：`message.reasoning` → `reasoning_content` → `model_extra.reasoning`，
  三通道通吃

**游戏端要点**

- 思考档位条仅在 API 对话模式显示（`_build_thinking_bar`），三态切换直出/思考/深度
- 折叠标签 `💭 思考过程 ▸/▾`：默认收起不打断阅读，玩家点开看推理
- **超时分档**：直出 5s；思考/深度 30s（`API_TIMEOUT_THINKING`）——
  实测开思考后 ox-alpha 响应 8~9s，固定 5s 会永远超时回落，思考内容到不了 UI
- 规则回退/模型未思考（简单问题跳过推理）→ thinking_text 为空 → 标签隐藏

**验证**（2026-08-22）

- pytest `522 passed`
- 真实 API：thinking=high/max 请求返回 thinking_text；鸡兔同笼类推理问题稳定出思考，
  简单问题（1+1）模型跳过思考为空属正常
- 游戏内端到端：临时 autoload 驱动真实请求 → 思考内容 9.2s 到达 → 折叠面板展开截图
  `assets/previews/思考可视化验证.png`；headless 跑帧零 SCRIPT ERROR

**文件清单**

- `agent/providers/openai_provider.py` — _thinking_body 三通道翻译 + reasoning 捕获
- `agent/llm/client.py` — LLMResponse.reasoning 字段
- `npc/npc.py` — talk()/_chat_with_review() 透传思考
- `npc/server.py` — /api/talk 响应 thinking_text
- 游戏端 `scripts/npc/DialogueUI.gd` — 档位条 + 折叠面板 + 分档超时

## 11. 耐力系统 + 做事权重（动态 routine）

> 2026-08-22。NPC 行为从"静态权重随机"升级为"生理×昼夜×库存"动态决策；
> 玩家侧同步实装即时耐力池。参数全 [PLACEHOLDER]，后续按真实人类生理结构（MET 功耗/氧债恢复）再调比例。

**大脑侧**

- actor 槽位新增 `stamina`（0~100，旧档无键自动补满，向后兼容）
- 动作消耗在 `world.apply_action` 统一收口（自主循环+LLM 工具全路径生效）：
  move −4 / gather −10 / craft −6 / deliver −2 / say −0；失败动作不扣；下限 0
- rest 步骤每 tick +30（scheduler 收口，rest 不走 apply_action）
- **动态权重** `_dynamic_weight`：persona 静态 weight × 因子，纯数值零 LLM

| 因子 | 规则 |
|---|---|
| 耐力 | rest：<30 力竭 ×8、<60 疲惫 ×3；gather/craft：<30 ×0.1、<60 ×0.5 |
| 昼夜 | 22:00~06:00 夜间：rest ×2、gather/craft ×0.4 |
| 库存缺口 | gather：delivered<5 ×1.6（缺料优先）；>20 ×0.5（满仓降权） |

- 昼夜推算：`game_hour(world)` = (8 + tick//60) % 24 —— tick 3s，60 tick=3 分钟=1 游戏小时，
  与游戏端 WorldTime 同构（大脑侧自洽节律，不与游戏端强同步）
- `observe()` 体力感知：力竭/疲惫时注入"你浑身发沉…"——**对话层一致性**：苍累了会自己说
- `/api/state` 透出 `stamina`（游戏端可视化 NPC 体力用）

**玩家侧（游戏端）**

- 即时耐力池 `stamina`（区别于只涨的成长值 `endurance`，两者并存）
- 奔跑 8/s、跳跃单次 10；停止消耗 1.5s（喘气期）后开始恢复：静止 14/s、慢走 7/s
- 耗尽（<5）禁跑自动落回走路；无数值系统的测试场景不受限
- HUD "耐"条改显实时 stamina

**验证**：pytest `542 passed`（新增 20：扣减/恢复/昼夜/权重统计/旧档兼容）；
游戏端 headless 零 SCRIPT ERROR + autoload 数值自检 5/5。

**文件**：`npc/world.py`、`npc/scheduler.py`、`npc/server.py`（state 透出）、
游戏端 `player_stats.gd` / `PlayerStatsSystem.gd` / `player.gd` / `StatusBarsHUD.gd`

## 12. 游戏→大脑世界同步（context 直通）

> 2026-08-22。补全"世界真相单向"缺口：此前大脑活在文本世界，游戏端真实天气/时间/位置进不来
> （game_hour 靠数 tick 猜、天气根本不知道）。同时是 GTA MOD 前置改造 #2（见 adapters/gta_bridge.md）。

**链路**

```
游戏 DialogueUI._collect_context()   → 请求体 {"context": {weather, game_hour, player_pos}}
→ server /api/talk._apply_context()  → 直写共享 world 扩展键(_weather/_game_hour/_player_pos)
→ 权重: game_hour() 真实时间优先; _dynamic_weight 天气因子
→ 对话: observe() 注入"天上下着雨…" — 村民聊天下雨会说雨
```

**规则**

- 天气权重（叠加在 §11 耐力/昼夜之上）：rain → gather×0.5、rest×1.5（躲雨）；
  festival → gather×0.6、rest×0.7（节庆少干活少躺着）
- `game_hour`：同步的真实时间优先，无同步回退 tick 自推（60 tick=1小时近似）
- 未同步时全部静默——纯文本世界自洽，零回归
- 坏值容错：game_hour 传字符串静默忽略；context 为 None/{} 不写任何键

**验证**：pytest `555 passed`（新增 13：context 写入/容错/时间优先/天气权重/感知）；
真实 API 端到端——context 带 weather=rain，苍回复"下雨了。雨点砸在树叶上，噼啪响"
并基于雨天常识建议（别进林子、湿柴烧不得）。

**文件**：`npc/server.py`（_apply_context）、`npc/world.py`（observe 感知）、
`npc/scheduler.py`（时间优先+天气权重）、游戏端 `WorldEvents.gd` / `WorldTime.gd`
（公开 get_active_event/current_hour）、`DialogueUI.gd`（_collect_context）

## 13. 动态注册（NPC 随刷随出）

> 2026-08-22。GTA 前置改造 #1（见 adapters/gta_bridge.md）：ped 是运行时随刷随出的，
> persona 不能只靠服务器启动时读 JSON。灵魂附体模型的"魂"从预装变成即插即用。

**端点**

```
POST /api/npc/register    body: {persona: {id 必填, name/identity/voice/rules... 可选},
                                 persistent: false(默认流民)/true(常驻),
                                 use_llm: true/false(规则模式)}
POST /api/npc/unregister  body: {npc_id}   — ped despawn 时调用
```

**身份分层落地**

| 层 | persistent | save() | despawn 后 |
|---|---|---|---|
| 流民 ephemeral | false | 跳过（RAM-only） | 反注册即忘（陌生人聊一次就忘） |
| 常驻 locals | true | 反注册时落盘 | 重注册读记忆卡续前缘（"又来剪头了？"） |
| cast 静态角色（cang/ali） | — | tick 转换点落盘 | 不可反注册（404，脚本世界的主人） |

**要点**

- **幂等**：重复注册返回 `existed=true` 不重建 — ped 反复进出同步半径安全
- **人设兜底**：最少只需 `id`，缺省字段按路人填；routine 默认空（流民不自主干活，
  身体归游戏原生 AI — 平时沉睡，对话才唤醒）
- **id 白名单** `[A-Za-z0-9_-]{1,32}`：store 文件名由 id 拼出，防路径穿越
- **上限** `MAX_DYNAMIC_NPCS=200`：超了 429（防失控；GTA 同步半径内通常 <30）
- **流民跳过反思/遗忘整理**：RAM-only 记忆 despawn 即忘，周期性 LLM 反思纯属浪费
- **tick 竞态防线**：`tick_round` 对"actor 槽在而 NPC 已反注册"跳过不炸
- 常驻层 persistent=true 且记忆卡已存在 → 走 `NPC.load` 续前缘，人设仍以注册传入为准

**验证**：pytest `569 passed`（新增 14：幂等/白名单/上限/流民不落盘/常驻落盘续前缘/
cast 拒绝反注册/再注册全新身份/tick 孤儿槽防御）。真实 API 端到端：注册格罗夫街小哥 →
按人设对话（街头俚语 + 只知自己世界的三条路 + 接地不编造 + 感知已同步的雨天 context）→
重复注册 existed=true → 反注册。

**文件**：`npc/npc.py`（ephemeral 模式）、`npc/scheduler.py`（tick 防竞态）、
`npc/server.py`（register/unregister 端点 + 流民跳反思整理）

## 14. 六项强化（2026-08-23）

> 对照外部评审建议逐项落地；全量测试 **588 passed**。客户端 API 完全兼容（GTA mod / Godot 均无需改动），重启大脑服务器 bat 生效。

| # | 强化 | 落点 | 说明 |
|---|---|---|---|
| 1 | 动态资源词典 | `reviewer.py::load_resource_lexicon_from_world()` | B2 编译的资源识别不再写死：世界 `locations` 里出现的资源自动进词典（整表替换语义，空世界回退 `DEFAULT_RESOURCE_ALIASES`）；manifest 可带 `resources` 段补充别名。游戏加新资源只改世界 JSON，零代码 |
| 2 | 中文分词 | `memory.py::_tokenize()` | `_relevance_score` 相关度打分改 jieba 分词（jieba 为可选依赖，未装自动回退空格切分）。ChromaDB 向量检索留作 P1（`agent/memory/vector_store.py` 已有可借鉴封装） |
| 3 | 多世界隔离 | `server.py --port / --world-id / --store-dir` | 一台机器多游戏共用大脑：GTA 跑 8765、Godot 跑 8766，各自独立记忆卡目录（`npc/store_<world_id>`）；请求 `world_id` 不匹配 → HTTP 409 响亮失败，杜绝记忆串世界。旧客户端不带 world_id 完全兼容 |
| 4 | 清单解析正式化 | `reviewer.py::parse_manifest_doc()` | 修复隐藏 bug：`--manifest` 原来把整个文档直接喂给 `load_manifest()`，校验失败被静默吞掉、回退默认清单——等于传了清单也白传。现在同时接受 `{"actions":...,"resources":...}` 包装格式与旧裸动作表 |
| 5 | SSE 推送通道 | `GET /api/events/stream` | FastAPI `StreamingResponse` 实现，1s 扫描粒度 + 每 15 个空闲循环发 `: ping` 心跳防断连；零新依赖。2 秒轮询 `/api/events` 永久保留兼容，客户端可渐进迁移 |
| 6 | 引擎接入 SDK 补课 | `engine-clients/common/PROTOCOL.md` 等 | 权威契约文档：铁律（LLM 单通道严格串行）/ 端点契约与错误语义表（400/404/409/503）/ 六条实战客户端模式（超时竞争法、请求序号防串台、本地兜底话术、WinForms IME 三坑等）/ 零代码接入清单；新增硬化骨架示范 `unity/NPCSidekickClient.cs`（旧 stub 五件保留未动） |

**配套测试**：`tests/test_resource_lexicon.py`（词典收集/回退/恢复）、`tests/test_memory_tokenize.py`（分词计分）、
`tests/test_server_multiworld.py`(409 守卫/store 目录)——均带 autouse 夹具恢复默认词典，防用例间污染。
**部署**：重启 `启动NPC服务器_GTA.bat` 即生效。

## 15. 调试台三件套（2026-08-23）

> 近期优化第一批落地：Web 调查台接通真后端 + 版本握手 + 观测端点。
> 全量测试 **601 passed**（新增 13，`tests/test_server_console.py`）。客户端 API 完全向后兼容。

| 件 | 端点/落点 | 说明 |
|---|---|---|
| 调试台页面 | `web/static/npc.html`（服务器 `/npc.html` 本尊托管，同源零配置） | mock 全部移除改走真后端：对话（思考折叠）/世界状态（耐力条+动态背包+交付量）/**记忆卡整卡编辑回写**/审批策略热调（select 即改即生效）/SSE 时间线（断流自动退轮询）/world_id 输入（localStorage 持久）。**不开游戏即可调 prompt——串行约束下最省时间的工具** |
| 记忆回写 | `POST /api/memory` | `{npc_id, entries[]}` 整表替换 + 落盘；content 截 2000 字、importance 夹紧 0-9、非法条目整体 400 不半套生效。语义=调试台整卡编辑，生产游戏端勿用 |
| 版本握手 | `GET /api/version` | `{name, version, world_id, features{events_sse, multi_world, jieba_tokenize, tts, memory_edit…}}`；客户端启动探测一次按特性降级，以后加功能挂账即可、老客户端不炸 |
| 观测端点 | `GET /api/stats` | talk{total/errors/延迟 total·max·last/llm vs rules}、tts/task/memory_save 计数、sse_clients(_peak)、uptime_sec/tick/mode/npcs。零侵入埋点（收口在端点处）；内存态重启清零——先攒基线，时序库等真需要再上 |

**契约固化**（`tests/test_server_console.py`）：校验失败不计入调用统计；记忆回写走盘可被
`NPC.load` 读回；world 守卫覆盖新端点；静态页由服务器本尊托管（`/` 与 `/npc.html`）。
协议侧已同步 `engine-clients/common/PROTOCOL.md` §2.7~2.9 + 自带调试台小节。

## 16. 数据驱动世界状态（HUD 面板声明，2026-08-23）

> 用户实测反馈：调试台把旧石器的耐力/背包/交付焊死在页面上 —— GTA 的阿曼达
> "没有耐力、没有背包、也不交付主角"。解法不是重做 web，而是**界面跟着世界契约走**：
> 换游戏时页面永远零改动，改的是世界 JSON 的一行声明。

**契约**：世界 JSON 可选键 `"_hud": {"panels": ["position", "activity", …]}`。

- 缺省 = 默认全面板（position/activity/stamina/inventory/delivered）—— 存量世界与
  旧石器世界零回归（行为不变）
- 服务端 `/api/state` 新增 `panels[]` 透传（`server.py::_DEFAULT_HUD_PANELS`）；
  **actors 数据字段照旧完整** —— 面板只是显示层契约，游戏端镜像照读全量字段
- 调试台按 panels 渲染（未知面板名静默忽略 = 向前兼容）；GTA 世界声明
  `["position","activity"]`，阿曼达不再显示耐力条/空背包/交付量
- **换新游戏 = 世界 JSON 里写一行 `_hud`**，页面零改动

**测试**：`tests/test_world_hud.py`（5 例：默认全开 / 声明优先 / 空声明回退 /
GTA 纯对话世界 / actors 数据完整）。全量 **606 passed**。

## 17. 子代理化：B2 编译 / A 审查升级为 LLM 子代理（2026-08-24）

> 用户需求："B2 和 A 就像子 agent 一样"，参考 GitHub 开源
> [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
> （subagent seam）与 [openai/codex](https://github.com/openai/codex)（custom agents）。
> 此前 B2/A 是纯规则层（词表+白名单），GTA 类纯对话世界没声明资源词典 →
> 规则版 compile_task 永远接不了单 —— 这是 LLM 化的直接动机。

**新增 `npc/subagent.py`（~200 行）**：SubagentSpec（窄人设身份卡）+ SubagentResult
四件套（data/raw/error/stop_reason，对齐 DSH `SubagentResult`）+ run_subagent 运行器。
stop_reason 词表：completed / schema_invalid / refusal / error / no_llm / disabled。

**四条纪律（与 harness/codex 对齐）**：
1. **spawn 新鲜眼** — 每次独立简报（动作清单+词典+输入+回复），绝不继承 B1 对话史；
   A 只看产出不看生成方推理（防确认偏差 — 本项目铁律的同款表述）
2. **结构化契约** — 输出必须解析为 JSON 对象；围栏宽容剥离；失败响亮记账
3. **失败语义分角色** — B2 失败 → 不落账（承诺成立=已落账兜底）；
   A 失败 → 放行（审查不卡对话铁律不变）
4. **窄而专·零递归** — depth=0 由构造保证（纯函数无工具环）

**接入点（npc.py，规则护栏全部原样保留）**：
- `_maybe_book_via_b2()` — 在 B1 生成后、A 审查前出牌。触发闸门=闲聊零调用：
  回复承诺/高风险（与 A 同信号）或输入像派活但词典没接住。产物过与规则路径
  相同的三道门（deny 档 → review_task 白名单+可行性 → 唯一落账口 book()），
  LLM 只提议、代码决定执行（防篡改不变）
- `_a_semantic_block()` — 规则层放行后的语义深审（人设/编造/空口承诺），
  verdict `{block: bool, reason}`；非 bool/失败/关开关 → 一律放行

**观测**：生命周期写世界日志 `[B2]/[A] npc_id …` → server 解析为
`{type:"subagent"}` 事件（SSE/polling 调试台可见）；观察者钩子上报
`/api/stats["subagent"][name]`（total/errors/latency 三指标，无真实调用不计数）；
`/api/version features.subagent_roles = {"b2": bool, "a": bool}` 报告运行时开关状态。

**开关（代码默认关 — 存量行为零回归，631 passed 含 19 新例）**：
```bat
set NPC_SUBAGENT_B2=1   REM 打开 B2 编译子代理
set NPC_SUBAGENT_A=1    REM 打开 A 语义审查子代理
set NPC_SUBAGENT=0      REM 总闸（压过单开关）
```
两个启动 bat 已默认打开。B2/A 都走 review 通道模型（NPC_REVIEW_MODEL 生效）。

**已知边界**：craft 类任务经对话仍不可接（review_task 的 resource 可行性检查
不认识 recipe — 待后续给清单可行性补配方维度）；LLM 串行约束下 B2/A 与对话
共享同一通道，调度队列落地后按 P0>审查>编译>反思排序。

## 18. 记忆分层·冷层归档（2026-08-24）

> 外部建议："热数据(画像)常驻、温数据(锚点)索引、冷数据(daily)归档、本体只读"。
> 对照 [deepseek-harness session 子系统](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.zh.md)
> 后的结论: DSH/Codex 都没有热温冷三层（它们是"一条仅追加日志=唯一真源, 其余全派生"，
> 为人类操作员的编码会话服务）; 但该建议与 DSH 从两头摸到同一原则 —
> **单一事实源 + 派生层**。采纳其生命周期词汇给现有系统立规矩, 不换引擎。

**分层全景（本表即验收单）**：

| 层 | 语义 | 落点 | 状态 |
|---|---|---|---|
| 🔥 热·画像 | 每轮必进上下文 | 人设+system_prompt+欲望/目标+行为日志直供（防编造） | 早已有 |
| 🌡️ 温·锚点 | 按需加权召回 | 记忆卡 + jieba 检索(重要度×新近度×相关度) + 同义词一跳 | ✅（向量锚点=P1） |
| ❄️ 冷·归档 | 长尾落盘 | `log_archive.jsonl` 分段归档（**本次落地**） | ✅ |
| 📕 本体 | 只追加不删除 | world.log / task_log；记忆卡=唯一人工可编辑层（设计点#4 不改） | ✅ 语义即事件源 |

**冷层实现**（`world.py::rotate_world_log` + `server.py::_events_since`）：
- world.log 超过 `NPC_LOG_TAIL`（默认 500，0=关）→ 头部段落追加写
  `NPC_LOG_ARCHIVE_DIR`（默认 `npc/store/log_archive/log_archive.jsonl`），
  每行 `{"i": 绝对索引, "text": 原行}`；内存只留尾部
- **游标铁律**：`world["_log_offset"]` 累计搬走条数，`log_count = offset + len(log)`
  是绝对流位置 — 旧客户端游标跨轮转恒有效；`since < offset` 的冷段由服务端
  从归档文件回放，客户端零改动
- **写失败 → 放弃本轮轮转**（宁可多占内存，绝不丢事件 — 本体只读语义不破）
- 观测：`/api/stats.log_offset`（已归档条数）、`/api/version features.log_archive`

**测试**：`tests/test_log_archive.py`（10 例：阈值/开关/无归档目录不截断/
绝对索引续号/写失败不截断/游标跨轮转冷回放/增量/降级/观测标志）。
全量 **641 passed**。

## 19. LLM 调度队列（任务书 #01，NPC_SCHEDULER，默认 OFF）

> 背景：所有 LLM 调用裸奔 — talk 端点同步调 `npc.talk()`，一次 48 秒对话卡死
> 整个事件循环（期间 `/api/stats` 都无响应）；反思/审查/编译随时插队抢模型。

**目标**：服务器层调度器 — 四类工作按优先级排队，单线程串行执行，对话永远最优先，
对话排队超时自动落回本地规则兜底。开关 `NPC_SCHEDULER`（不设/`0` = 走原路径，零变化）。

### 19.1 优先级

| 类别 | 常量 | 数值 | 说明 |
|---|---|---|---|
| 对话 | `P_TALK` | 0 | B1 对话（最高） |
| 审查 | `P_REVIEW` | 1 | A 审查 / review 槽 |
| 编译 | `P_COMPILE` | 2 | B2 任务编译 |
| 反思 | `P_REFLECT` | 3 | 反思归纳 |

同优先级 FIFO（入队序号做次键）。

### 19.2 结构（`npc/scheduler.py::LLMScheduler` + 单例 `SCHED`）

- `asyncio.PriorityQueue` + 专职 worker task；执行体 `ThreadPoolExecutor(max_workers=1)`
  — 保住「同一时刻全进程只有一个 LLM 请求在飞」的硬约束（上游网关免费通道串行）
- 核心 API `async def run(priority, fn, /, *args, timeout=None, **kwargs)`：
  入队 → worker 在执行线程跑 `fn` → 返回结果 / 抛原异常 / 超 `timeout` 秒抛 `SchedulerTimeout`
  （该条目标记 skip，worker 跳过不执行）
- **嵌套直通（防死锁，最重要）**：worker 线程内的 fn 再调 `run()/invoke()` 时不入队、
  直接执行 — 端点层把整个 `npc.talk` 以 P_TALK 入队，talk 内部还会触发审查/B2 的
  LLM 调用，它们若再排队就是自己等自己
- `start()/stop()` 由 FastAPI lifespan 挂载/卸载；队列在 `start()` 时按当前 running loop
  重建（asyncio 队列绑定首次使用的 loop，跨生命周期复用会 hang）

### 19.3 接线点（`if SCHED 启用`，OFF 时不经过任何新代码）

| 位置 | 包装 | 优先级 | timeout |
|---|---|---|---|
| `server.py` talk 端点 | 整个 `npc.talk` | P_TALK | 60s；超时落 rules 兜底 |
| `npc.py` 反思 LLM | 仅 LLM 总结那一下 | P_REFLECT | None（后台慢慢等） |
| `subagent.py` `run_subagent` | 子代理那次调用 | P_COMPILE(B2) / P_REVIEW(A) | 60s |

### 19.4 可观测

`/api/stats.scheduler` = `{enabled, depth{talk,review,compile,reflect}, waits, timeouts,
avg_wait_ms}`；`/api/version.features.scheduler`（bool）。OFF 时也有该结构（enabled=false，
深度为 0）。

**测试**：`tests/test_scheduler.py::TestLLMScheduler`（6 例：kill-switch / 优先级 /
串行性 / 嵌套直通 / 超时兜底 / 指标，全部在 `NPC_SCHEDULER=1` 下过）。全量 **647 passed**。


### §19 补记（2026-08-24 实弹验收）

- 验收打回两处后收货：invoke() 加 _serial_lock 真互斥（sync 调用方与 executor
  对话任务不再并发双飞）；start() 守卫前置；PROTOCOL.md 补 scheduler 字段。
- 实弹教训：反思若在事件循环线程上同步 invoke 等锁，会把整个 loop 冻到对话
  结束（客户端 90s 无响应）。修复：`_tick_loop` 的反思批次整体挪进
  `asyncio.to_thread(_reflect_batch)` —— 等锁只冻工作线程；回归测试
  `test_loop_stays_alive_while_lock_busy` 用心跳断言钉死此契约。
- 已知取舍：免费网关慢的日子（LLM >60s），talk 按设计落规则兜底；
  mod 侧 HTTP 超时 120s 仍有余量，如需更常吃到真 LLM 可上调端点 timeout。

## 20. 会话快照与未决清单（2026-08-24 深夜）

### 协作模式升级
确立「主控 agent 出任务书 + Trae 强模型执行 + 主控验收」外包流程。
首单 #01 调度队列已按此流程收货（含一轮打回 + 实弹补刀，见 §19 补记）。
任务书模板留存于工作区：`任务书_01_调度队列.md`、`任务书_01_返工单.md`。

### 今日落地
- mod v1.7（走开不再销毁回复 + 停留120s）/ v1.8（原生字幕双保险 + 抽搐修复 +
  错误分类），详见 gta_bridge.md 附录
- 调度队列收货：NPC_SCHEDULER=1 实弹验证通过（60s 兜底准时、循环存活）
- 排障三连根因存档：①脑服进程随会话消亡 ②沙箱致 store 写入 500
  ③记忆卡被测试期规则台词污染 → LLM 复读旧兜底

### 未决清单（按优先序，等拍板）
1. ~~记忆卡污染治理~~ **已完成（2026-08-25，详见 docs/记忆卡治理_方案稿.md §八实施记录）**：
   实测病根=日常循环同文刷屏(单卡3527条"完成：休息2刻")×反思劫持放大；两道闸
   （`NPC_MEMORY_DEDUP` 写入去重聚合+反思卫生，默认关，GTA bat 已开）+
   一次性迁移工具 scripts/migrate_memory_cards.py（备份于 store_backup_20260824_*）。
   12656→28 条，家人三卡归零带人设重启，cang/ali 真实反思全保留。回归 666 passed
2. ~~bat 开关~~ **已完成（2026-08-25 杂项收尾）**：GTA bat 已加 `set NPC_SCHEDULER=1`
   （调度队列实弹验证后转正），双击即排队行为。
3. ~~talk 排队超时~~ **已完成（2026-08-25 杂项收尾）**：新增 `NPC_TALK_QUEUE_TIMEOUT`
   （秒；server.py 读 env，缺省/非法一律 60s，默认行为不变），GTA bat 设 110 ——
   网关慢日（实测 100s+）少落兜底多吃真 LLM，代价是最坏等待变长（mod HTTP 上限 120s 内）。
4. **B2/A 提示词**：用户拟自写。A(审查) 即可换；B2(编译) 建议等 §10
   GTA 方言清单定稿再写，避免二次返工。路由关键词("任务编译器"/"审查员")
   与测试联动，更换时需同步
5. **§10 任务回路**未开工（follow_player/goto 最小闭环起步）
6. 温层向量锚点(P1)、Godot 清理包 —— 可继续出 Trae 任务书

### 运行状态备注
- 会话内脑服(pwsh-4)带全开关在跑；会话结束即停，日常自启用 bat
- 免费网关延迟波动大（实测 28s~100s+），一切"卡住"先查网关再看代码

## 21. 网关抖动重试（2026-08-25 补记）
- **NPC_LLM_RETRY**（代码默认关，启动NPC服务器_GTA.bat 已开）: LLMClient.chat 统一入口处自动补试，
  默认节奏 2s/6s 两档（最坏多花 8s，仍在 talk 排队超时 / mod HTTP 120s 预算内）；
  talk 排队超时现可调（`NPC_TALK_QUEUE_TIMEOUT`，默认 60s 行为不变，GTA bat 已放宽 110s）。
  NPC_LLM_RETRY_DELAYS="a,b" 可自定义。
- 只救瞬时病: 超时 / 连接断 / 限流429 / 5xx；4xx 参数错立刻原样抛——盲试是浪费预算。
- 判别不依赖具体 SDK: 看异常名关键词 + status_code 属性（provider 可插拔不破防）。
- 思想来源: agent/executor/retry.py 的 ExponentialBackoff（本仓库存货）+
  DeepSeek Harness 的 retryPolicy/backoffBaseMs 模式；对照 openai/codex issue #233、#690
  （限流重试被两大家族都当核心基建，而本项目此前为零）。
- 测试: tests/test_llm_retry.py 7 例（time.sleep 打桩零真实等待），全套 646 passed。

## 22. A 审查退役 + 入站安检门（2026-08-25 已实施）

> 状态：**已实施并全量回归通过**。设计全文（7 项决议清单）见 `docs/安全审查重构_方案稿.md`。
> 业界依据：《同类项目调研_AI-NPC.md》第四部分——开源/商业均无"逐句 LLM 自审"先例，本方案为商业双轨派(Inworld)的本地丐版。

- **A 整体拆除（非降级）**：`_chat_with_review` 去 A 分支；`_a_semantic_block` 与 `subagent.py` 的 A_SYSTEM 按"乙案"封存不删（保留供日后对照）；`NPC_SUBAGENT_A` 废弃，读到即 warning 忽略。拆除理由：A 本为 fail-open 无牙闸门 + 免费网关下常超时形同虚设。
- **新增入站安检门** `NPC_SAFETY_GATE`（默认 OFF，bat 选择接入）：L1 违规词硬拦 → 罐头拒绝 + 三不清除 + 日志只记类别哈希不记原文；L2 软旗 → 不拦截，只给 B1 打"礼貌转话题"标记。词表两级制放独立 txt 可编辑。
- **安全宪法**：`safety_constitution.md` 注入每次 prompt；记忆卡注入时自动前置红线句（迁移时物理盖戳）。
- **三不清除**：违规原文永不进上下文（占位对方案）/ 记忆卡（写卡口过滤）/ 反思（材料源已过滤，天然继承）；因从不落盘，无需事后清除。
- 收益：最坏网关调用 3 次 → 1 次；出站违诺重生成保留一次（REVIEW_RETRY_HINT 机制不变）。
- 连带修订：未决清单#4 中"A(审查)提示词即可换"一项**作废**（A 已退役，B2 提示词仍按原计划等 §10 方言定稿）；白闪 A/B 待办重定义为"安检门开关体验对比"。
- **实施记录（2026-08-25）**：新增 `npc/safety.py`（两级词表门+宪法注入+话术池）、
  `safety_constitution.md`（项目根）、`npc/safety_words_L1.txt`/`L2.txt`（可编辑词表）；
  `npc.py` 改造八处——A 分支拆除、`_a_semantic_block` 乙案封存、talk 入站门+占位对、
  宪法与红线句注入、写卡口过滤、A 开关废弃告警；bat 删 `NPC_SUBAGENT_A`、增 `NPC_SAFETY_GATE=1`。
  测试：旧 A 流水线用例改为退役断言（TestARetired×3），新增 `tests/test_safety_gate.py` 11 例；
  全套 **657 passed**（基线 646，净增 11）。GTA5 mod / Godot 消费者零改动——拒绝话术对消费者透明。
