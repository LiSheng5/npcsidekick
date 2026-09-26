# NPCSidekick v4

通用游戏 AI NPC 系统的大脑（本地 Python 服务器）。任何能发 HTTP 的游戏（通过 mod）都能接入。

**核心原则 —— 大脑提议，游戏执行**：大脑只提议动作，执行归游戏；动作经结构化字段提议，
与对话文字完全分离；大脑侧唯一闸门是**能力白名单**（只有 mod 声明过的动作可被提议）。

---

## 目录

```
NPCSidekick_v4/
├── core/           基础设施（LLM 客户端 / 配置 / 日志 / provider 工厂 / 工具 schema），自包含
├── memory.py       记忆卡 + AI Town 加权检索 + 半衰期修剪
├── tools.py        remember / recall + 由能力清单生成的动作工具
├── chatlog.py      持久聊天记录（{id}_chat.jsonl）+ 滚动摘要
├── tts.py          语音合成（edge-tts，可选输出通道；缺依赖自动降级）
├── server.py       HTTP 服务（/api/talk 流式 等）
├── console_api.py  控制台（调试面）端点，与 mod 契约分开
├── web/console/    控制台前端（零构建：index.html + app.js + styles.css）
├── personas/       角色卡 JSON（加一个文件就多一个 NPC；`_模板.json` 是空白模板）
├── avatars/        关系网节点头像（avatars/{id}.<ext>，控制台里点节点就能上传）
├── store/          运行时数据（记忆卡 + 聊天记录，已 gitignore）
├── tests/          pytest
└── requirements.txt
```

## 安装

```bash
pip install -r requirements.txt
```

**模型由你自己配，v4 不绑任何厂商**：配 `AGENT_MODEL` + `NPC_API_KEY` + `NPC_BASE_URL` 三件套即可
（Key 也可放工程根 `api_key.txt`；旧名 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ZHIPU_API_KEY` 仍兼容）。
三件套缺任何一项也能起服务 —— 此时 `/api/talk` 回退角色卡里的 `rules` 回复、不提议动作，
`GET /api/state` 的 `llm.missing` 会写明缺哪几项。接谁当大脑见下方「换模型」。

可选依赖（缺失自动降级，不装也能跑）：`jieba`（中文分词，检索更准）、`edge-tts`（语音合成，装了才有 `audio` 帧）。

## 启动

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8765
```

服务器只绑 `127.0.0.1`（本地，不出机器）。用哪个模型由环境变量决定（见下方「换模型」）。

### 换模型（三件套）

v4 只走 OpenAI 兼容端点 —— 国内外主流厂商与本地 Ollama/vLLM 基本都提供，换三家变量即可：

| 想接 | `AGENT_MODEL` | `NPC_BASE_URL`（**以厂商最新文档为准**） |
|---|---|---|
| DeepSeek | `deepseek-v4-flash` / `deepseek-v4-pro` | `https://api.deepseek.com` |
| OpenAI | `gpt-5` 等 | `https://api.openai.com/v1` |
| 通义千问（百炼） | `qwen-plus` 等 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Kimi / Moonshot | `kimi-k3` 等 | `https://api.moonshot.cn/v1` |
| 智谱 GLM | `glm-5.2` 等 | `https://open.bigmodel.cn/api/paas/v4` |
| 本地 Ollama | `qwen3:8b` 等 | `http://localhost:11434/v1` |
| OpenRouter（一个端点用多家，含 Claude / Gemini） | `anthropic/claude-...` 等 | `https://openrouter.ai/api/v1` |
| 其它厂商 / 自建网关 / 代理 | 厂商给的模型名 | 厂商文档里的兼容端点 |

端点解析规则：**显式配的 `NPC_BASE_URL` 永远优先**，不会被模型名推断覆盖（接网关时这条是关键）；
没配端点时才按模型名猜厂商兜底，猜不出就是"未配置"。

各家方言（思考参数、长度参数名等）v4 不做猜测 —— 用 `NPC_LLM_EXTRA_BODY` 自己填，
一段 JSON 原样并入请求体，例如 OpenAI 系思考档位：`NPC_LLM_EXTRA_BODY='{"reasoning":{"effort":"high"}}'`。

## 走一遍四个端点（报到 / 对话 / 回报 / 状态）

```bash
# 1) mod 报到：声明能力清单（白名单即唯一闸门）+ 心跳
curl -X POST http://127.0.0.1:8765/api/capabilities \
  -H "Content-Type: application/json" \
  -d '{"mod":"sims4","actions":[{"name":"cook","desc":"用厨房做饭","params":{"dish":"菜名(字符串)"}}]}'

# 2) 对话（SSE：delta / action / audio / done 帧；带 "voice":true 才有 audio 帧）
curl -X POST http://127.0.0.1:8765/api/talk \
  -H "Content-Type: application/json" \
  -d '{"npc_id":"cang","message":"你能帮我做顿饭吗","mod":"sims4",
       "observation":{"summary":"玩家在厨房，刚下班，有点饿"}}'

# 3) 动作结果回报（确定性写记忆卡："完成: …" / "没做成: …"）
curl -X POST http://127.0.0.1:8765/api/action_result \
  -H "Content-Type: application/json" \
  -d '{"npc_id":"cang","action":"cook","ok":true,"note":"面煮好了，玩家吃了说不错"}'

# 4) 状态查询（调试）
curl http://127.0.0.1:8765/api/state
curl http://127.0.0.1:8765/api/npcs
```

## 控制台（调试面）

浏览器打开 **http://127.0.0.1:8765/console/**（根路径 `/` 自动跳转）——零构建前端，不需要 node。

| 面板 | 能干什么 |
|---|---|
| 总览 | 模型 / 思考档位 / mod 在线 / 各 NPC 的记忆与聊天体量 |
| 关系网 | 角色卡的 `relations` 字段画成图（零依赖 SVG，分层布局）；**点击刷新按钮刷新 / 滚轮缩放 / 拖拽平移 / 拖节点调位置 / 双击节点聚焦看邻居**；点节点里的「+」上传头像；没填就显示空态，不编造关系 |
| Mod / 能力 | 看已声明的动作清单与心跳状态；**可模拟一次 mod 报到**（不用进游戏就能联调） |
| 记忆卡 | 条目增删改、钉住、按"当前强度"可视化（谁快被遗忘一目了然）、手动修剪 |
| 聊天记录 | 逐轮查看 + 摘要状态与 token 进度 |
| 试对话 | `POST /api/talk` 真流式（delta / action / audio / done 帧序列可见）+ 一键回报结果；可勾选语音播报 |
| 角色卡 | `personas/{id}.json` 查看与编辑（保存即生效）；页内可展开**逐字段中文说明表**，空白模板 `personas/_模板.json`，填写说明 `personas/_角色卡说明.md` |

控制台用的调试端点见 `协议.md` §8 —— 它们**不属于 mod 契约**，mod 不需要调用。

## 测试

```bash
python -m pytest tests -q
```

测试全零网络：LLM 一律用 `tests/conftest.py` 里的 `FakeProvider` 注入。

## 环境变量（全部现读，可热切）

| 变量 | 默认 | 作用 |
|---|---|---|
| `AGENT_MODEL`（或 `NPC_MODEL`） | 无 | 模型名，用户自定（不设 = 未配置，无大脑） |
| `NPC_API_KEY` | 无 | API Key（旧名 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ZHIPU_API_KEY` 仍兼容；也可放 `api_key.txt`） |
| `NPC_BASE_URL` | 无 | OpenAI 兼容端点；不设时按模型名猜厂商兜底，猜不出 = 未配置 |
| `NPC_LLM_EXTRA_BODY` | 无 | 一段 JSON 原样并入请求体（各家方言自己填，覆盖 v4 的默认参数） |
| `NPC_REASONING_EFFORT` | 未设置 | 思考模式：`off`/`disabled`/`none` 显式关；`low`/`medium`/`high`/`max` 开；未设置 = 服务端默认 |
| `NPC_SUMMARY_TOKEN_THRESHOLD` | `20000` | 滚动摘要触发阈值（绝对 token 数） |
| `NPC_SUMMARY_KEEP_RECENT` | `8` | 摘要后仍逐字保留的最近轮数 |
| `NPC_MOD_HEARTBEAT_TIMEOUT` | `60` | mod 心跳超时（秒）：超时视离线，不再提议动作 |
| `NPC_LLM_RETRY` / `NPC_LLM_RETRY_DELAYS` | 关 | LLM 网关抖动重试（见 `core/client.py`） |

## 记忆卡（可直接手改）

`store/{id}_memory.json` —— JSON 条目列表：

```json
[{"id": "<uuid>", "content": "玩家爱吃面", "importance": 7,
  "category": "preference", "created_at": 1790260168.1, "pinned": true}]
```

- 写入口只有三个：`remember` 工具、动作结果回报、人工手改
- `pinned: true` = 人工钉住，豁免一切自动修剪
- 遗忘（确定性、零 LLM）：`强度 = importance × 0.5 ^ (小时/72)`，低于 1.0 移除；
  豁免 `importance ≥ 8`、`pinned`、`category` 为 `reflection` / `consolidated`
- 兼容读旧版记忆卡（`content` / `importance` / `created_at` 字段相同）
