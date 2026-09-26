"""HTTP 服务 —— 大脑 ↔ mod 的契约实现。

端点：
  POST /api/capabilities   mod 报到：声明能力清单（白名单唯一闸门）+ 心跳
  POST /api/talk           对话：SSE 流式（delta / action / audio / done 帧）
  POST /api/action_result  动作结果回报 → 确定性写记忆卡
  POST /api/tts            语音合成（独立输出通道，不属于 mod 契约）
  GET  /api/state          服务器概况（调试）
  GET  /api/npcs           各 NPC 状态摘要（调试）

两条通道严格分离（台词只走 delta 帧，动作只在流末尾出 action 帧）：
  · 台词通道 = SSE 的 delta 帧，只有角色说的话
  · 结构化通道 = action 帧，在流末尾，mod 自己决定执不执行
  · 语音通道 = audio 帧（仅 voice=true 时），在 done 之前，缺依赖/超时则不出帧
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
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

# 角色卡目录（加一个文件就多一个 NPC）
PERSONA_DIR = Path(__file__).resolve().parent / "personas"

# 控制台静态资源（零构建：index.html + app.js + styles.css）
CONSOLE_DIR = Path(__file__).resolve().parent / "web" / "console"

# 关系网节点头像（avatars/{id}.<ext>，静态挂在 /avatars；与控制台同属调试面）
AVATAR_DIR = Path(__file__).resolve().parent / "avatars"

# v4 不预设厂商：模型名 / key / 端点三件套由用户自配（AGENT_MODEL / NPC_API_KEY / NPC_BASE_URL）

# 单次对话内的记忆工具循环上限（防死循环）
MAX_TOOL_ROUNDS = 4

# mod 心跳超时（秒）：超时视该 mod 离线，不再提议动作
DEFAULT_HEARTBEAT_TIMEOUT = 60.0
_ENV_HEARTBEAT_TIMEOUT = "NPC_MOD_HEARTBEAT_TIMEOUT"

# 思考模式档位：未设置 → 不指定，用服务端默认；
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
    """思考档位（现读，可热切）：页面设置 > 落盘配置 > 环境变量。空白 → None（不指定）。"""
    raw = str(llm_settings().get("reasoning_effort") or "").strip()
    return raw or None


def thinking_effort() -> Optional[str]:
    """实际发给模型的档位 —— 勾了"该模型不认思考参数"就一律 None（一个参数都不发）。

    对不认这些字段的端点，连 `thinking:{type:disabled}` 都是未知参数会 400，
    所以"不支持思考的模型"要映射到"不指定"，而不是显式关闭。
    """
    if llm_settings().get("thinking_unsupported"):
        return None
    return reasoning_effort()


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
    """角色卡 JSON 编译成系统提示词（含台词纪律与"查不到就说不知道"的接地要求）。"""
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
    """LLM 不可用时的规则回复（按角色卡 rules.replies 的关键词命中，兜底用 rules.fallback）。"""
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


# ── 请求校验（字段缺失/非法 → 400，响亮失败）────────────────

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
    # 大脑可被页面热换：端点一律读 holder["client"]，而不是闭包里那一份
    llm_holder: Dict[str, Any] = {"client": llm_client}

    perm_hint = key_perm_hint()             # 只检测 api_key.txt 权限，不改文件
    if perm_hint:
        log.warning("api_key_file_permission", hint=perm_hint)

    if llm_client is None:
        try:
            llm_client = build_default_client()
        except Exception as exc:                 # 无 key / 初始化失败 → 规则回复兜底
            log.warning("llm_unavailable", error=scrub(str(exc))[:160])
            llm_client = None

    def lock_for(npc_id: str) -> asyncio.Lock:
        return locks.setdefault(npc_id, asyncio.Lock())

    # ── 能力清单（mod 报到 + 心跳）──────────────────────
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

    # ── 对话（SSE 流式：delta / action / audio / done）──
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
        llm = llm_holder["client"]          # 页面换过脑就拿到新的
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
                    memory.prune(npc_id)                       # 确定性遗忘（半衰期修剪，不走 LLM）
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
                                                      reasoning_effort=thinking_effort()):
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
                            # 同义词族**按请求显式传入**（取自本 NPC 的角色卡）——
                            # 不走 memory 的进程级全局表，多 NPC 并发时互不污染
                            result = tools.run_tool(c["name"], c["args"], npc_id, capabilities,
                                                    top_k=RECALL_TOP_K,
                                                    synonyms=persona.get("entity_synonyms"))
                            messages.append({
                                "role": "tool", "tool_call_id": c["id"],
                                "content": str(result.data if result.ok else (result.error or "")),
                            })
                        if round_no == MAX_TOOL_ROUNDS - 1:
                            log.warning("talk_tool_rounds_exhausted", npc_id=npc_id)
                except Exception as exc:                            # 降级：LLM 出错就回退角色卡的规则回复
                    log.warning("talk_llm_failed", npc_id=npc_id, error=scrub(str(exc))[:160])
                    if not emitted:
                        fallback = rule_reply(persona, message)
                        full_parts.append(fallback)
                        emitted = True
                        yield _sse({"type": "delta", "text": fallback})

                chatlog.append_turn(npc_id, message, "".join(full_parts))

            if proposal:
                yield _sse({"type": "action", "action": proposal})
            if wants_voice and tts.available():
                # 台词流已走完，这里才整段合成 —— 不阻塞 delta 的逐字输出
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

    # ── 动作结果回报（游戏执行完回报 → 写记忆卡）─────────
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

    # ── 语音合成（独立输出通道，不属于 mod 契约）─────────
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

    # ── 状态查询（/api/state、/api/npcs，调试用）─────────
    @app.get("/api/state")
    async def state():
        return {
            "model": getattr(llm_holder["client"], "model", None),
            "llm": llm_config_status(),          # 三件套状态：缺什么写什么
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
    app.state.llm_holder = llm_holder

    # ── 控制台（调试面，见 console_api.py，不属于 mod 契约）──
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    app.include_router(console_api.build_router(
        PERSONA_DIR, lock_for, registry, AVATAR_DIR,
        llm_api={
            "status": llm_config_status,
            "apply": lambda patch: apply_llm_settings(patch, llm_holder),
            "reset": lambda: reset_llm_settings(llm_holder),
        }))
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


# ── LLM 设置（页面可改，落 store/llm_config.json；key 不在这里）──

_LLM_FIELDS = ("model", "base_url", "reasoning_effort", "thinking_unsupported")
_EFFORT_VALUES = ("off", "disabled", "none", "low", "medium", "high", "max")

# 敏感字段名：这些绝不进页面、绝不进 store/llm_config.json（收到就响亮拒绝）
_SENSITIVE_KEYS = ("api_key", "key", "token", "authorization", "secret")


def mask_secret(value: str) -> str:
    """只留头尾：`sk-abc…wxyz`。页面与接口只给掩码，绝不回原文。"""
    value = value or ""
    if not value:
        return ""
    if len(value) <= 8:
        return f"{value[:2]}…"
    return f"{value[:3]}…{value[-4:]}"


def scrub(text: str) -> str:
    """日志/报错脱敏：把当前 key 的原文换成掩码（异常里偶尔会带出请求头/URL）。"""
    from core import config

    key = config.API_KEY or ""
    out = text or ""
    if len(key) >= 6 and key in out:
        out = out.replace(key, mask_secret(key))
    return out


def api_key_status() -> Dict[str, Any]:
    """key 的只读状态：有没有、来自哪、掩码 —— 内容一个字都不回。"""
    from core import config
    from core.settings import api_key_source

    key = config.API_KEY or ""
    return {
        "present": bool(key),
        "masked": mask_secret(key) or None,
        "source": api_key_source() or None,
        "perm_hint": key_perm_hint(),     # api_key.txt 权限太松时的提示（Windows 才有）
    }


# ── api_key.txt 权限：只检测，不自动改 ────────────────────

_KEY_PERM_HINT: Optional[str] = None
_KEY_PERM_CHECKED = False


def _world_readable_lines(icacls_output: str, path: str = "") -> List[str]:
    """从 icacls 输出里挑"其他账户也能读/写"的行（Everyone / Users / Authenticated Users）。

    先把文件名本身从行首去掉 —— 否则 `C:\\Users\\...` 这种路径会被误判成 Users 账户。
    """
    hits: List[str] = []
    prefix = (path or "").strip().lower()
    for raw in (icacls_output or "").splitlines():
        line = raw.strip()
        if prefix and line.lower().startswith(prefix):
            line = line[len(prefix):]
        low = line.lower()
        if not any(name in low for name in ("everyone", "users", "authenticated users")):
            continue
        if any(right in low for right in ("(r)", "(rx)", "(rw)", "(w)", "(m)", "(f)")):
            hits.append(raw.strip())
    return hits


def scan_key_file_perm(path: Optional[Path] = None) -> Optional[str]:
    """看看 api_key.txt 是不是对其他账户也可读 —— **只检测，绝不改文件**。

    只在 Windows + 文件存在时跑一次 icacls；非 Windows / 没这文件 / icacls 失败 → None（静默）。
    返回一段可直接复制的收紧命令，由用户自己决定跑不跑。
    """
    if os.name != "nt":
        return None
    target = Path(path) if path else Path(__file__).resolve().parent / "api_key.txt"
    if not target.exists():
        return None
    try:
        proc = subprocess.run(["icacls", str(target)],
                              capture_output=True, text=True, timeout=3)
    except Exception:                                   # 命令缺失/超时/被拦 → 一律闭嘴
        return None
    hits = _world_readable_lines(proc.stdout, str(target))
    if not hits:
        return None
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "当前用户"
    return (f"api_key.txt 对其他账户也可读（{hits[0]}）—— v4 不会自动改你的文件，"
            f'要收紧请自己跑：icacls "{target}" /inheritance:r '
            f'/grant:r "{user}:(R,W)" "SYSTEM:(F)" "Administrators:(F)"')


def key_perm_hint(refresh: bool = False) -> Optional[str]:
    """给日志与页面用的提示（只算一次，重启才刷新）。"""
    global _KEY_PERM_HINT, _KEY_PERM_CHECKED
    if refresh or not _KEY_PERM_CHECKED:
        _KEY_PERM_HINT = scan_key_file_perm()
        _KEY_PERM_CHECKED = True
    return _KEY_PERM_HINT

_LLM_RUNTIME: Dict[str, Any] = {}          # 页面改过的值（优先级最高，进程内有效）


def llm_config_path() -> Path:
    """页面设置的落盘位置（与记忆卡同级的运行时目录，已 gitignore）。"""
    return memory.STORE_DIR / "llm_config.json"


def _load_llm_file() -> Dict[str, Any]:
    try:
        data = json.loads(llm_config_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_llm_file(cfg: Dict[str, Any]) -> None:
    path = llm_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _env_llm_config() -> Dict[str, Any]:
    """环境变量那一层 —— 页面与文件都没设时的起点。"""
    from core import config

    return {
        "model": os.environ.get("AGENT_MODEL") or os.environ.get("NPC_MODEL") or "",
        "base_url": os.environ.get("NPC_BASE_URL") or config.BASE_URL or "",
        "reasoning_effort": os.environ.get(_ENV_REASONING_EFFORT) or "",
        "thinking_unsupported": False,
    }


def llm_settings() -> Dict[str, Any]:
    """生效中的设置：页面改的 > store/llm_config.json > 环境变量。"""
    merged = _env_llm_config()
    merged.update({k: v for k, v in _load_llm_file().items() if k in _LLM_FIELDS})
    merged.update(_LLM_RUNTIME)
    return merged


def _llm_origin() -> str:
    """当前值来自哪一层（页面上要说清，免得用户以为改了没生效）。"""
    if _LLM_RUNTIME:
        return "runtime"
    if _load_llm_file():
        return "file"
    return "env"


def llm_config_status() -> Dict[str, Any]:
    """当前 LLM 设置与状态：缺什么写什么，并标明值来自哪一层。"""
    from core import config
    from core.factory import resolve_llm_config

    cfg = llm_settings()
    status = resolve_llm_config(api_key=config.API_KEY or "", model_name=cfg["model"],
                                base_url=cfg["base_url"])
    status["reasoning_effort"] = reasoning_effort()      # 用户设的档位
    status["thinking_effort"] = thinking_effort()        # 实际下发的（None = 一个都不发）
    status["thinking_unsupported"] = bool(cfg["thinking_unsupported"])
    status["origin"] = _llm_origin()
    status["api_key"] = api_key_status()      # 只有掩码，没有原文
    return status


def apply_llm_settings(patch: Dict[str, Any],
                       holder: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """改设置 → 落盘 → 换脑。校验不通过抛 ValueError（调用方转 400）。"""
    for key in patch:                       # 护栏：key 类字段一律拒绝，别被静默吞掉
        if str(key).lower() in _SENSITIVE_KEYS:
            raise ValueError(f"key 不在这里改（收到字段 {key}）—— 请放环境变量 NPC_API_KEY 或工程根 api_key.txt")

    clean: Dict[str, Any] = {}
    for key in ("model", "base_url", "reasoning_effort"):
        if key not in patch or patch[key] is None:
            continue
        value = str(patch[key]).strip()
        if key == "reasoning_effort" and value and value not in _EFFORT_VALUES:
            raise ValueError(f"思考档位只能是 {'/'.join(_EFFORT_VALUES)} 或留空（不指定）")
        clean[key] = value
    if patch.get("thinking_unsupported") is not None:
        clean["thinking_unsupported"] = bool(patch["thinking_unsupported"])

    _LLM_RUNTIME.update(clean)
    _save_llm_file({k: v for k, v in llm_settings().items() if k in _LLM_FIELDS})
    if holder is not None:
        rebuild_llm(holder)
    return llm_config_status()


def reset_llm_settings(holder: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """清掉页面设置与落盘文件，回到环境变量那一层。"""
    _LLM_RUNTIME.clear()
    try:
        llm_config_path().unlink()
    except OSError:
        pass
    if holder is not None:
        rebuild_llm(holder)
    return llm_config_status()


def rebuild_llm(holder: Dict[str, Any]) -> None:
    """按生效设置重建大脑；没配齐 → None（/api/talk 走角色卡 rules 兜底）。"""
    holder["client"] = build_default_client()


def build_default_client() -> Optional[LLMClient]:
    """按用户配置创建 LLM 客户端；三件套缺任何一项 → None（走规则回复兜底）。"""
    status = llm_config_status()
    if not status["ready"]:
        log.warning("llm_not_configured", missing=",".join(status["missing"]),
                    hint="可在控制台「模型」页直接填；未配齐时 /api/talk 走角色卡 rules 回复")
        return None
    if status["source"] == "inferred":
        log.info("llm_base_url_inferred", model=status["model"], base_url=status["base_url"],
                 hint="想换厂商/网关请显式配 NPC_BASE_URL")
    from core import config
    from core.factory import create_provider

    provider = create_provider(
        api_key=config.API_KEY,
        model_name=status["model"],
        base_url=status["base_url"],
        temperature=config.TEMPERATURE,
        max_tokens=config.MAX_TOKENS,
    )
    return LLMClient(provider=provider)


app = create_app(llm_client=build_default_client())
