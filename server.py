"""HTTP 服务 —— 大脑 ↔ mod 的契约实现（协议.md 全文）。

端点：
  POST /api/capabilities   mod 报到：声明能力清单（白名单唯一闸门）+ 心跳
  POST /api/talk           对话：SSE 流式（delta / action / audio / done 帧）
  POST /api/action_result  动作结果回报 → 确定性写记忆卡
  POST /api/tts            语音合成（独立输出通道，协议.md §8）
  GET  /api/state          服务器概况（调试）
  GET  /api/npcs           各 NPC 状态摘要（调试）

两条通道严格分离（设计.md §2.2 / §5）：
  · 台词通道 = SSE 的 delta 帧，只有角色说的话
  · 结构化通道 = action 帧，在流末尾，mod 自己决定执不执行
  · 语音通道 = audio 帧（仅 voice=true 时），在 done 之前，缺依赖/超时则不出帧
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.client import LLMClient
from core.logging_config import log

import chatlog
import console_api
import memory
import tools
import tts

# 角色卡目录（设计.md §7：加一个文件就多一个 NPC）
PERSONA_DIR = Path(__file__).resolve().parent / "personas"

# 控制台静态资源（零构建：index.html + app.js + styles.css）
CONSOLE_DIR = Path(__file__).resolve().parent / "web" / "console"

# 关系网节点头像（avatars/{id}.<ext>，静态挂在 /avatars；与控制台同属调试面）
AVATAR_DIR = Path(__file__).resolve().parent / "avatars"

# 默认模型（设计.md §6：deepseek-v4-flash 便宜，deepseek-v4-pro 更强）
DEFAULT_MODEL = "deepseek-v4-flash"

# 单次对话内的记忆工具循环上限（防死循环）
MAX_TOOL_ROUNDS = 4

# mod 心跳超时（秒）：超时视该 mod 离线，不再提议动作（协议.md §1）
DEFAULT_HEARTBEAT_TIMEOUT = 60.0
_ENV_HEARTBEAT_TIMEOUT = "NPC_MOD_HEARTBEAT_TIMEOUT"

# 思考模式档位（设计.md §6）：未设置 → 不指定，用服务端默认；
# off/disabled/none → 显式关闭；low/medium/high/max → 开启
_ENV_REASONING_EFFORT = "NPC_REASONING_EFFORT"

# 记忆检索返回条数（recall 工具）
RECALL_TOP_K = 5

# 语音合成超时（秒）—— 语音是锦上添花，超时就不等（纯文本保底）
TTS_TIMEOUT_SEC = 5.0


async def _tts_synthesize_safe(text: str, voice: str) -> Optional[bytes]:
    """合成音频，超时/失败 → None（调用方回退纯文本，绝不卡对话）。"""
    try:
        return await asyncio.wait_for(tts.synthesize(text, voice), timeout=TTS_TIMEOUT_SEC)
    except Exception:
        return None


def reasoning_effort() -> Optional[str]:
    """思考模式档位（现读环境变量，可热切）。未设置/空白 → None（服务端默认）。"""
    raw = os.environ.get(_ENV_REASONING_EFFORT)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def heartbeat_timeout() -> float:
    raw = os.environ.get(_ENV_HEARTBEAT_TIMEOUT)
    if raw is None or not str(raw).strip():
        return DEFAULT_HEARTBEAT_TIMEOUT
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return DEFAULT_HEARTBEAT_TIMEOUT


# ── 角色卡（persona JSON → 系统提示词）──────────────────────

def load_persona(npc_id: str) -> Dict[str, Any]:
    """读 personas/{id}.json；缺失/非法 → HTTPException(400)。"""
    path = PERSONA_DIR / f"{npc_id}.json"
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"未找到 NPC 角色卡: {path}")
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"角色卡 JSON 损坏: {path} ({exc})") from exc
    if not isinstance(data, dict) or not data.get("id"):
        raise HTTPException(status_code=400, detail=f"角色卡缺少 id 字段: {path}")
    return data


def build_system_prompt(persona: Dict[str, Any], observation: Any = None) -> str:
    """角色卡 JSON 编译成系统提示词（设计.md §4.2 + §2.2 台词纪律 + §4.4 接地）。"""
    lines = [
        f"你是{persona.get('identity') or '一个游戏角色'}。",
        f"性格: {persona.get('personality') or '友善'}。",
        f"说话风格: {persona.get('speech_style') or '自然'}。",
    ]
    samples = persona.get("voice_samples") or []
    if isinstance(samples, list) and samples:
        lines.append("你说过的台词（语气和用词严格按这些来）:")
        lines.extend(f"- 「{s}」" for s in samples)
    taboos = persona.get("taboos") or []
    if isinstance(taboos, list) and taboos:
        lines.append(f"禁忌: 你绝不会{'、'.join(str(t) for t in taboos)}。")

    if observation:
        text = observation if isinstance(observation, str) else json.dumps(
            observation, ensure_ascii=False)
        lines.append(f"当前情况（游戏观测）: {text}")

    lines.extend([
        "规矩:",
        "1. 你只输出角色说的话（台词）。不要出现“调用工具/函数/参数”之类的说法，"
        "也不要写旁白或舞台说明。",
        "2. 想做什么就用自然语言说出来（例如“我去煮饭”）。动作由游戏侧执行，"
        "你只负责提议；游戏没做，就等于没发生。",
        "3. 只依据上文出现的事实回答。不知道就说不知道，绝不编造。",
        "4. 涉及往事、答应过的事或记不清的细节时，先查记忆（recall）再按查到的原文回答；"
        "查不到就说不知道，绝不编造。",
    ])
    return "\n".join(lines)


def rule_reply(persona: Dict[str, Any], message: str) -> str:
    """LLM 不可用时的规则回复（协议.md §6：回退角色卡 rules 字段）。"""
    rules = persona.get("rules") or {}
    replies = rules.get("replies") or {}
    if isinstance(replies, dict):
        for keyword, reply in replies.items():
            if keyword and keyword in message:
                return str(reply)
    fallback = rules.get("fallback")
    return str(fallback) if fallback else "……"


# ── 能力清单登记 + 心跳 ───────────────────────────────────

class Registry:
    """mod 能力清单与心跳状态（每个 app 一个实例）。"""

    def __init__(self, timeout: Optional[float] = None):
        self._mods: Dict[str, Dict[str, Any]] = {}
        self._timeout_override = timeout

    def timeout(self) -> float:
        return heartbeat_timeout() if self._timeout_override is None else self._timeout_override

    def declare(self, mod: str, actions: List[dict]) -> None:
        self._mods[mod] = {"actions": list(actions), "last_seen": time.time()}

    def touch(self, mod: str) -> None:
        entry = self._mods.get(mod)
        if entry is not None:
            entry["last_seen"] = time.time()

    def online(self, mod: str) -> bool:
        entry = self._mods.get(mod)
        if entry is None:
            return False
        return (time.time() - entry["last_seen"]) <= self.timeout()

    def online_mods(self) -> List[str]:
        return [m for m in self._mods if self.online(m)]

    def actions(self, mod: Optional[str]) -> List[dict]:
        """该 mod 声明且在线时的动作清单；离线/未知 → 空（不提议动作）。"""
        if mod:
            return list(self._mods[mod]["actions"]) if self.online(mod) else []
        online = self.online_mods()
        if len(online) == 1:                     # 只有一个在线 mod → 就是它
            return list(self._mods[online[0]]["actions"])
        return []

    def state(self) -> Dict[str, Any]:
        now = time.time()
        return {
            m: {
                "online": self.online(m),
                "actions": len(e["actions"]),
                "last_seen_s_ago": round(now - e["last_seen"], 1),
            }
            for m, e in self._mods.items()
        }


# ── 请求校验（§6：字段缺失/非法 → 400，响亮失败）────────────

async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return data


def _require_text(body: Dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"字段 {key} 必填且必须是非空字符串")
    return value.strip()


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── 流式工具调用累积 ─────────────────────────────────────

class _ToolCallBuffer:
    """把跨 chunk 的 tool_call 增量拼成完整调用。"""

    def __init__(self) -> None:
        self.name = ""
        self.arguments = ""
        self.call_id = ""

    def feed(self, delta: Dict[str, Any]) -> None:
        if delta.get("id"):
            self.call_id = str(delta["id"])
        if delta.get("function_name"):
            self.name += str(delta["function_name"])
        if delta.get("function_arguments"):
            self.arguments += str(delta["function_arguments"])

    def finalize(self, index: int) -> Dict[str, Any]:
        try:
            args = json.loads(self.arguments) if self.arguments.strip() else {}
        except json.JSONDecodeError:
            log.warning("tool_args_bad_json", tool=self.name, raw=self.arguments[:120])
            args = {}
        if not isinstance(args, dict):
            args = {}
        return {"id": self.call_id or f"call_{index + 1}", "name": self.name, "args": args}


# ── 应用 ─────────────────────────────────────────────────

def create_app(llm_client: Optional[LLMClient] = None,
               heartbeat_timeout_override: Optional[float] = None) -> FastAPI:
    """组装应用。llm_client 可注入（测试用假 Provider）；None 时按环境自动创建。"""
    app = FastAPI(title="NPCSidekick v4", version="4.0")
    registry = Registry(timeout=heartbeat_timeout_override)
    locks: Dict[str, asyncio.Lock] = {}
    started_at = time.time()

    if llm_client is None:
        try:
            llm_client = build_default_client()
        except Exception as exc:                 # 无 key / 初始化失败 → 规则回复兜底
            log.warning("llm_unavailable", error=str(exc)[:160])
            llm_client = None

    def lock_for(npc_id: str) -> asyncio.Lock:
        return locks.setdefault(npc_id, asyncio.Lock())

    # ── 能力清单（协议.md §1）──────────────────────────
    @app.post("/api/capabilities")
    async def capabilities(request: Request):
        body = await _json_body(request)
        mod = _require_text(body, "mod")
        actions = body.get("actions")
        if not isinstance(actions, list) or not actions:
            raise HTTPException(status_code=400, detail="字段 actions 必填且必须是非空数组")
        clean = []
        for i, action in enumerate(actions):
            if not isinstance(action, dict):
                raise HTTPException(status_code=400, detail=f"actions[{i}] 必须是对象")
            name = action.get("name")
            if not isinstance(name, str) or not name.strip():
                raise HTTPException(status_code=400, detail=f"actions[{i}].name 必填")
            desc = action.get("desc")
            if not isinstance(desc, str) or not desc.strip():
                raise HTTPException(status_code=400, detail=f"actions[{i}].desc 必填")
            params = action.get("params", {})
            if params is not None and not isinstance(params, dict):
                raise HTTPException(status_code=400, detail=f"actions[{i}].params 必须是对象")
            clean.append({"name": name.strip(), "desc": desc.strip(), "params": params or {}})
        registry.declare(mod, clean)
        log.info("capabilities_declared", mod=mod, actions=len(clean))
        return {"ok": True, "mod": mod, "actions": [a["name"] for a in clean]}

    # ── 对话（协议.md §2）──────────────────────────────
    @app.post("/api/talk")
    async def talk(request: Request):
        body = await _json_body(request)
        npc_id = _require_text(body, "npc_id")
        message = _require_text(body, "message")
        observation = body.get("observation")
        mod = body.get("mod")
        if isinstance(mod, str) and mod.strip():
            registry.touch(mod.strip())
            mod = mod.strip()
        else:
            mod = None

        persona = load_persona(npc_id)
        capabilities = registry.actions(mod)
        llm = llm_client
        timeout = registry.timeout()
        wants_voice = bool(body.get("voice"))        # 按需输出通道：默认纯文本

        async def stream():
            full_parts: List[str] = []
            proposal: Optional[Dict[str, Any]] = None
            emitted = False
            async with lock_for(npc_id):
                try:
                    if llm is None:
                        raise RuntimeError("LLM 不可用")
                    memory.prune(npc_id)                       # 确定性遗忘（§3.4，零 LLM）
                    memory.set_synonyms(persona.get("entity_synonyms"))
                    history = chatlog.build_history(npc_id, llm)
                    messages = [{"role": "system",
                                 "content": build_system_prompt(persona, observation)}]
                    messages.extend(history)
                    messages.append({"role": "user", "content": message})
                    tool_defs = tools.build_tool_definitions(capabilities)

                    for round_no in range(MAX_TOOL_ROUNDS):
                        buffers: Dict[int, _ToolCallBuffer] = {}
                        round_text: List[str] = []
                        async for chunk in llm.stream(messages, tools=tool_defs,
                                                      reasoning_effort=reasoning_effort()):
                            if chunk.has_content:
                                round_text.append(chunk.content)
                                full_parts.append(chunk.content)
                                emitted = True
                                yield _sse({"type": "delta", "text": chunk.content})
                            if chunk.has_tool_call:
                                delta = chunk.tool_call_delta or {}
                                buffers.setdefault(
                                    int(delta.get("index") or 0), _ToolCallBuffer()).feed(delta)

                        calls = [buffers[k].finalize(k) for k in sorted(buffers)]
                        calls = [c for c in calls if c["name"]]
                        if not calls:
                            break

                        action_calls = [c for c in calls
                                        if tools.is_action_tool(c["name"], capabilities)]
                        if action_calls:
                            proposal = {"name": action_calls[0]["name"],
                                        "params": action_calls[0]["args"]}
                            break                                   # 提议通道：不执行，流末尾出台

                        messages.append({
                            "role": "assistant",
                            "content": "".join(round_text),
                            "tool_calls": [{
                                "id": c["id"], "type": "function",
                                "function": {"name": c["name"],
                                             "arguments": json.dumps(c["args"], ensure_ascii=False)},
                            } for c in calls],
                        })
                        for c in calls:
                            result = tools.run_tool(c["name"], c["args"], npc_id,
                                                    capabilities, top_k=RECALL_TOP_K)
                            messages.append({
                                "role": "tool", "tool_call_id": c["id"],
                                "content": str(result.data if result.ok else (result.error or "")),
                            })
                        if round_no == MAX_TOOL_ROUNDS - 1:
                            log.warning("talk_tool_rounds_exhausted", npc_id=npc_id)
                except Exception as exc:                            # §6 降级：回退角色卡规则回复
                    log.warning("talk_llm_failed", npc_id=npc_id, error=str(exc)[:160])
                    if not emitted:
                        fallback = rule_reply(persona, message)
                        full_parts.append(fallback)
                        emitted = True
                        yield _sse({"type": "delta", "text": fallback})

                chatlog.append_turn(npc_id, message, "".join(full_parts))

            if proposal:
                yield _sse({"type": "action", "action": proposal})
            if wants_voice and tts.available():
                # 台词流已走完，这里才整段合成 —— 不阻塞 delta（设计.md §5）
                voice = tts.voice_for(persona)
                audio = await _tts_synthesize_safe("".join(full_parts), voice)
                if audio:
                    yield _sse({"type": "audio", "audio": tts.to_base64(audio), "voice": voice})
            yield _sse({"type": "done"})

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-Mod-Heartbeat-Timeout": str(timeout)},
        )

    # ── 动作结果回报（协议.md §4）───────────────────────
    @app.post("/api/action_result")
    async def action_result(request: Request):
        body = await _json_body(request)
        npc_id = _require_text(body, "npc_id")
        action = _require_text(body, "action")
        ok = body.get("ok")
        if not isinstance(ok, bool):
            raise HTTPException(status_code=400, detail="字段 ok 必填且必须是布尔值")
        note = body.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise HTTPException(status_code=400, detail="字段 note 必须是字符串")

        async with lock_for(npc_id):
            entry = memory.add_action_result(npc_id, action, ok, note)
        log.info("action_result_recorded", npc_id=npc_id, action=action, ok=ok)
        return {"ok": True, "npc_id": npc_id, "entry": entry}

    # ── 语音合成（协议.md §8：独立输出通道，非 mod 契约）──
    @app.post("/api/tts")
    async def tts_endpoint(request: Request):
        """给任意文本配音（含本地对话表 / 头顶气泡）。未装 edge-tts → 503。

        voice 缺省时按 npc_id 的角色卡取音色，再缺省用全局默认。
        """
        body = await _json_body(request)
        text = _require_text(body, "text")
        if not tts.available():
            raise HTTPException(status_code=503, detail="edge-tts 未安装")
        voice = body.get("voice")
        if not isinstance(voice, str) or not voice.strip():
            npc_id = body.get("npc_id")
            persona = None
            if isinstance(npc_id, str) and npc_id.strip():
                try:
                    persona = load_persona(npc_id.strip())
                except HTTPException:
                    persona = None
            voice = tts.voice_for(persona)
        audio = await _tts_synthesize_safe(text, voice)
        return {"audio": tts.to_base64(audio) if audio else "", "voice": voice}

    # ── 状态查询（协议.md §5）──────────────────────────
    @app.get("/api/state")
    async def state():
        return {
            "model": getattr(llm_client, "model", None),
            "reasoning_effort": reasoning_effort(),
            "tts": tts.available(),
            "store_dir": str(memory.STORE_DIR),
            "persona_dir": str(PERSONA_DIR),
            "npcs": len(_persona_ids()),
            "mods": registry.state(),
            "heartbeat_timeout_s": registry.timeout(),
            "uptime_s": round(time.time() - started_at, 1),
        }

    @app.get("/api/npcs")
    async def npcs():
        result = []
        for npc_id in _persona_ids():
            entries = memory.load_card(npc_id)
            msgs = chatlog.load_turns(npc_id)
            result.append({
                "npc_id": npc_id,
                "memory_entries": len(entries),
                "chat_messages": len(msgs),
                "last_activity": max([m["ts"] for m in msgs], default=0.0),
            })
        return {"npcs": result}

    app.state.registry = registry
    app.state.llm = llm_client

    # ── 控制台（调试面，见 console_api.py / 协议.md §8）──
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    app.include_router(console_api.build_router(PERSONA_DIR, lock_for, registry, AVATAR_DIR))
    app.mount("/avatars", StaticFiles(directory=str(AVATAR_DIR)), name="avatars")
    if CONSOLE_DIR.is_dir():
        app.mount("/console", StaticFiles(directory=str(CONSOLE_DIR), html=True), name="console")

        @app.get("/", include_in_schema=False)
        async def root():
            return RedirectResponse(url="/console/")

    else:                                             # 静态资源缺失也不影响游戏面
        @app.get("/", include_in_schema=False)
        async def root_no_console():
            return {"service": "NPCSidekick v4", "console": "未安装（缺 web/console）",
                    "api": ["/api/talk", "/api/capabilities", "/api/action_result",
                            "/api/state", "/api/npcs"]}

    return app


def _api_key() -> Optional[str]:
    from core import config
    return config.API_KEY or None


def _persona_ids() -> List[str]:
    if not PERSONA_DIR.exists():
        return []
    # 下划线开头 = 模板/说明一类非 NPC 文件（如 personas/_模板.json），不算角色
    return sorted(p.stem for p in PERSONA_DIR.glob("*.json") if not p.name.startswith("_"))


def build_default_client() -> Optional[LLMClient]:
    """按 设计.md §6 的默认模型创建客户端；无 key → None。"""
    if _api_key() is None:
        return None
    from core import config
    from core.factory import create_provider

    model = os.environ.get("AGENT_MODEL") or DEFAULT_MODEL
    provider = create_provider(
        api_key=config.API_KEY,
        model_name=model,
        base_url=config.BASE_URL,
        temperature=config.TEMPERATURE,
        max_tokens=config.MAX_TOKENS,
    )
    return LLMClient(provider=provider)


app = create_app(llm_client=build_default_client())
