<p align="center">
  <img src="https://img.shields.io/badge/version-3.2-9b59b6?style=flat-square" alt="version">
  <img src="https://img.shields.io/badge/python-3.10+-purple?style=flat-square" alt="python">
  <img src="https://img.shields.io/badge/tests-982%20passed-brightgreen?style=flat-square" alt="tests">
  <img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="license">
  <img src="https://img.shields.io/badge/game-Godot%204.x-orange?style=flat-square" alt="godot">
</p>

<h1 align="center">NPCSidekick</h1>

<p align="center"><b>AI-driven game NPC framework</b> — villagers who work, follow orders, and remember.</p>

<p align="center"><b>English</b> &nbsp;|&nbsp; <a href="#中文文档">中文文档</a></p>

---

## 🎬 Demo

<video src="docs/demo.mp4" controls width="100%"></video>

---

## What is it?

**NPCSidekick** is the brain for game characters. A small local Python server drives NPCs in any game that can speak HTTP:

- **NPCs actually work** — they autonomously gather resources and bring them back to the camp. The routine runs on a tick-driven world simulation, *no LLM in the loop* — fast and deterministic
- 💬**Orders in plain dialogue** — say `give me 2 wood` and the NPC replies honestly (accept or refuse), then *really does it*: walks to the forest, chops, comes back, and hands the wood to your inventory
- 🧠**They remember** — memory cards are plain editable JSON files; restart the server and progress survives
- 🎨**Add an NPC by dropping one JSON file** — personality, speech style, daily routine table. Zero game code
- **Game integration = 3 HTTP endpoints** — `/api/talk`, `/api/state`, `/api/task`. A reference Godot integration pattern is in [docs/游戏接入.md](docs/游戏接入.md)
- **Hybrid architecture** — LLM is used only for dialogue and decisions; game mechanics stay in deterministic code. No API key? The village still lives (rule fallback)

## How it works

```
┌─────────────┐   HTTP (3 endpoints)   ┌──────────────────────────────────┐
│  Your game  │ ◄────────────────────► │   NPCSidekick server (local)     │
│ (Godot/…)   │   talk / state / task  │                                  │
│  mirrors &  │                        │  ┌────────────────────────────┐  │
│  performs   │                        │  │ tick loop (every 3s)       │  │
└─────────────┘                        │  │  · one step per NPC/tick   │  │
                                       │  │  · routine table (weighted)│  │
                                       │  │  · player orders win       │  │
                                       │  │  · resource regen          │  │
                                       │  └────────────────────────────┘  │
                                       │  ┌────────────────────────────┐  │
                                       │  │ dialogue (LLM, optional)   │  │
                                       │  │ rules fast-path fallback   │  │
                                       │  └────────────────────────────┘  │
                                       │  memory cards = editable JSON    │
                                       └──────────────────────────────────┘
```

**Design lineage**: AI Town–style tick loop · Stanford Generative Agents memory · complexity tiers (ordinary NPCs = rules + small LLM; the full agent loop is reserved for NPCSidekick v4).

## Quick start

```bash
# 1. install (not yet on PyPI — clone and install for now)
git clone https://github.com/LiSheng5/npcsidekick.git && cd npcsidekick
pip install -e .

# 2. run the brain (loads the paleolithic demo villagers 苍/阿黎)
python -m npc.server --adapter paleolithic --no-browser

# 3. talk to it
curl -X POST http://127.0.0.1:8765/api/talk \
  -H "Content-Type: application/json" \
  -d '{"npc_id":"cang","message":"给我两根木材"}'
```

Then point your game at `http://127.0.0.1:8765`. See **[docs/游戏接入.md](docs/游戏接入.md)** for the full 3-endpoint contract and the Godot reference pattern.

## Documentation

- [游戏接入](docs/游戏接入.md) — endpoints, curl examples, Godot integration pattern
- [API 参考](docs/API.md) — full endpoint contract (all 40 endpoints)
- [角色制作 — Making NPCs with JSON](docs/角色制作.md) — persona fields, routine table, example
- [本地模型接入](docs/本地模型.md) — Ollama, env vars, v4 plan
- [NPC 大脑架构](docs/NPC大脑架构.md) — the brain itself: roles, memory, safety tiers, capability audit (Chinese)
- [Web Console](docs/WEB_CONSOLE_架构理解.md) — the developer console (Graph / Characters / Live / Memory / Activity / Playground / Settings)
- [协议契约](engine-clients/common/PROTOCOL.md) — game-side protocol v1 (capability negotiation + mirror face)
- [ARCHITECTURE.md](ARCHITECTURE.md) — agent engine internals
- [QUICKSTART.md](QUICKSTART.md) — the underlying agent engine CLI

## Design notes

- **Player orders beat autonomous routines** — and the NPC refuses honestly when resources are depleted ("木材现在弄不到了，采空了")
- **Anti-hallucination** — factual recall answers come from memory cards verbatim; the LLM context only states world facts, never invented ones
- **Short-term dialogue history in memory only** — chat logs never pollute the memory cards
- **Resource regen** — trees grow back (every tick +1 up to cap), matching the game-side 60s respawn

## Built with

- **DeepSeek** — the NPCs' dialogue & decision brain (V4 Flash)
- **Claude Code** — development, review, debugging
- **Trae** — feature work, bug fixing
- **WorkBuddy** — feature development, bug fixing
- **ChatGPT** — research & reference

## License

MIT — see [LICENSE](LICENSE).

---

# 中文文档

## 这是什么？

**NPCSidekick** 是游戏 NPC 的大脑——一个跑在本地的小型 Python 服务器，通过 HTTP 驱动游戏里的角色：

- **村民真的会干活** — 自主采集资源运回营地（tick 驱动的世界模拟，日常行动**不经过 LLM**，快且确定）
- 💬 **对话下指令** — 说"给我两根木材"，村民诚实回答（接受或拒绝），然后**真的去做**：走去森林、砍树、回来、把木头递进你的背包
- 🧠 **他们记得住** — 记忆卡是纯 JSON 文件，可打开直接改；重启服务器进度不丢
- 🎨 **丢一个 JSON 就多一个 NPC** — 人设、说话风格、日常作息表，零游戏代码
- **游戏接入 = 3 个 HTTP 端点** — `/api/talk`、`/api/state`、`/api/task`；Godot 接入模式见 [docs/游戏接入.md](docs/游戏接入.md)
- **混合架构** — LLM 只用于对话和决策，游戏机制全在确定性代码里；没有 API key 村庄照样运转（规则回退）

## 工作原理

```
┌─────────────┐   HTTP(3 个端点)    ┌──────────────────────────────────┐
│  你的游戏    │ ◄────────────────► │   NPCSidekick 服务器(本地)        │
│ (Godot/….)  │  talk / state /task │                                  │
│  镜像+表演   │                     │  ┌────────────────────────────┐  │
└─────────────┘                     │  │ tick 循环(每 3 秒)          │  │
                                    │  │  · 每 tick 每 NPC 一步      │  │
                                    │  │  · 日常作息表(加权随机)     │  │
                                    │  │  · 玩家指令优先             │  │
                                    │  │  · 资源再生                 │  │
                                    │  └────────────────────────────┘  │
                                    │  ┌────────────────────────────┐  │
                                    │  │ 对话(LLM,可选)              │  │
                                    │  │ 规则快路径回退              │  │
                                    │  └────────────────────────────┘  │
                                    │  记忆卡 = 可编辑 JSON            │
                                    └──────────────────────────────────┘
```

**设计血统**：AI Town 式 tick 循环 · 斯坦福 Generative Agents 记忆 · 复杂度分级（普通 NPC = 规则 + 小模型；完整 agent 循环留给NPCSidekick v4）。

## 快速开始

```bash
# 1. 安装(尚未发布 PyPI,先 clone 安装)
git clone https://github.com/LiSheng5/npcsidekick.git && cd npcsidekick
pip install -e .

# 2. 启动大脑(加载旧石器演示村民 苍/阿黎)
python -m npc.server --adapter paleolithic --no-browser

# 3. 和它说话
curl -X POST http://127.0.0.1:8765/api/talk \
  -H "Content-Type: application/json" \
  -d '{"npc_id":"cang","message":"给我两根木材"}'
```

然后把游戏接到 `http://127.0.0.1:8765` 即可。完整端点契约和 Godot 接入模式见 **[docs/游戏接入.md](docs/游戏接入.md)**。

## 文档

- [游戏接入](docs/游戏接入.md)
- [API 参考](docs/API.md) — 全量 40 条端点契约
- [角色制作 — 用 JSON 做 NPC](docs/角色制作.md)
- [本地模型接入](docs/本地模型.md)
- [NPC 大脑架构](docs/NPC大脑架构.md) — 大脑本体：三角色/记忆/安全分级/能力审计与演进路线图
- [Web Console](docs/WEB_CONSOLE_架构理解.md) — 开发者控制台（Graph/Characters/Live/Memory/Activity/Playground/Settings）
- [协议契约](engine-clients/common/PROTOCOL.md) — 游戏侧协议 v1（能力协商 + 镜像面）
- [ARCHITECTURE.md](ARCHITECTURE.md) — agent 引擎内部设计
- [QUICKSTART.md](QUICKSTART.md) — 底层 agent 引擎 CLI

## 设计要点

- **玩家指令 > 自主日常** — 资源采空时诚实拒绝（"木材现在弄不到了，采空了，等它长回来吧"）
- **防幻觉** — 事实回忆从记忆卡逐字回答；LLM 上下文只放世界事实，禁止编造
- **短期对话历史只在内存** — 聊天记录绝不污染记忆卡
- **资源再生** — 树会再长（每 tick +1 到上限），与游戏侧 60 秒重生对齐

## 开发工具

- **DeepSeek** — 村民的对话与决策大脑（V4 Flash）
- **Claude Code** — 开发、审查、调试
- **Trae** — 功能开发、修 bug
- **WorkBuddy** — 功能开发、修 bug
- **ChatGPT** — 查资料

## 开源协议

MIT — 见 [LICENSE](LICENSE)。
