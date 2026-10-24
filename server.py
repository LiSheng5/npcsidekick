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
  结果作为工具消息回上下文继续推理，并**当场写进记忆卡** —— 同步拿到结果就不用回报；
  长动作 / 结果不确定（running / 超时 unknown）除外，按 call_id 走 `/api/action_result`
  回报对账（详见 README「执行结果与『不确定』契约」）。

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
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.client import LLMClient
from core.config import summary_keep_recent, summary_ratio
from core.config_flags import env_num
from core.llm_runtime import (
    apply_llm_settings,
    build_default_client,
    key_perm_hint,
    llm_config_status,
    reasoning_effort,
    reset_llm_settings,
    short_err,
)
from core.logging_config import log
from core.paths import Paths, default_paths, valid_npc_id
from core.personas import PersonaError, persona_ids, persona_path, read_persona
from http_util import json_body
from pending import PendingActions

import chatlog
import console_api
import memory
import tts
import turn

# 控制台静态资源（零构建：index.html + app.js + styles.css）—— 属代码文件，不随 Paths 注入
CONSOLE_DIR = Path(__file__).resolve().parent / "web" / "console"

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

def load_persona(persona_dir: Path, npc_id: str) -> Dict[str, Any]:
    """读 personas/{id}.json；缺失/非法 → HTTPException(400)。"""
    try:
        path = persona_path(persona_dir, npc_id)
    except ValueError as exc:                # npc_id 路径穿越企图
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    def dump(self) -> Dict[str, Dict[str, Any]]:
        """全部登记条目的只读视图（含完整动作清单）。

        控制台的能力清单页用这个（要展示每条动作的 desc/params）；
        /api/state 用 state()（只报数量，更紧凑）。此前控制台直接摸 `_mods`
        私有字段，与 state() 口径还对不齐 —— 现在统一走公开方法。
        """
        return {m: dict(e) for m, e in self._mods.items()}


# ── 请求校验（字段缺失/非法 → 400，响亮失败）────────────────

def _require_text(body: Dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"字段 {key} 必填且必须是非空字符串")
    return value.strip()


def _require_npc_id(npc_id: str) -> str:
    """npc_id 会被拼进文件名：非法（含 `../` 穿越企图）→ 400。"""
    if not valid_npc_id(npc_id):
        raise HTTPException(status_code=400, detail=f"非法的 NPC id: {npc_id}")
    return npc_id


def _note_from_params(action: str, params: Any) -> str:
    """回报没带 note 时，用登记的动作参数拼一句描述（没参数就返回空串 → 写卡回落到动作名）。"""
    if isinstance(params, dict) and params:
        detail = "，".join(f"{k}={v}" for k, v in params.items())
        return f"{action}（{detail}）"
    return ""


# 待定动作 TTL 到点还没回报时的失败文案（"没做成: " 前缀由 memory.add_action_result 加）
PENDING_EXPIRED_NOTE = "一直没等到结果"

# 旧 mod 回报不带 call_id，HTTP 重试会把同一份回报投递多遍 —— 同 key 短窗内只写一次卡
LEGACY_REPORT_DEDUP_S = 10.0

# "迟到回报可覆盖的过期失败卡"登记上限（call_id → 失败卡；超出丢最老的）
_EXPIRED_CARDS_MAX = 256


# ── 应用 ─────────────────────────────────────────────────

@dataclass
class _GameCtx:
    """create_app 的装配产物 —— 游戏面端点工厂共用的那袋状态与收账工具。

    端点本体按资源分组在 `_routes_*` 工厂里；create_app 只做装配。
    """

    registry: Registry
    persona_dir: Path
    store_dir: Path
    llm_holder: Dict[str, Any]                 # 大脑可被页面热换，端点一律读 holder["client"]
    pending_actions: PendingActions
    executor: Optional[Any]
    started_at: float
    summary_scheduler: turn.SummaryScheduler = field(default_factory=turn.SummaryScheduler)
    # 迟到回报要能纠正"TTL 到点已写下的失败卡"：call_id → {npc_id, action, entry_id}，取一次即弃
    expired_cards: "OrderedDict[str, Dict[str, Any]]" = field(default_factory=OrderedDict)
    # 旧 mod 回报（无 call_id）的短窗去重账：(npc_id, action, ok, note) → 最近一次回报时间
    legacy_reports: Dict[tuple, float] = field(default_factory=dict)
    locks: Dict[str, asyncio.Lock] = field(default_factory=dict)

    def lock_for(self, npc_id: str) -> asyncio.Lock:
        return self.locks.setdefault(npc_id, asyncio.Lock())

    async def expire_pending_actions(self) -> None:
        """TTL 到点还没等到回报的动作 → 写一条失败记忆（顺手清理，不开后台任务）。

        `/api/talk` 开始与 `/api/action_result` 到达时各调一次；写卡在 `lock_for(npc_id)` 内
        （与正在跑的回合串行），失败不连累请求本身。写下的失败卡登记进 `expired_cards` ——
        回报迟到几秒才到的话可以来覆盖这张卡（同一动作不留两条记录）。
        """
        for info in self.pending_actions.expire():
            log.warning("pending_action_expired", call_id=info["call_id"],
                        npc_id=info["npc_id"], action=info["action"])
            entry_id = None
            try:
                async with self.lock_for(info["npc_id"]):
                    entry = await asyncio.to_thread(
                        memory.add_action_result, info["npc_id"], info["action"], False,
                        PENDING_EXPIRED_NOTE, store_dir=self.store_dir)
                entry_id = entry.get("id") if isinstance(entry, dict) else None
            except Exception as exc:    # 写卡失败只是少一条记忆，别把请求打挂
                log.warning("pending_expire_memory_failed", npc_id=info["npc_id"],
                            action=info["action"],
                            error=short_err(exc, store_dir=self.store_dir))
            self.expired_cards[info["call_id"]] = {"npc_id": info["npc_id"],
                                                   "action": info["action"],
                                                   "params": info.get("params"),
                                                   "entry_id": entry_id}
            while len(self.expired_cards) > _EXPIRED_CARDS_MAX:
                self.expired_cards.popitem(last=False)

    def replace_expired_card(self, stale: Dict[str, Any], ok: bool, note: str) -> Dict[str, Any]:
        """迟到的真实回报覆盖 TTL 到点写下的失败卡：删旧卡按真实结果重写（同步，交线程池）。

        entry_id 为空 = 当时失败卡就没写成 —— 直接按真实结果补一条即可。
        """
        if stale.get("entry_id"):
            try:
                memory.delete_entry(stale["npc_id"], stale["entry_id"], store_dir=self.store_dir)
            except Exception as exc:
                log.warning("expired_card_delete_failed", npc_id=stale["npc_id"],
                            error=short_err(exc, store_dir=self.store_dir))
        return memory.add_action_result(stale["npc_id"], stale["action"], ok, note,
                                        store_dir=self.store_dir)


def _routes_contract(app: FastAPI, ctx: _GameCtx) -> None:
    """mod 契约端点：报到（capabilities）/ 对话（talk）/ 动作结果回报（action_result）。"""

    @app.post("/api/capabilities")
    async def capabilities(request: Request):
        body = await json_body(request)
        mod = _require_text(body, "mod")
        execute_url = body.get("execute_url")
        if execute_url is not None and not isinstance(execute_url, str):
            raise HTTPException(status_code=400, detail="字段 execute_url 必须是字符串")
        if isinstance(execute_url, str) and execute_url.strip():
            # 要 POST 的地址必须是 http(s) —— file:// 之类别的 scheme 一律不收（SSRF 面收紧）
            parsed = urlparse(execute_url.strip())
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise HTTPException(status_code=400,
                                    detail="字段 execute_url 必须是 http(s) URL")
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
        ctx.registry.declare(mod, clean, execute_url=execute_url)
        log.info("capabilities_declared", mod=mod, actions=len(clean),
                 execute_url=bool(str(execute_url or "").strip()))
        return {"ok": True, "mod": mod, "actions": [a["name"] for a in clean]}

    @app.post("/api/talk")
    async def talk(request: Request):
        body = await json_body(request)
        npc_id = _require_npc_id(_require_text(body, "npc_id"))
        message = _require_text(body, "message")
        observation = body.get("observation")
        mod = body.get("mod")
        if isinstance(mod, str) and mod.strip():
            ctx.registry.touch(mod.strip())
            mod = mod.strip()
        else:
            mod = None

        persona = load_persona(ctx.persona_dir, npc_id)
        # 动作要"能执行"才进 LLM 的工具定义：mod 在线 + 声明过动作 + 给了执行接口（协议 §1）
        execute_url = ctx.registry.execute_url(mod)
        capabilities = ctx.registry.actions(mod) if execute_url else []
        llm = ctx.llm_holder["client"]     # 页面换过脑就拿到新的
        timeout = ctx.registry.timeout()
        wants_voice = bool(body.get("voice"))        # 按需输出通道：默认纯文本
        await ctx.expire_pending_actions()  # 每轮对话开始顺手清理过期待定动作（§4）

        return StreamingResponse(
            turn.stream_turn(llm=llm, npc_id=npc_id, message=message, persona=persona,
                             observation=observation, capabilities=capabilities,
                             execute_url=execute_url, mod=mod, executor=ctx.executor,
                             lock=ctx.lock_for(npc_id), pending_actions=ctx.pending_actions,
                             wants_voice=wants_voice,
                             store_dir=ctx.store_dir, execute_timeout=execute_timeout,
                             summary_scheduler=ctx.summary_scheduler),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-Mod-Heartbeat-Timeout": str(timeout)},
        )

    @app.post("/api/action_result")
    async def action_result(request: Request):
        """长动作回报。匹配规则（协议 §4）：

        1. 带 `call_id` 且命中登记 → 写卡；`note` 为空时用登记的 `params` 拼一句描述。
           **先销账、后清理过期** —— 反过来会让 TTL 边界秒级迟到的回报被 expire 先扫掉
        2. 带 `call_id` 但登记已被 TTL 清掉（失败卡已写）→ 迟到的真实结果**覆盖**那张失败卡
           （同一动作不留两条记录），`matched=true`
        3. 带 `call_id` 但没命中（重复回报 / 未知）→ **不写卡**（同一次动作不能写两遍），
           仍返回 200，`matched=false`
        4. 不带 `call_id`（旧 mod）→ 先按 `(npc_id, action)` 取最早的登记（FIFO），
           **无论命中与否都写卡** —— 旧 mod 的回报不能丢；但同一 `(npc_id, action, ok, note)`
           短窗（`LEGACY_REPORT_DEDUP_S`）内的重复投递只写一次（旧 mod 重试会投递多遍），
           这种被去重的回报返回 `deduped=true`
        """
        body = await json_body(request)
        npc_id = _require_npc_id(_require_text(body, "npc_id"))
        action = _require_text(body, "action")
        ok = body.get("ok")
        if not isinstance(ok, bool):
            raise HTTPException(status_code=400, detail="字段 ok 必填且必须是布尔值")
        note = body.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise HTTPException(status_code=400, detail="字段 note 必须是字符串")

        raw_call_id = body.get("call_id")
        if raw_call_id is not None and not isinstance(raw_call_id, str):
            raise HTTPException(status_code=400, detail="字段 call_id 必须是字符串")
        call_id = (raw_call_id or "").strip()

        mod = body.get("mod")
        if isinstance(mod, str) and mod.strip():
            # 捎带 mod 也算一次心跳（协议 §1）：长动作执行完回报时，mod 不该被判离线。
            # 只刷新已报到过的 mod；不带 mod 就不猜是哪一个。
            ctx.registry.touch(mod.strip())

        stale: Optional[Dict[str, Any]] = None
        if call_id:
            # 先销账再清理过期（顺序见 docstring 1/2）
            info = ctx.pending_actions.resolve(call_id)
            if info is None:
                stale = ctx.expired_cards.pop(call_id, None)
                if stale is not None and (stale["npc_id"], stale["action"]) != (npc_id, action):
                    log.warning("action_result_stale_mismatch", npc_id=npc_id,
                                action=action, call_id=call_id)
                    stale = None
            if info is None and stale is None:
                log.warning("action_result_unmatched", npc_id=npc_id, action=action,
                            call_id=call_id)
                return {"ok": True, "npc_id": npc_id, "entry": None, "matched": False}
            matched = True
        else:
            key = (npc_id, action, ok, note.strip())
            seen_at = ctx.legacy_reports.get(key)
            now = time.time()
            if seen_at is not None and now - seen_at <= LEGACY_REPORT_DEDUP_S:
                log.warning("action_result_legacy_deduped", npc_id=npc_id,
                            action=action, ok=ok)
                return {"ok": True, "npc_id": npc_id, "entry": None,
                        "matched": False, "deduped": True}
            ctx.legacy_reports[key] = now
            for old_key, ts in list(ctx.legacy_reports.items()):
                if now - ts > LEGACY_REPORT_DEDUP_S:
                    del ctx.legacy_reports[old_key]
            info = ctx.pending_actions.resolve_legacy(npc_id, action)
            matched = info is not None

        await ctx.expire_pending_actions()  # 每个回报到达顺手清理过期待定动作（§4）
        record = info or stale
        if record is not None and not note.strip():
            note = _note_from_params(action, record.get("params"))   # 回报没写 note → 用登记信息补

        async with ctx.lock_for(npc_id):
            if stale is not None:
                entry = await asyncio.to_thread(ctx.replace_expired_card, stale, ok, note)
            else:
                entry = await asyncio.to_thread(memory.add_action_result, npc_id, action,
                                                ok, note, store_dir=ctx.store_dir)
        log.info("action_result_recorded", npc_id=npc_id, action=action, ok=ok,
                 matched=matched, call_id=call_id or None)
        return {"ok": True, "npc_id": npc_id, "entry": entry, "matched": matched}


def _routes_status(app: FastAPI, ctx: _GameCtx) -> None:
    """非契约端点：语音合成（tts）、状态查询（state / npcs，调试用）。"""

    @app.post("/api/tts")
    async def tts_endpoint(request: Request):
        """给任意文本配音（含本地对话表 / 头顶气泡）。未装 edge-tts → 503。

        voice 缺省时按 npc_id 的角色卡取音色，再缺省用全局默认。
        """
        body = await json_body(request)
        text = _require_text(body, "text")
        if not tts.available():
            raise HTTPException(status_code=503, detail="edge-tts 未安装")
        voice = body.get("voice")
        if not isinstance(voice, str) or not voice.strip():
            npc_id = body.get("npc_id")
            persona = None
            if isinstance(npc_id, str) and npc_id.strip():
                try:
                    persona = await asyncio.to_thread(load_persona, ctx.persona_dir,
                                                      npc_id.strip())
                except HTTPException:
                    persona = None
            voice = tts.voice_for(persona)
        audio = await tts.synthesize_safe(text, voice)
        return {"audio": tts.to_base64(audio) if audio else "", "voice": voice}

    @app.get("/api/state")
    async def state():
        return {
            "model": getattr(ctx.llm_holder["client"], "model", None),
            "llm": llm_config_status(store_dir=ctx.store_dir),   # 三件套状态：缺什么写什么
            "reasoning_effort": reasoning_effort(store_dir=ctx.store_dir),
            "tts": tts.available(),
            "store_dir": str(ctx.store_dir),
            "persona_dir": str(ctx.persona_dir),
            "npcs": len(persona_ids(ctx.persona_dir)),
            "mods": ctx.registry.state(),
            "heartbeat_timeout_s": ctx.registry.timeout(),
            # 双端共享值经这里下发（heartbeat_timeout_s 的先例）：前端不再各抄一份
            "avatar_max_bytes": console_api.AVATAR_MAX_BYTES,
            "delete_kept_note": console_api.DELETE_KEPT_NOTE,
            # 摘要阈值两项随 env 可热切，前端聊天页文案按它现算
            "summary_ratio": summary_ratio(),
            "summary_keep_recent": summary_keep_recent(),
            "uptime_s": round(time.time() - ctx.started_at, 1),
        }

    @app.get("/api/npcs")
    async def npcs():
        result = []
        for npc_id in persona_ids(ctx.persona_dir):
            try:
                entries = memory.load_card(npc_id, store_dir=ctx.store_dir)
                msgs = chatlog.load_turns(npc_id, store_dir=ctx.store_dir)
            except Exception as exc:         # 坏卡/异常文件名只影响它自己
                result.append({"npc_id": npc_id, "error": str(exc)})
                continue
            result.append({
                "npc_id": npc_id,
                "memory_entries": len(entries),
                "chat_messages": len(msgs),
                "last_activity": max([m["ts"] for m in msgs], default=0.0),
            })
        return {"npcs": result}


def create_app(llm_client: Optional[LLMClient] = None,
               heartbeat_timeout_override: Optional[float] = None,
               executor: Optional[Any] = None,
               paths: Optional[Paths] = None) -> FastAPI:
    """组装应用。llm_client 可注入（测试用假 Provider）；None 时按环境自动创建。

    executor 可注入（测试用假执行器）：`async (url, payload, timeout) -> dict`；
    缺省走真实 HTTP（`turn._http_execute`）请求 mod 的 execute_url。
    paths 可注入（运行时数据目录：store / personas / avatars）；None 时用工程默认。
    """
    app = FastAPI(title="失控NPC v4", version="4.0")
    paths = paths or default_paths()
    avatar_dir = paths.avatar_dir
    registry = Registry(timeout=heartbeat_timeout_override)
    llm_holder: Dict[str, Any] = {"client": llm_client}
    started_at = time.time()

    perm_hint = key_perm_hint(store_dir=paths.store_dir)  # 只检测 api_key.txt 权限，不改文件
    if perm_hint:
        log.warning("api_key_file_permission", hint=perm_hint)

    if llm_client is None:
        try:
            llm_client = build_default_client(store_dir=paths.store_dir)
        except Exception as exc:                 # 无 key / 初始化失败 → 规则回复兜底
            log.warning("llm_unavailable",
                        error=short_err(exc, store_dir=paths.store_dir, limit=160))
            llm_client = None
    llm_holder["client"] = llm_client             # 自动创建的大脑也要进 holder（页面可热换）

    ctx = _GameCtx(registry=registry, persona_dir=paths.persona_dir,
                   store_dir=paths.store_dir, llm_holder=llm_holder,
                   pending_actions=PendingActions(), executor=executor,
                   started_at=started_at)
    _routes_contract(app, ctx)
    _routes_status(app, ctx)
    app.state.registry = registry
    app.state.llm_holder = llm_holder
    app.state.pending = ctx.pending_actions

    # ── 控制台（调试面，见 console_api.py，不属于 mod 契约）──
    avatar_dir.mkdir(parents=True, exist_ok=True)
    app.include_router(console_api.build_router(
        ctx.persona_dir, ctx.lock_for, registry, avatar_dir,
        store_dir=ctx.store_dir,
        llm_api={
            "status": lambda: llm_config_status(store_dir=ctx.store_dir),
            "apply": lambda patch: apply_llm_settings(patch, llm_holder,
                                                      store_dir=ctx.store_dir),
            "reset": lambda: reset_llm_settings(llm_holder, store_dir=ctx.store_dir),
        }))
    app.mount("/avatars", StaticFiles(directory=str(avatar_dir)), name="avatars")
    if CONSOLE_DIR.is_dir():
        app.mount("/console", StaticFiles(directory=str(CONSOLE_DIR), html=True), name="console")

        @app.get("/", include_in_schema=False)
        async def root():
            return RedirectResponse(url="/console/")

    else:                                             # 静态资源缺失也不影响游戏面
        @app.get("/", include_in_schema=False)
        async def root_no_console():
            return {"service": "失控NPC v4", "console": "未安装（缺 web/console）",
                    "api": ["/api/talk", "/api/capabilities", "/api/action_result",
                            "/api/state", "/api/npcs"]}

    return app


app = create_app(llm_client=build_default_client())
