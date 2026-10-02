"""HTTP 服务 —— 大脑 ↔ mod 的契约实现。

端点：
  POST /api/capabilities   mod 报到：声明能力清单（白名单唯一闸门）+ 执行接口 + 心跳
  POST /api/talk           对话：SSE 流式（delta / action / audio / done 帧）
  POST /api/action_result  动作结果回报 → 确定性写记忆卡（可选通道，长动作才用）
  POST /api/tts            语音合成（独立输出通道，不属于 mod 契约）
  GET  /api/state          服务器概况（调试）
  GET  /api/npcs           各 NPC 状态摘要（调试）

执行模型（2026-09-28 起）：**动作 = LLM 直接调用的工具**。
  记忆工具由本进程执行；动作工具由本服务**同步调用** mod 的 `execute_url`（协议 §3），
  结果作为工具消息回上下文继续推理，并**当场写进记忆卡** —— mod 不用回报。

两条通道严格分离（台词只走 delta 帧，流末尾的 action 帧只是"已执行动作"的记录）：
  · 台词通道 = SSE 的 delta 帧，只有角色说的话
  · 结构化通道 = action 帧，在流末尾，mod **不需要**照它做任何事（仅记录/展示）
  · 语音通道 = audio 帧（仅 voice=true 时），在 done 之前，缺依赖/超时则不出帧

本文件只留"契约端点 + 应用装配"：
  · 回合主体（LLM ↔ 工具循环、动作同步转发）在 `turn.py`
  · 角色卡读取/编译在 `core/personas.py`；LLM 设置在 `core/llm_runtime.py`
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.client import LLMClient
from core.config_flags import env_num
from core.llm_runtime import (
    apply_llm_settings,
    build_default_client,
    key_perm_hint,
    llm_config_status,
    reasoning_effort,
    reset_llm_settings,
    scrub,
)
from core.logging_config import log
from core.personas import PersonaError, persona_ids, persona_path, read_persona

import chatlog
import console_api
import memory
import tts
import turn

# 角色卡目录（加一个文件就多一个 NPC）
PERSONA_DIR = Path(__file__).resolve().parent / "personas"

# 控制台静态资源（零构建：index.html + app.js + styles.css）
CONSOLE_DIR = Path(__file__).resolve().parent / "web" / "console"

# 关系网节点头像（avatars/{id}.<ext>，静态挂在 /avatars；与控制台同属调试面）
AVATAR_DIR = Path(__file__).resolve().parent / "avatars"

# v4 不预设厂商：模型名 / key / 端点三件套由用户自配（AGENT_MODEL / NPC_API_KEY / NPC_BASE_URL）

# mod 心跳超时（秒）：超时视该 mod 离线，不再调用任何动作
DEFAULT_HEARTBEAT_TIMEOUT = 60.0
_ENV_HEARTBEAT_TIMEOUT = "NPC_MOD_HEARTBEAT_TIMEOUT"

# 动作执行等待上限（秒）：大脑同步调 mod 的 execute_url 时最多等这么久
DEFAULT_EXECUTE_TIMEOUT = 30.0
_ENV_EXECUTE_TIMEOUT = "NPC_EXECUTE_TIMEOUT"


def heartbeat_timeout() -> float:
    """mod 心跳超时（现读，可热切）：NPC_MOD_HEARTBEAT_TIMEOUT 秒，默认 60。"""
    return max(0.0, env_num(_ENV_HEARTBEAT_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT))


def execute_timeout() -> float:
    """动作执行等待上限（现读，可热切）：NPC_EXECUTE_TIMEOUT 秒，默认 30。"""
    return max(0.0, env_num(_ENV_EXECUTE_TIMEOUT, DEFAULT_EXECUTE_TIMEOUT))


# ── 角色卡（读取/枚举在 core/personas.py，这里只管 HTTP 口径）──

def load_persona(npc_id: str) -> Dict[str, Any]:
    """读 personas/{id}.json；缺失/非法 → HTTPException(400)。"""
    path = persona_path(PERSONA_DIR, npc_id)
    try:
        persona = read_persona(path)
    except PersonaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not persona.get("id"):
        raise HTTPException(status_code=400, detail=f"角色卡缺少 id 字段: {path}")
    return persona


# ── 能力清单登记 + 心跳 ───────────────────────────────────

class Registry:
    """mod 能力清单与心跳状态（每个 app 一个实例）。"""

    def __init__(self, timeout: Optional[float] = None):
        self._mods: Dict[str, Dict[str, Any]] = {}
        self._timeout_override = timeout

    def timeout(self) -> float:
        return heartbeat_timeout() if self._timeout_override is None else self._timeout_override

    def declare(self, mod: str, actions: List[dict],
                execute_url: Optional[str] = None) -> None:
        url = execute_url.strip() if isinstance(execute_url, str) else ""
        self._mods[mod] = {"actions": list(actions), "execute_url": url or None,
                           "last_seen": time.time()}

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
        """该 mod 声明且在线时的动作清单；离线/未知 → 空（不调用动作）。"""
        if mod:
            return list(self._mods[mod]["actions"]) if self.online(mod) else []
        online = self.online_mods()
        if len(online) == 1:                     # 只有一个在线 mod → 就是它
            return list(self._mods[online[0]]["actions"])
        return []

    def execute_url(self, mod: Optional[str]) -> Optional[str]:
        """该 mod 的执行接口地址（协议 §1）；离线 / 未知 / 没给 → None。

        没给执行接口 = 该 mod 没有可执行的动作 —— 动作工具不进 LLM 的工具定义。
        """
        if mod:
            entry = self._mods.get(mod)
            if entry is None or not self.online(mod):
                return None
            return entry.get("execute_url")
        online = self.online_mods()
        if len(online) == 1:                     # 只有一个在线 mod → 就是它
            return self._mods[online[0]].get("execute_url")
        return None

    def state(self) -> Dict[str, Any]:
        now = time.time()
        return {
            m: {
                "online": self.online(m),
                "actions": len(e["actions"]),
                "execute_url": e.get("execute_url"),
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


# ── 应用 ─────────────────────────────────────────────────

def create_app(llm_client: Optional[LLMClient] = None,
               heartbeat_timeout_override: Optional[float] = None,
               executor: Optional[Any] = None) -> FastAPI:
    """组装应用。llm_client 可注入（测试用假 Provider）；None 时按环境自动创建。

    executor 可注入（测试用假执行器）：`async (url, payload, timeout) -> dict`；
    缺省走真实 HTTP（`turn._http_execute`）请求 mod 的 execute_url。
    """
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
        execute_url = body.get("execute_url")
        if execute_url is not None and not isinstance(execute_url, str):
            raise HTTPException(status_code=400, detail="字段 execute_url 必须是字符串")
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
        registry.declare(mod, clean, execute_url=execute_url)
        log.info("capabilities_declared", mod=mod, actions=len(clean),
                 execute_url=bool(str(execute_url or "").strip()))
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
        # 动作要"能执行"才进 LLM 的工具定义：mod 在线 + 声明过动作 + 给了执行接口（协议 §1）
        execute_url = registry.execute_url(mod)
        capabilities = registry.actions(mod) if execute_url else []
        llm = llm_holder["client"]          # 页面换过脑就拿到新的
        timeout = registry.timeout()
        wants_voice = bool(body.get("voice"))        # 按需输出通道：默认纯文本

        return StreamingResponse(
            turn.stream_turn(llm=llm, npc_id=npc_id, message=message, persona=persona,
                             observation=observation, capabilities=capabilities,
                             execute_url=execute_url, mod=mod, executor=executor,
                             lock=lock_for(npc_id), wants_voice=wants_voice,
                             execute_timeout=execute_timeout),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-Mod-Heartbeat-Timeout": str(timeout)},
        )

    # ── 动作结果回报（可选通道：长动作异步回报 → 写记忆卡，协议 §4）──
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

        mod = body.get("mod")
        if isinstance(mod, str) and mod.strip():
            # 捎带 mod 也算一次心跳（协议 §1）：长动作执行完回报时，mod 不该被判离线。
            # 只刷新已报到过的 mod；不带 mod 就不猜是哪一个。
            registry.touch(mod.strip())

        async with lock_for(npc_id):
            entry = await asyncio.to_thread(memory.add_action_result, npc_id, action, ok, note)
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
                    persona = await asyncio.to_thread(load_persona, npc_id.strip())
                except HTTPException:
                    persona = None
            voice = tts.voice_for(persona)
        audio = await tts.synthesize_safe(text, voice)
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
            "npcs": len(persona_ids(PERSONA_DIR)),
            "mods": registry.state(),
            "heartbeat_timeout_s": registry.timeout(),
            "uptime_s": round(time.time() - started_at, 1),
        }

    @app.get("/api/npcs")
    async def npcs():
        result = []
        for npc_id in persona_ids(PERSONA_DIR):
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


app = create_app(llm_client=build_default_client())
