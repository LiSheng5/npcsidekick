# NPCSidekick 接入协议（PROTOCOL.md）

> 版本 2026-08-23 · 服务端 `python -m npc.server` · 本文档是客户端接入的唯一权威契约。
> 服务端契约变更必须同步本文件（CI 提醒: 改 server.py 的路由/载荷请 grep 本文档）。

## 0. 铁律（先读这个，血泪换来的）

1. **模型只说话，代码做动作**。LLM 的输出永远只是文本；一切影响游戏世界的行为
   （采集/交付/移动）必须走结构化任务单（B2 编译 + A 审查 + 落账执行）。
2. **零惩罚兜底**：大脑挂了/超时/404，客户端必须用本地话术兜底继续玩，
   绝不让玩家卡住等服务器。
3. **API 严格串行**：本地小模型同一时刻只能处理一个请求。客户端要防并发
   （请求序号防串台，见 §3.2），测试时绝不开并行请求。
4. **走开即取消**：玩家离开交互距离 → 本轮对话作废，迟到的回复不弹 UI。
5. **语音是锦上添花**：`audio` 缺失/播放失败 → 纯文本照常，绝不阻塞文字通道。

## 1. 服务器启动与多世界

```bash
python -m npc.server --adapter paleolithic --port 8765 --world-id godot --no-browser
python -m npc.server --adapter gta         --port 8766 --world-id gta   --no-browser
```

- 一个实例服务一个世界；两个游戏同时开 = 两个实例，**端口必须不同**。
- `--world-id` 会：① 使用独立记忆卡目录 `npc/store_<world_id>/`；② 开启守卫——
  请求体带 `world_id` 且不匹配 → **HTTP 409**（客户端接错线要响亮失败，
  而不是把记忆写进别人的世界）。
- 不传 `--world-id` = 单世界模式，行为与旧版完全一致（零回归）。
- 请求体里的 `world_id` 是可选字段；多世界客户端应当**总是带上**。

## 2. 端点契约

所有请求头建议带 `Origin: http://127.0.0.1:<port>`（服务端只放行本机 Origin）。

### 2.1 POST /api/talk — 对话（核心）

请求：
```json
{
  "npc_id": "cang",              // 必填（注册过的人设 id）
  "message": "给我两根木材",      // 必填
  "thinking": "off",             // 可选: "off" 关闭思考模式(省时)
  "voice": true,                 // 可选: true → 回复附带 TTS 音频
  "world_id": "godot",           // 可选: 多世界守卫
  "context": {                   // 可选: 游戏→大脑世界同步(坏值静默忽略)
    "weather": "rain",           //   "rain"/"festival"/""=晴
    "game_hour": 21,             //   0~23
    "player_pos": "(1204,332)",  //   任意字符串(地点名/坐标)
    "player_char": "麦克"         //   当前操控主角名
  }
}
```

响应（200）：
```json
{ "reply": "好，我这就去弄2个木材给你。",
  "thinking_text": "…",          // 可选,模型思考内容(可视化用)
  "audio": "<base64 mp3>" }      // 可选,仅 voice:true 且合成成功
```

错误语义（客户端按类兜底，不要只看 500）：
| 状态 | 含义 | 客户端动作 |
|---|---|---|
| 400 | message 空 / 参数坏 | 检查请求,不重试 |
| 404 | npc_id 未注册 | 提示"人格缺失",查 --adapter/personas |
| 409 | world_id 不匹配 | 接错实例了,改端口/改 world_id |
| 503 | edge-tts 未装 | voice 降级纯文本 |
| 超时 | 本地大脑 ~20s 正常,**120s 上限** | 显示"思考中(已等 X 秒)",到点本地兜底 |

### 2.2 GET /api/state — 世界镜像轮询（Godot Villager 模式）

响应含 `world_id` + `panels[]` + `actors{id:{position,inventory,state,stamina,activity}}` +
`delivered` + `tick` + `log_tail`。建议 2s 轮询；NPC 客户端用它把大脑侧活动
镜像到游戏侧动画。

`panels[]`（§16 数据驱动界面）: 本游戏该显示哪些状态面板 — 世界 JSON 用
`"_hud": {"panels": ["position", "activity", …]}` 声明，缺省 = 全部面板
（存量世界零回归）。**面板只是显示层契约，`actors` 数据字段照旧完整** —
游戏端镜像照读全量字段，不受声明影响；未知面板名界面忽略（向前兼容）。

### 2.3 GET /api/events?since=N — 增量事件（轮询版）

游标 = `world.log` 索引。响应 `events[]`（结构化事件）+ `log_count`（下轮 since）。
事件类型：`say / move / gather / craft / deliver / task / subagent`（字段见服务端 `_parse_log_line`）。`task`（任务书#02）：`{"type":"task","npc":…,"status":"started"|"done","desc":…}` —— 任务首派/完成的执行过程事件。
`subagent`（2026-08-24 §17）：`{"type":"subagent","agent":"b2"|"a","npc":…,"text":…}`
— B2 编译 / A 审查子代理的生命周期（开始/✓完成/✗失败原因）。客户端不认识的事件类型一律忽略。

**§18 冷层归档（2026-08-24）**：服务端超过 `NPC_LOG_TAIL` 条自动把头部搬盘
（`features.log_archive=true` 可探测）。`since/log_count` 从此是**绝对流位置**——
旧客户端已存的游标跨轮转依然有效，归档段由服务端从磁盘回放，**客户端零改动**；
`/api/stats.log_offset` 报告已归档条数。

### 2.4 GET /api/events/stream?since=N — SSE 推送（轮询的升级替代）

`text/event-stream`。每条事件一帧 `data: {...}`（schema 同 2.3），`retry: 3000`，
每 ~15s 一条 `: ping` 心跳。NPC 多/多游戏共用时用它，服务端零新依赖。

### 2.5 POST /api/npc/register · /api/npc/unregister — 动态人设（GTA 灵魂附体）

```json
// register
{ "persona": { "id": "ped_1830688247", "name": "阿曼达", "identity": "…", "voice": "…" },
  "persistent": false,      // false=流民(despawn 即忘) / true=常驻(落盘续前缘)
  "use_llm": true,
  "world_id": "gta" }
```
`id` 白名单 `[A-Za-z0-9_-]{1,32}`。unregister 传 `{"npc_id": "..."}`。
流民层：平时身体归游戏原生 AI，对话时才唤醒灵魂。

### 2.6 POST /api/tts — 独立语音合成

`{"text":"…","npc_id":"…"}` → `{"audio":"<base64 mp3>"}`（503=未装 edge-tts）。
用途：本地对话表/头顶气泡配音。

### 2.7 管理面：/api/mode · /api/approval(GET+POST) · /api/manifest(GET+POST) · /api/task · /api/tick · /api/memory(GET)

调试与运营用；游戏客户端一般只碰 `mode`（规则/LLM 切换）。

### 2.7.1 任务回路（协议 v1·M2 任务书#02 已实施）

启动开关 `NPC_TASK_LOOP=1`（GTA bat 已开）+ `--manifest <gta_actions.json>`。三个端点：

| 端点 | 载荷 → 语义 |
|---|---|
| POST `/api/consumer/hello` | `{"name":"shvdn_gta","version":"1.9","verbs":["follow_player","goto","say"]}` → 能力报到; 每 ≤30s 重发即心跳(60s 未心跳判死) |
| GET `/api/state?consumer=<name>` | 响应 `pending_tasks[]`（当前在岗任务；**纯读，不写日志**） |
| POST `/api/task_done` | `{"task_id":"t_1","status":"completed"\|"failed"\|"cancelled","detail":"…"}` → 销账; completed 落 EV_DONE 记忆 + "完成任务"事件, failed 落 EV_FAIL 记忆 + 商议字幕(say 事件) |

GTA 方言动作表（`npc/adapters/gta_actions.json`）：
- `follow_player`（tier3/ask, 持续型）：mod 让 ped 持续跟随玩家；无完成条件——玩家下新指令
  由账本 supersede 自动取消，mod 见 pending_tasks 消失即停
- `goto`（tier2/ask, params=[地点]）：mod 按内置中文地标表翻坐标走路；到达(6m) 报 completed，
  未知地点/超时(280s) 报 failed 进商议。地点由**地点词典**归一（world `locations` 的 key
  为规范名，清单可选 `places` 段补别名；最长匹配优先），未加载词典时回退尾词清洗
- 其他：`enter_car_with_player / drive_to / wander / fight / say`（say 走对话通道）

**派发与事件产生机制（任务书#05·A 修订，老 mod 零改动）**：booked→dispatched 转换由
**世界推进**驱动（tick 循环每帧 / 手动 `POST /api/tick`），不再依赖客户端轮询 ——
mod 断连或只走 `/api/events` 也丢不了任务。转换那一刻账本入队，世界推进与事件流端点
（`/api/events`、`/api/events/stream`）消费队列并写世界日志 `"X 接下任务: …"`；
pop 即消费 → 多客户端 poll、断连重连、反复取事件都只写一条日志。
`pending_tasks[]` 结构、日志行文本、`/api/task_done` 语义三项不变。

**动作呈现由清单声明（任务书#05·B）**：动作 spec 可选 `desc_tpl`（账本 desc）与
`ack_tpl`（承诺回话），占位符取任务单自身的键（如 `{地点}`/`{resource}`/`{count}`）；
未声明模板时沿用服务端内置措辞，`/api/manifest` 也不会多出这两个字段。

### 2.8 GET /api/version — 版本/特性握手

响应 `{name, version, world_id, features{events_sse, multi_world, jieba_tokenize,
tts, memory_edit, subagent_roles{b2,a}, scheduler, …}}`。客户端启动探测**一次**，按特性降级：
`features.events_sse=false` → 继续轮询；`multi_world=false` → 不必带 world_id。
以后服务端加功能在这里挂账，老客户端不炸。

### 2.9 GET /api/stats — 观测（调优基线）

响应含 `talk{total, errors, latency_ms_total/max/last, llm, rules}`、
`tts / task / memory_save` 计数、`subagent{b2_compiler|a_reviewer{total, errors,
latency_ms_total/max/last}}`（§17，无真实调用不出现）、`sse_clients(_peak)`、
`scheduler{enabled, depth{talk,review,compile,reflect}, waits, timeouts, avg_wait_ms}`
（§19，NPC_SCHEDULER）、`uptime_sec / tick / mode / npcs`。
内存态，重启清零。用途：调 prompt 对比延迟、看 LLM vs 规则占比。
注意契约：**400 校验失败不计入调用统计**（收口在业务执行之后，
见 `tests/test_server_console.py`）。
语义：`NPC_SCHEDULER=1` 时对话最优先排队（P_TALK > 审查 > 编译 > 反思）、
排队 60s 超时自动落回本地规则兜底（log `npc_talk_queue_timeout`）。

### 自带调试台（零配置）

大脑服务器直接托管控制台：启动后开 `http://127.0.0.1:<port>/console/`（根路径 `/` 指过去;
旧 `npc.html` 已于 2026-09-08 下架）——
对话调试（思考折叠）、**记忆卡整卡编辑回写**（POST /api/memory，整表替换语义，
生产客户端勿用）、审批策略热调（select 即改即生效）、SSE 时间线（断流自动退轮询）。
新游戏接入前先在浏览器里把 prompt 调通，再进引擎联调——省下最贵的串行时间。

## 3. 客户端必学模式（两个游戏踩出来的）

### 3.1 超时竞争法（Timer-race）
HTTP 客户端超时设 120s（大脑慢是常态），但 **UI 用独立计时器**：
每秒刷"思考中…已等 X 秒"；X>120 或 HTTP 抛错 → 立刻切本地兜底文案。
参考：Godot `DialogueUI.gd` / GTA mod `CheckHttp()`。

### 3.2 请求序号防串台
每次发起请求 `seq = ++_reqSeq`；响应回来 `if (seq != _reqSeq) discard;`。
走开取消后，旧线程的迟到响应绝不污染新一轮。**这是并发安全的最低配实现。**

### 3.3 本地话术兜底（零惩罚）
每个错误类别一条本地台词：404→"人格缺失"、超时→"想太久了"、连不上→"服务器没开"。
玩家永远有话说，服务器死活不影响可玩性。

### 3.4 输入法输入框（PC 全屏游戏的深坑）
全屏独占会把新窗口键盘布局钉死英文。WinForms 客户端三件套：
1. 弹窗时 `LoadKeyboardLayout("00000804", KLF_ACTIVATE)` +
   `WM_INPUTLANGCHANGEREQUEST` 主动切中文 + `SetForegroundWindow` 夺前台；
2. **回车/Esc 手动接管**——WinForms 对 `Visible=false` 的按钮不派发
   AcceptButton/CancelButton（血案现场：能打字但发不出退不出）；
3. `Form.BackColor` 禁止 alpha<255（直接抛"控件不支持透明的背景色"）。
彻底解法：游戏设无边框窗口化，或提供剪贴板直发通道（Ctrl+X）。

### 3.5 语音播放（零依赖）
base64 mp3 → `%TEMP%` 临时文件 → winmm/MCI `open/play alias`（Windows 自带，
无外部 DLL）。失败静默跳过。合成超时服务端 5s 收口，别让语音拖死文字。

### 3.6 轮询节奏与升级路径
2s 轮询 `/api/state`（镜像）+ `/api/events`（事件）撑 1-2 个 NPC 没问题；
NPC 多了换 `/api/events/stream`(SSE)。**别在游戏主线程发同步 HTTP**——
Godot 用信号/await，SHVDN 用后台线程 + volatile 握手。

## 4. 新游戏接入清单（填表即用，零服务端代码）

1. **人设**：`npc/personas/<game>/*.json`（identity/personality/speech_style/
   taboos/rules.replies/routine；字段见现有 cang.json）
2. **世界**：适配器 `npc/adapters/<game>.py` 导出 `VILLAGERS`（+可选 `WORLD`：
   locations/resources/exits/recipes——**资源名写进 locations，对话接单自动认识**）
3. **动作清单**：抄 `examples/actions.json` 填 `actions`（tier/approval/params）
   + 可选 `resources`（规范名→别名表）；启动 `--manifest <path>`
4. **桥接**：游戏侧客户端照 §2/§3 实现；桥接笔记放 `npc/adapters/<game>_bridge.md`
5. **测试**：`pytest tests/ -k "<game>"` 全绿再联调；联调严格串行（铁律 #3）
