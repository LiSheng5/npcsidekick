# NPCSidekick v4

通用游戏 AI NPC 系统的大脑（本地 Python 服务器）——任何能发 HTTP 的游戏（通过 mod）都能接入。

**核心原则：玩家对话 → LLM 调用工具执行。** LLM 在回合内自行决定调什么工具（记忆工具 + mod 声明的游戏动作），
拿到结果继续推理，直到说出台词。动作由大脑**同步调用** mod 的接口、结果**当场写进记忆卡**；唯一闸门是报到声明的**能力白名单**。

## 特性

- 🧠 **记忆卡**：确定性遗忘（强度按半衰期衰减），可钉住豁免，零 LLM 成本
- 🔌 **厂商无关**：只走 OpenAI 兼容端点——DeepSeek / OpenAI / 千问 / Kimi / GLM / Ollama / OpenRouter 换三个环境变量即接
- 🎮 **能力白名单**：mod 报到声明能做什么，大脑就只能调用什么
- 🖥️ **零构建控制台**：真流式试对话、记忆卡管理、关系网图、模拟 mod 报到（不用进游戏就能联调）
- 🗣️ 可选语音合成（edge-tts）、滚动摘要、多人格（`personas/` 加一个 JSON 就多一个 NPC）

## 快速开始

```bash
pip install -r requirements.txt

# 配大脑三件套（key 也可放工程根 api_key.txt，或在控制台「模型」页直接填）
export AGENT_MODEL=deepseek-flash
export NPC_API_KEY=sk-xxx
export NPC_BASE_URL=https://api.deepseek.com

python -m uvicorn server:app --host 127.0.0.1 --port 8765
```

浏览器打开 **http://127.0.0.1:8765/console/** 即是调试控制台。三件套没配齐也能起服务——此时对话回退角色卡里的 `rules` 回复、不调用动作，控制台会用红字写明缺哪几项。服务器只绑 `127.0.0.1`，不出机器。

## 四个端点

mod 侧要用的就这四个，**完整字段级契约见 `规格.md` 第一部分**：

| # | 端点 | 干什么 |
|---|---|---|
| 1 | `POST /api/capabilities` | 报到：声明能力白名单 + 执行接口 `execute_url` + 心跳（不填 `execute_url` = 大脑不调它的动作） |
| 2 | `POST /api/talk` | 对话（SSE：`delta` / `action` / `audio` / `done` 帧），玩家说话时调 |
| 3 | 你自己的 `POST <execute_url>` | 大脑同步调它执行动作，**做完才回响应**（超 30s 视失败） |
| 4 | `POST /api/action_result` | 可选：只有长动作异步才用；同步路径由大脑调 `/execute` 当场写卡 |

```bash
# 例：对话（SSE 流）
curl -X POST http://127.0.0.1:8765/api/talk -H "Content-Type: application/json" \
  -d '{"npc_id":"cang","mod":"mygame","message":"你能帮我做顿饭吗",
       "observation":{"summary":"玩家在厨房，刚下班，有点饿"}}'
```

## 换模型（三件套）

v4 只走 OpenAI 兼容端点——国内外主流厂商与本地 Ollama/vLLM 基本都提供，换三个变量即可：

| 想接 | `AGENT_MODEL` | `NPC_BASE_URL`（**以厂商最新文档为准**） |
|---|---|---|
| DeepSeek | `deepseek-flash`（= V4.1 Flash）/ `deepseek-v4-pro` | `https://api.deepseek.com` |
| OpenAI | `gpt-5` 等 | `https://api.openai.com/v1` |
| 通义千问（百炼） | `qwen-plus` 等 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Kimi / Moonshot | `kimi-k3` 等 | `https://api.moonshot.cn/v1` |
| 智谱 GLM | `glm-5.2` 等 | `https://open.bigmodel.cn/api/paas/v4` |
| 本地 Ollama | `qwen3:8b` 等 | `http://localhost:11434/v1` |
| OpenRouter（一个端点用多家，含 Claude / Gemini） | `anthropic/claude-...` 等 | `https://openrouter.ai/api/v1` |
| 其它厂商 / 自建网关 / 代理 | 厂商给的模型名 | 厂商文档里的兼容端点 |

端点解析规则：**显式配的 `NPC_BASE_URL` 永远优先**（接网关时最关键）；没配才按模型名猜厂商，猜不出 = "未配置"。

**不想改环境变量？** 控制台「模型」页可直接填模型名 / 端点 / 深度思考档位 / **API Key** —— 改完立即生效并记进
`store/llm_config.json`（下次启动还在）。优先级 **页面 > 该文件 > 环境变量**；另有「恢复环境变量默认」与
「删掉明文 key」（只删 key，别的不动）。

## API Key 怎么保护

| 方式 | 存哪 | 说明 |
|---|---|---|
| **环境变量 / `api_key.txt`**（更安全） | 不进 `store/` | 推荐；换 key 要重启 |
| **控制台「模型」页填**（更方便） | **明文**落 `store/llm_config.json` | 页面优先于环境变量；**接口只回掩码、日志已脱敏**，`store/` 在 `.gitignore` 里不会进仓库 |

选了"页面填"的代价：那把 key 是**明文**躺在 `store/llm_config.json` 里，本机能读到它的程序都能拿到。
v4 为此做了三处提示：① 页面上红字 + 「删掉明文 key」按钮；② 文件里自带一行 `_warning`；③ 接口与日志**绝不回原文**（异常先脱敏）。
其余凭据字段（`token` / `authorization` / `secret` / `password`）一律 400 拒绝。

另：工程根若有 `api_key.txt` 且对其他账户可读，启动时日志与控制台会给一条**可直接复制的 `icacls` 收紧命令**
—— v4 **只提示，不会自动改你的文件**。

## 深度思考与各家方言

**模型不认思考参数**：勾上「该模型不认思考参数」，v4 就一个思考参数都不发（等于关闭，也不会因未知字段被拒）；
不能映射成 `thinking:{type:disabled}` —— 对不认这字段的端点同样 400。

各家方言（思考参数、长度参数名等）v4 不猜 —— 用 `NPC_LLM_EXTRA_BODY` 自己填，一段 JSON 原样并入请求体，
例：OpenAI 系思考档位 `NPC_LLM_EXTRA_BODY='{"reasoning":{"effort":"high"}}'`。

## 环境变量（全部现读，可热切）

| 变量 | 默认 | 作用 |
|---|---|---|
| `AGENT_MODEL`（或 `NPC_MODEL`） | 无 | 模型名，用户自定（不设 = 未配置，无大脑；控制台「模型」页可改） |
| `NPC_API_KEY` | 无 | API Key（旧名 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ZHIPU_API_KEY` 仍兼容；也可放 `api_key.txt`） |
| `NPC_BASE_URL` | 无 | OpenAI 兼容端点；不设时按模型名猜厂商兜底，猜不出 = 未配置 |
| `NPC_LLM_EXTRA_BODY` | 无 | 一段 JSON 原样并入请求体（各家方言自己填，覆盖 v4 的默认参数） |
| `NPC_REASONING_EFFORT` | 未设置 | 思考模式：`off`/`disabled`/`none` 显式关；`low`/`medium`/`high`/`max` 开；未设置 = 服务端默认 |
| `NPC_SUMMARY_TOKEN_THRESHOLD` | `20000` | 滚动摘要触发阈值（绝对 token 数） |
| `NPC_SUMMARY_KEEP_RECENT` | `8` | 摘要后仍逐字保留的最近轮数 |
| `NPC_MOD_HEARTBEAT_TIMEOUT` | `60` | mod 心跳超时（秒）：超时视离线，不再调任何动作 |
| `NPC_EXECUTE_TIMEOUT` | `30` | 动作执行等待上限（秒）：大脑同步调 mod 的 `/execute` 时最多等这么久，超时视作失败 |
| `NPC_LLM_RETRY` / `NPC_LLM_RETRY_DELAYS` | 关 | LLM 网关抖动重试（见 `core/client.py`） |

## 控制台（调试面）

浏览器打开 **http://127.0.0.1:8765/console/**（根路径 `/` 自动跳转）——零构建前端，不需要 node。

| 面板 | 能干什么 |
|---|---|
| 总览 | 模型 + LLM 端点（没配齐三件套时红字写明缺哪几项）/ 思考档位 / mod 在线 / 各 NPC 的记忆与聊天体量 |
| 模型 | **直接改谁来当大脑**：模型名 / OpenAI 兼容端点 / 深度思考档位，改完立即生效并记进 `store/llm_config.json`（下次启动还在）；带常见厂商端点速查 |
| 关系网 | 角色卡的 `relations` 字段画成图（零依赖 SVG，分层布局）；**点击刷新按钮刷新 / 滚轮缩放 / 拖拽平移 / 拖节点调位置 / 双击节点聚焦看邻居**；点节点里的「+」上传头像；没填就显示空态，不编造关系 |
| Mod / 能力 | 看已声明的动作清单、执行接口与心跳状态；**可模拟一次 mod 报到**（不用进游戏就能联调） |
| 记忆卡 | 条目增删改、钉住、按"当前强度"可视化（谁快被遗忘一目了然）、手动修剪 |
| 聊天记录 | 逐轮查看 + 摘要状态与 token 进度 |
| 试对话 | `POST /api/talk` 真流式（delta / action / audio / done 帧序列可见）+ 动作记录展示与（可选）异步回报；可勾选语音播报 |
| 角色卡 | `personas/{id}.json` 查看与编辑（保存即生效）；页内可展开**逐字段中文说明表**，空白模板 `personas/_模板.json`，填写说明 `personas/_角色卡说明.md` |

控制台走的是**独立调试端点**（与 mod 用的游戏面端点分开）——它们**不属于 mod 契约**，mod 不需要调用。

## 记忆卡（可直接手改）

`store/{id}_memory.json`——JSON 条目列表：

```json
[{"id": "<uuid>", "content": "玩家爱吃面", "importance": 7,
  "category": "preference", "created_at": 1790260168.1, "pinned": true}]
```

- 写入口只有三个：`remember` 工具、动作结果回报、人工手改
- `pinned: true` = 人工钉住，豁免一切自动修剪
- 遗忘（确定性、零 LLM）：`强度 = importance × 0.5 ^ (小时/72)`，低于 1.0 移除；
  豁免 `importance ≥ 8`、`pinned`、`category` 为 `reflection` / `consolidated`
- 兼容读旧版记忆卡（`content` / `importance` / `created_at` 字段相同）

## 测试

```bash
python -m pytest tests -q
```

测试全零网络：LLM 一律用 `tests/conftest.py` 里的 `FakeProvider` 注入。

## License

MIT
