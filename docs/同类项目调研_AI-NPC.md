# 同类 AI NPC 项目调研归档

> 调研日期：2026-08 · 方式：多路 web_search 子代理交叉印证（直连抓取受限，未证实处均标"未确认"）
> 背景：我方 = **NPCSidekick**——Python FastAPI 协议中立的多引擎大脑服务（127.0.0.1:8765）：一套 HTTP 协议，每个引擎一个薄适配器；现有/规划消费者：Godot 生存游戏、GTA5 SHVDN(C#) 模组、HTML/Web 调试台；
> 三角色架构 B1对话/B2任务编译并预订/A语义审查；铁律"LLM 只提议，代码决定执行"；承诺=已审查+已预订；记忆卡无向量库；免费网关延迟 28–100s，有零 LLM 快速路径。

---

## 第一部分：三大深挖

### 1. AI Town（a16z-infra/ai-town）

**总体**：Convex 后端即游戏引擎的极简社会模拟模板。PixiJS 只管渲染；agent 循环跑在 Convex 调度函数里，状态机 thinking/idling/walking。没有外部引擎——自己就是引擎，与我们"引擎 HTTP 轮询大脑"耦合方向相反。

- 架构：Convex 同时当数据库和函数运行时，world/player/conversation/memories 规范化表。[DeepWiki 总览](https://deepwiki.com/a16z-infra/ai-town)
- 记忆：memories 表存描述文本+embedding（对话摘要、观察事件），Convex vector search 相似度召回 + 时近性混合排序注入 prompt。**无重要性评分/反思/遗忘压缩**。[Memory System](https://deepwiki.com/a16z-infra/ai-town/3.2-memory-system)
- 对话：startConversation 创建记录→邀请→轮流流式发言→任一方离开结束；会话整体写成一条带 embedding 的记忆。影响行为的唯一通道是记忆，**不存在任务队列**。[Conversation System](https://deepwiki.com/a16z-infra/ai-town/3.3-conversation-system)
- 行动：LLM 只输出社交意图；走位全脚本寻路，闲逛目标也是代码定的。天然符合我方铁律。[Agent Behavior](https://deepwiki.com/a16z-infra/ai-town/3.1-agent-behavior-and-decision-making)
- 护栏：prompt 文字约束格式，解析失败重试或回退默认行为；无独立审查者。护栏厚度低于我方三角架构。
- 成本：默认接 Together AI 托管便宜模型，社区普遍改 Ollama 本地零成本。未见缓存/分级模型。
- fork：[ai-town-cn 中文版](https://github.com/cxz007/ai-town-cn)、[open-ai-town 重写版](https://github.com/Ishnoor-Singh/open-ai-town)；生态以换模型/汉化/自部署为主，鲜见补任务系统的深度改进。

**借鉴点**：① 会话显式状态机（发起→邀请→轮流→离开由代码推进，LLM 只填内容）——映射到轮询循环的离散步骤，每步都是零 LLM 兜底的天然断点；② 会话结束必写记忆卡 + 时近性混合检索即可维持社交连贯（与我方弃向量库路线互证）；③ 行动层完全脚本化，最小调用面省延迟。

### 2. Generative Agents（斯坦福 Smallville，joonspk-research/generative_agents）

**总体**：用"事件级记忆流+三因子检索+反思树+分层计划"证明 LLM NPC 可长期生活；原版昂贵且无独立审查。"LLM 只出意图、代码执行世界状态"与我方铁律同构。

- 记忆流：每条=一个原子事件的一句话描述。字段：描述文本、创建时间(游戏时)、最后访问时间、embedding。对话/计划/反思洞察同样入流。[论文 arXiv:2304.03442](https://arxiv.org/abs/2304.03442)、[Memory Structure](https://deepwiki.com/joonspk-research/genagents/4.1-memory-structure)
- 检索打分：`score = α·recency + β·importance + γ·relevance` 归一化加权取 Top-K。
  - recency：距上次被访问的游戏小时指数衰减 exp(-λΔt)，衰减因子 0.995/游戏小时；**每次被检索即刷新时间戳**
  - importance：**写入时 LLM 打一次 1–10 分存档**（吃早餐低分、分手高分），运行期不再重算
  - relevance：embedding 余弦相似度
  - [Memory Retrieval](https://deepwiki.com/joonspk-research/genagents/4.2-memory-retrieval)
- 反思：最近未反思记忆 importance 累计 >150 触发；先问"最近经历中 3 个最突出问题"，各自检索相关记忆，LLM 综合产出带证据引用的洞察树；洞察重新入库打分，可递归二阶反思。
- 规划：粗日计划→逐小时→5–30 分钟动作块；ReAct 循环里重估，感知异常即重算；对话可打断，聊完 LLM 决定继续或改写；计划本身入记忆流。
- 执行关系：LLM 只输出意图（去哪/说什么），寻路与地图交互全由 Django+maze 代码层执行；前端 Phaser 仅渲染。无独立审查角色。
- 成本：25 代理两天约 $2000+（官方称"数千美元"）。省钱路线：[AZURE-ARC-0 本地复刻](https://github.com/AZURE-ARC-0/generative-agents)、ai-town 换轻栈、[camel-ai/oasis](https://github.com/camel-ai/oasis) 小模型批量驱动百万代理（成本最好但记忆深度最浅）。记忆治理最佳仍是原版三件套。

**借鉴点**：① 拆卡不删卡——700KB 罐头卡拆成事件级四字段记录（描述+时间+importance+last_retrieved），治理靠 recency 衰减自然沉底+只取 Top-K；② 写时一次打分，运行期零 LLM（关键词重合替代 embedding 余弦），完美兼容零 LLM 快速路径；③ reflection 定期把高价值旧事件簇压成洞察卡，700KB 可缩至 KB 级。

### 3. 游戏模组类（Skyrim/FO4/MC/GTA）

- **Mantella**（[art-from-the-machine/Mantella](https://github.com/art-from-the-machine/Mantella)）：Python 独立进程内置 HTTP 服务，SKSE/Papyrus JSON 收发。世界状态（位置/时间/天气/战斗/背包/关系）注入 prompt；长对话摘要压缩（PR#662）。动作：早期关键字→白名单 Papyrus 函数误触发频发（[Issue#220](https://github.com/art-from-the-machine/Mantella/issues/220)）；[PR#512](https://github.com/art-from-the-machine/Mantella/pull/512) 改为**独立 function-calling 二级调用**做复杂动作——结构与执行分离，最接近"B2 编译+预订"。护栏弱：解析失败回落默认台词。三者中最活跃。
- **HerikaServer**（[abeiro/HerikaServer](https://github.com/abeiro/HerikaServer)）：Papyrus 推事件给本地 PHP 服务端调 OpenAI 兼容接口回传浮窗。事件流驱动+persona+摘要记忆，无向量库。以旁白/对话为主，动作能力缺失。护栏基本无。维护趋缓。
- **SkyrimNet/IntelEngine**（[MinLL/SkyrimNet-GamePlugin](https://github.com/MinLL/SkyrimNet-GamePlugin)、[galanx/IntelEngine-GamePlugin](https://github.com/galanx/IntelEngine-GamePlugin)）：较新事件驱动框架，游戏插件暴露事件总线给外部 AI 服务订阅回写。协议细节未确认。
- **VillagerGPT-zh**（[jim60105/VillagerGPT-zh](https://github.com/jim60105/VillagerGPT-zh)）：Bukkit 插件，LLM 提议自定义交易，**插件校验后才执行**——干净的提议/执行分离样本。
- **Voyager**（学术）：LLM 写技能代码→Mineflayer API 执行→技能库复用闭环。
- **FO4 系**：Mantella 官方支持 + [Fallout-4-Mantella-Mod](https://github.com/YetAnotherModder/Fallout-4-Mantella-Mod)；另有 Pantella 分支、CHIM（细节未确认）。

**横向对比**：最接近三道门=VillagerGPT 与 Mantella PR#512；最放飞=Herika 与早期 Mantella 关键字直通。无人做到"已审查+已预订才成立"——我方差异化成立。

## 第二部分：各引擎普查

### Godot
| 项目 | 形态 | 备注 |
|---|---|---|
| [GDLlama](https://github.com/xarillian/GDLlama) | llama.cpp GDExtension 进程内本地推理 | 活跃维护；记忆/动作校验未确认 |
| [noko](https://github.com/nthnn/noko) | Ollama API（独立服务进程） | 对话型 |
| [AI-Npc-Town](https://github.com/lhh737/AI-Npc-Town)⭐ | **Godot+FastAPI+Docker 外挂大脑，与我方架构几乎同构**（但单引擎单游戏） | 带记忆系统和好感度机制，可直接对标 |
| godot-llm（Adriankhl） | Asset Library 收录本地 LLM 插件 | 细节多未确认 |
| 参考：Inworld 官方 Godot SDK | 云端 SaaS | [inworld-ai/inworld-godot-sdk](https://github.com/inworld-ai/inworld-godot-sdk) |

### Unity
| 项目 | 形态 | 备注 |
|---|---|---|
| LLMUnity | llama.cpp 进程内 | 事实标准；无长期记忆/动作系统 |
| [Unity-LLM-Driven-NPC-Package](https://github.com/dodo13114arch/Unity-LLM-Driven-NPC-Package) | 云 API+TTS/STT+NPC Manager | |
| unity-llm-npc（Lazy-zed） | Ollama 本地动态 NPC | |
| UniChat（AkiKurisu） | 在线+离线双模聊天管线 | |
| sharp-transformers（HF） | Sentis 进程内小模型 | demo 级 |
| Unity-AI-town（1117BoyangWang） | 生成式代理小镇模拟 | |
| [Golem](https://github.com/TreasureProject/Golem)⭐ | Embodied Agent Protocol | 全场唯一触及 AI→动作协议层的开源项目；我方走协议路线后的头号精读对象 |

### Unreal
| 项目 | 形态 | 动作 |
|---|---|---|
| [Llama-Unreal](https://github.com/getnamo/Llama-Unreal) | llama.cpp 进程内 | 无动作系统；RAG 检索 |
| UnrealGenAISupport | 多云 API 进程内 | 开发工具向，附 MCP server |
| EchoWhisperAPI | 本地独立服务 HTTP | 社区里与"外挂大脑"最像的轻量实现 |
| [FANTASIA](https://github.com/antori82/FANTASIA)⭐（[LangGraph 版](https://codeberg.org/antori/FANTASIA-LangGraphDemo)） | 外部 Python 栈 LangGraph+Neo4j | LLM 只出决策、行为树执行——提议/执行分离同构 |
| Inworld SDK | 云端 SaaS，GoalData 目标状态机（条件/completed/failed/链式触发）+预注册 Trigger Events+长期记忆服务+Safety Module 规则分级&独立 Chat Moderation API（功能等同 A 角色） | Goal 完成触发事件回调→蓝图消费 |
| Convai 插件 | 云端 API，长期记忆按玩家跨会话 | **白名单+参数 schema+客户端 Action Handler 注册表三层**，校验权在开发者蓝图 |
| NVIDIA ACE | 微服务管线 Riva ASR/TTS+NeMo LLM+Audio2Face-3D，云端或 RTX 本地双部署 | Kairos demo 逻辑与 Convai 合作；function calling 自接 |
| AWS Dynamic Game NPC Dialogue | 全云端 LLMOps 参考架构 | 依赖 AWS 收费服务 |
| Epic 官方 | UEFN Conversation/Personas 仅限 Fortnite 生态 | 通用 UE 无官方 LLM 工具 |

ACE 借鉴：① 大脑拆成 ASR/LLM/TTS/动画独立微服务流式串联，各段可独立替换；② 表情语音由确定性模型从文本派生，不让 LLM 操控表现层；③ 同一接口云/本地双部署。

### 商业 SDK 补充
- Charisma.ai：作者对话图(story graph)+LLM 节点混合，变量/事实做记忆，图结构约束天然防跑偏；freemium。
- Replica/Stella：TTS 出身 UE5 Smart NPCs，细节未确认。
- Xsolla xsolla-ai-kit：实为编程助手技能包，非 NPC 插件。

### Skyrim/FO4/GTA/RDR2 及其他平台
- Living LS AIs（[Nexus 1817](https://www.nexusmods.com/gta5/mods/1817)）：SHVDN(.NET) 进程内直连 LLM；**具名命令词表** `[DROPWEAPON]`/`[COWER]` 由脚本执行——受限词表路线，与"已预订才成立"思路同源但无审查层、无外挂解耦。**未被下架，至今在线**。
- Sentience V（GTA5 Enhanced）：整包 AI 活剧情；Eurogamer 定性"潜力大、问题明显"；**已被 Take-Two 下架**（详见第四部分）。
- lossantosalive：SHVDN3 语音转录管线，非完整大脑框架。
- RDR2：暂无成熟公开 LLM NPC 模组（仅外包需求帖，未确认）。
- Roblox：roblox-ai-dialogue、roblox-gpt3-game 等，均服务端转发对话型。
- RPG Maker MZ：[NPC-GPT plugin](https://github.com/Gamer-Tool-Studio/npc-gpt-plugin-rmmz)，最成熟的插件化方案。
- 军事仿真：MASA LIFE（前 LLM 时代行为中间件，非生成式）。
- Web/独立：threejs-talking-avatar、Microverse 本地小镇等。

## 第三部分：两张总表

### 表 A · 项目横评（第一轮）

| 项目 | 引擎关系 | 记忆 | 行动执行 | 护栏 |
|---|---|---|---|---|
| AI Town | 自己就是引擎 | 向量+时近性，无反思遗忘 | 无任务概念，移动全脚本 | 薄 |
| Generative Agents | 自己是引擎 | 最强：三因子+反思树+分层计划 | LLM 出意图代码执行，计划可重算 | prompt 约束 |
| Mantella | 外挂大脑（同构引擎族） | 注入 prompt+摘要压缩 | PR#512 结构化调用→白名单函数 | 弱 |
| Herika | PHP 服务端+事件流 | persona+摘要，无向量库 | 无动作能力 | 无 |
| VillagerGPT | 插件内嵌 | 简单 | 提议→校验后执行 | 分离 |
| **NPCSidekick（我们）** | **协议中立的多引擎大脑服务（N 个薄适配器）** | 手工记忆卡（700KB 待治） | B2 编译→三道门→账本预订 | **最厚：B/A 双网关+失败语义** |

### 表 B · 引擎全景（第二轮）

| 引擎 | 大脑形态主流 | 动作校验最强代表 |
|---|---|---|
| Godot | 进程内本地推理；AI-Npc-Town 为外挂 FastAPI 先例 | 未见 |
| Unity | llama.cpp 进程内 | Golem 协议（唯一触及协议层） |
| Unreal | 本地进程内/外部栈/商业 SaaS 三足 | Convai 三层（白名单+schema+Handler） |
| Skyrim/FO4 | 外挂服务（同构） | Mantella PR#512 |
| GTA5/RDR2 | SHVDN 进程内直连或封闭整包（最不工程化） | 具名词表映射 |
| 商业跨引擎 | 云端 SaaS | 平台代管状态机 |

## 第四部分：横向规律与法务警示

**规律四条**：
1. 大脑形态三派：进程内嵌入 / 外挂服务（工程化主流）/ 云端 SaaS。外挂派中带审查+账本的只有我们；跨引擎的外挂派更是仅此一家。
2. 动作校验光谱：无校验(Herika) → 词表映射(LivingLSAIs/Inworld Trigger) → 三层协议(Convai) → 我方账本独一档（时间维度+可审计）。开源界无直接先例。
3. 记忆光谱：无 → 摘要卡(我们在档，缺治理) → 向量 → 图数据库 → 云端抽事实 → 三因子+反思（治理最强）。
4. GTA5 平台最大风险是法务不是技术。

**下架案例专记**：
- Sentient Streets（2023-05，作者 Bloo，Inworld AI 技术+AI 克隆语音）：Take-Two 律师函下架，作者放弃抗争。[Eurogamer](https://www.eurogamer.net/gta-5-ai-mod-taken-down-by-take-two-lawyers)、[GamesIndustry](https://www.gamesindustry.biz/take-two-shuts-down-ai-gta-5-mod)
- Sentience V（2024，YouTube 病毒传播后）：版权打击移除 Nexus 页面并下架视频。[Kotaku](https://kotaku.com/grand-theft-auto-5-gta-6-mod-ai-npc-legal-youtube-1850749678)、[GamesHub](https://www.gameshub.com/news/news/gta-5-ai-powered-story-mod-2626732)、[Yahoo](https://tech.yahoo.com/ai/articles/gta-5-ai-powered-mod-154556974.html)、[IGN：作者不反抗](https://s.ign.com/articles/creator-of-ai-powered-gta-5-story-mode-mod-unlikely-to-fight-back-against-take-two-after-shutdown)
- 原因分析：① 替换/新增剧情触碰 Take-Two 叙事主权红线；② 角色语音克隆叠加人格权风险；③ 高调病毒式传播招致优先执法（RPS：Rockstar 刚放宽单机模组政策仍照打，[链接](https://www.rockpapershotgun.com/gta-5-ai-mod-shot-down-by-take-two-even-as-rockstar-relax-policy-on-modding)；官方政策见 [Rockstar 单机模组页](https://support.rockstargames.com/articles/5NVOAYjcTomO8v6SX2k76k/pc-single-player-mods)；历史先例 2017 OpenIV 事件 [ArsTechnica](https://arstechnica.com/gaming/2017/06/single-player-modding-returns-to-gta-v-after-publisher-takedown/#p3)）。（推断）Living LS AIs 存活正因反着来：纯游戏性反应、不碰叙事、不克隆语音、低调分发。
- **对我方 GTA 消费者的生存法则**：工具类/游戏性增强安全；"AI 叙事内容 + 角色声音 + 高调营销"三件套是死亡组合。

## 第五部分：可借鉴清单（两轮合并，按行动优先级）

| # | 抄什么 | 来源 | 对应我方哪件事 |
|---|---|---|---|
| ① | Convai 三层动作协议（白名单+参数 schema+Handler 注册表） | Convai 文档 | §10 任务执行循环 gta_actions.json 方言清单设计 |
| ② | GoalData 状态机字段（completed/failed/链式触发） | Inworld | 任务账本条目状态机字段 |
| ③ | 记忆三件套：拆卡不删卡+写时打分+反思压缩 | 斯坦福小镇改造版 | 待办#1 700KB 家庭卡治理 |
| ④ | 动作分流结构化第二跳 | Mantella PR#512 | 高延迟网关停顿感；子代理合并讨论参照 |
| ⑤ | 会话结束必写卡硬规则 | AI Town | 第 2 天法则落地保障 |
| ⑥ | 表现层确定性模型派生，LLM 不碰底层 | NVIDIA ACE | 远期语音/表情管线 |
| ⑦ | 法务红线：不做 IP 复刻卖点 | Sentience 之死 | GTA 模组生存策略 |

**参考阅读（横评文章）**：[Cinevva 工具现实检验](https://app.cinevva.com/guides/ai-npcs-dialogue)、[14 个免费本地工具](https://gamineai.com/blog/14-free-local-llm-tools-indie-game-dialogue-npcs-2026)、[传统游戏AI vs Agent NPC](http://www.metavert.io/compare/game-ai-vs-agent-npcs)

## 第六部分：多引擎定位分析（2026-08-25 补记）

**定位声明**：我方不是"GTA 模组配了个大脑"，而是**大脑即服务，引擎只是消费者**——准确对照物是商业 SaaS（Inworld/Convai），而非任何单一游戏模组。开源界无同类；商业同类是 Inworld，我方比它多三样：本地部署、可审计账本、零网关费。

### 座位重排

| | 形态 | 多引擎方式 | 校验权 | 可审计 |
|---|---|---|---|---|
| Inworld / Convai | 云端 SaaS | 每引擎一个官方 SDK | 平台代管 | ❌ 黑盒 |
| Mantella | 外挂服务 | 多游戏但同构引擎族（Creation 系） | mod 侧白名单 | 部分 |
| **NPCSidekick** | 本地外挂服务 | **一套 HTTP 协议，每引擎一个薄适配器** | 大脑侧三道门 | ✅ 本地账本 |

### 定位改变的三件事

1. **协议层从实现细节升级为产品核心**
   - 动作方言按消费者分包：`gta_actions.json` / `godot_actions.json` / `web_actions.json`——三道门逻辑一套，词典各管各的；
   - 加能力协商：消费者上线先声明"我支持哪些动作"，B2 只预订对方执行得了的事（防止预订了 GTA 能做、Godot 没实现的空承诺）；
   - 头号参考物：Golem 的 Embodied Agent Protocol（见第二部分 Unity 节）。
2. **HTML 消费者是白捡的调试台**：浏览器直连 8765（FastAPI 开 CORS），不开 GTA/Godot 测全链路（对话→审查→落账→回忆）；同时也是协议中立的最薄证明——一个网页都能当消费者，什么引擎不能？
3. **多消费者的两个新问题**：
   - 信任边界：现在是 localhost 全信任；HTML 台一旦暴露局域网需简单 token——防篡改铁律的延伸：不只防 LLM 篡改，也防未授权消费者写库；
   - 会话隔离：每个消费者各自的 NPC 各自的记忆空间和账本视图，互不串台。

---
*本文件由 ox-alpha 于 2026-08.24 汇总两轮子代理调研生成；所有"未确认"处未经直接源码验证，引用前请点原始链接复核。*
