"""
NPCSidekick — Web 服务（制作者体验优先，参考 AI Town 的 one-command 启动）。

一条命令:  python -m npc.server [--adapter gta] [--port 8765] [--world-id gta]
  - 自动打开浏览器 http://127.0.0.1:8765/npc.html
  - API: /api/npc /api/talk /api/state /api/memory(GET+POST 回写) /api/task
         /api/events(/stream) /api/version(握手) /api/stats(观测)

多世界隔离（2026-08-23）: 一个实例服务一个世界。两个游戏同时开 = 起两个实例:
    python -m npc.server --adapter paleolithic --port 8765 --world-id godot
    python -m npc.server --adapter gta         --port 8766 --world-id gta
  · 各实例独立 store 目录（npc/store_<world_id>）→ 记忆卡永不串世界;
  · 请求带 world_id 且与本实例不符 → 409（客户端接错了线,响亮地失败而不是污染别人记忆）;
  · 不传 world_id 的旧客户端完全兼容（零回归）。

推送通道（2026-08-23）: /api/events/stream = SSE 增量事件流（1s 粒度+心跳）。
2s 轮询 /api/events 继续可用且仍是默认; NPC 多了/多游戏共用时客户端换 SSE 即可,
服务端零新依赖（uvicorn 原生支持 streaming）。

制作者契约（体验设计）:
  - 不写任何后端代码 — 只改 JSON 配置（人设/世界），后端全部隐形
  - 无 API key 也能跑 — LLM 缺失时自动退化规则模式（苍照样聊天干活）
  - 单一命令启动，自动开浏览器

安全: 仅绑定 127.0.0.1 + Origin 校验（本地单机工具；去掉 token —
修复 I13"前端不发 token 导致全部 401"的问题，代价是仅限本机使用）。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import webbrowser
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Dict, List, Optional

# 代码位置锚定的项目根（cwd 无关 — 游戏自动拉起时从任意目录启动都能找到角色/记忆卡/页面）
_BASE_DIR = Path(__file__).resolve().parents[1]

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent.logging_config import log
from npc.npc import NPC
from npc.reviewer import (approval_table, set_approval, get_manifest,
                          load_manifest, manifest_is_default,
                          parse_manifest_doc, set_manifest_resources,
                          get_manifest_resources,
                          load_resource_lexicon_from_world)
from npc.scheduler import SCHED, P_TALK, SchedulerTimeout, tick_round
from npc.world import LOG_TAIL_DEFAULT, rotate_world_log
from npc.tts import available as tts_available
from npc.tts import synthesize as tts_synthesize
from npc.tts import to_base64 as tts_to_base64
from npc.tts import voice_for as tts_voice_for

_ALLOWED_ORIGIN_PREFIXES = ("http://127.0.0.1", "http://localhost")

# ── 动态注册(2026-08-22,GTA 前置 #1) —————————————————————
# 流民层: persistent=false(默认) RAM-only,despawn 反注册即忘
# 常驻层: persistent=true 反注册落盘,重注册读记忆卡续前缘
_DYNAMIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")   # id 白名单(防路径穿越:store 文件名由 id 拼出)
MAX_DYNAMIC_NPCS = 200   # 流民上限(防失控;GTA 同步半径内通常 <30)
# 程序化人设兜底: 游戏只需传 id(+可选 name/identity/voice 等),缺省字段按路人填
_DEFAULT_DYNAMIC_PERSONA = {
    "identity": "街上的陌生人",
    "personality": "普通路人",
    "speech_style": "随意口语",
    "taboos": [],
    "desires": {},
    "goals": {},
    "rules": {"replies": {}, "fallback": "嗯？"},
    "routine": [],   # 流民不自主干活(身体归游戏原生 AI) — 灵魂附体:平时沉睡,对话才唤醒
}

# 自主循环帧间隔（秒）— 环境变量 NPC_TICK_INTERVAL 可覆盖
TICK_INTERVAL = 3.0
# 后台反思触发频率: 每 N 个 tick（≈60s）给每个 NPC 跑一次反思归纳（阶段①，未达阈值不调 LLM）
REFLECT_EVERY_TICKS = 20
# 阶段③: 遗忘合并频率 — 每 N 个 tick（≈3min）跑一次（重复主题合并 + 弱旧修剪）
CONSOLIDATE_EVERY_TICKS = 60
# SSE 推送扫描间隔（秒）与心跳节奏（每 N 次空转发一条注释帧防代理断连）
SSE_SCAN_INTERVAL = 1.0
SSE_HEARTBEAT_IDLE = 15
# 版本/特性握手（2026-08-23 §15）: 客户端启动探测一次, 按特性降级 — 加功能不炸老客户端
BRAIN_VERSION = "2026.08.24"
# 世界状态 HUD 面板默认集（§16 数据驱动界面）: 世界 JSON 可用 "_hud": {"panels": [...]}
# 声明本游戏要显示哪些面板（如 GTA 纯对话世界只要 position/activity）;
# 不声明 = 全部面板（旧世界/旧石器世界向后兼容, 行为不变）。未知面板名前端忽略。
_DEFAULT_HUD_PANELS = ("position", "activity", "stamina", "inventory", "delivered")


def _tick_interval() -> float:
    try:
        return float(os.environ.get("NPC_TICK_INTERVAL", TICK_INTERVAL))
    except ValueError:
        return TICK_INTERVAL

# 语音合成超时（秒）— 语音=锦上添花，超时就别等（文本保底）
TTS_TIMEOUT_SEC = 5.0   # 收紧: 给游戏端 8s 超时留余量


def world_mismatch(expected: str, got) -> bool:
    """world_id 守卫（纯函数便于测试）: 双方都声明了且不一致 → True。

    本实例没配 world_id → 永不拦截（单世界用法零回归）;
    请求没带 world_id → 放行（旧客户端兼容; 多世界客户端应当总带上）。
    """
    return bool(expected and got and str(got) != expected)


async def _tts_synthesize_safe(text: str, voice: str) -> Optional[bytes]:
    """合成音频，超时/失败 → None（调用方回退纯文本，绝不卡对话）。"""
    try:
        return await asyncio.wait_for(tts_synthesize(text, voice), timeout=TTS_TIMEOUT_SEC)
    except Exception:
        return None


def _save_on_transitions(npcs: Dict[str, NPC], events: Dict) -> None:
    """save 只在转换点（started/completed/failed）— 活动进行中的 tick 零落盘。"""
    for actor_id, ev in events.items():
        if ev:
            npcs[actor_id].save()


def _reflect_batch(npcs: list) -> None:
    """一批 NPC 的反思归纳（在 to_thread 工作线程里跑，与对话 LLM 经 _serial_lock 互斥）。"""
    for npc in npcs:
        npc.maybe_reflect()


async def _tick_loop(world, npcs: Dict[str, NPC]) -> None:
    """后台自主循环（村民日常）: 每帧 tick_round + 转换点落盘。单帧异常不杀循环。"""
    tick_count = 0
    while True:
        await asyncio.sleep(_tick_interval())
        tick_count += 1
        try:
            events = tick_round(world, npcs, rng=random.Random())
            _save_on_transitions(npcs, events)
            # 阶段①: 定期触发反思归纳（阈值由 maybe_reflect 内部判定，未达不调 LLM）
            # 流民跳过: RAM-only 记忆 despawn 即忘,反思归纳纯属浪费 LLM
            # 任务书#01实弹教训(2026-08-24): 反思含 LLM 调用,invoke 等锁是同步阻塞——
            # 必须整体挪进 to_thread,等锁只冻这条工作线程,绝不冻事件循环本体
            if tick_count % REFLECT_EVERY_TICKS == 0:
                reflect_batch = [n for n in npcs.values()
                                 if not getattr(n, "ephemeral", False)]
                if reflect_batch:
                    await asyncio.to_thread(_reflect_batch, reflect_batch)
            # 阶段③: 低频遗忘合并（重复主题合并 + 弱旧修剪）;流民同样跳过
            if tick_count % CONSOLIDATE_EVERY_TICKS == 0:
                for npc in npcs.values():
                    if not getattr(npc, "ephemeral", False):
                        npc.consolidate()
        except Exception as exc:
            log.warning("npc_tick_error", error=str(exc))


def _verify_origin(request: Request) -> None:
    """Origin 校验: 只允许本机（DNS-rebind 防护）。"""
    origin = request.headers.get("origin", "")
    if origin and not any(origin.startswith(p) for p in _ALLOWED_ORIGIN_PREFIXES):
        raise HTTPException(status_code=403, detail="不允许的 Origin")


def load_village(store_dir: str = "npc/store", personas: Optional[Dict] = None,
                 world: Optional[Dict] = None):
    """加载示例村庄: 共享世界 + 全部示例 NPC。

    personas: {id: persona} 角色表 — 默认角色表（苍/阿黎）；
    传入适配器角色表（如 paleolithic.VILLAGERS）即得到该游戏的村民。
    world: 适配器自定义世界（2026-08-22 GTA 接入）— 传入则所有 NPC 共用它，
    记忆卡里的旧世界快照被替换（与人设以 JSON 为准同理：换游戏不该被旧
    记忆卡的世界快照绑架，否则阿曼达开口就是"你位于村庄"）。
    从记忆卡恢复（若有），否则新建。多 NPC 共用一个世界（AI Town 模式）。
    """
    from pathlib import Path

    from npc.persona import SAMPLE_NPCS, build_system_prompt
    from npc.world import actor_of, default_world

    cast = personas or SAMPLE_NPCS
    loaded: Dict[str, NPC] = {}
    shared = world   # None → 用第一个记忆卡的世界(无卡则默认世界)
    for pid, persona in cast.items():
        path = Path(store_dir) / f"{pid}_memory.json"
        if path.exists():
            npc = NPC.load(pid, store_dir=store_dir)
            # 人设以制作者 JSON 为准 — 记忆卡只存记忆/世界/任务日志。
            # （否则制作者改了 cang.json 却因旧记忆卡不生效，是制作者体验陷阱）
            npc.persona = persona
            npc.system_prompt = persona.get("system_prompt_override") or build_system_prompt(persona)
            if shared is None:
                shared = npc.world
            else:
                npc.world = shared
                actor_of(shared, pid)
        else:
            if shared is None:
                shared = default_world()
            npc = NPC(persona=persona, world=shared, store_dir=store_dir)
        loaded[pid] = npc
    # 清理孤儿槽: 记忆卡世界快照可能含当前角色表(cast)没有的 NPC(如删了 ali.json 的残留),
    # tick_round 会遍历 world["actors"] 全部槽做 npcs[actor_id] 索引 → 孤儿槽 KeyError → 全村冻结。
    for orphan in [a for a in shared["actors"] if a not in loaded]:
        del shared["actors"][orphan]
    return shared, loaded


def _apply_context(world: Dict, context: Optional[Dict]) -> None:
    """游戏→大脑世界同步(2026-08-22): 对话请求捎带的 context 直写世界扩展键(_ 前缀)。

    补全"世界真相单向"缺口: 此前大脑活在文本世界,游戏端真实天气/时间/位置进不来。
    GTA 流民层靠它聊完即弃(不维护镜像世界);Godot 常驻层持续同步同一接口。
    可选键: weather("rain"/"festival"/""=晴)、game_hour(0~23)、player_pos(地点/坐标串)
    坏值静默忽略(客户端手滑不炸服务)。
    """
    if not context:
        return
    if "weather" in context:
        world["_weather"] = str(context.get("weather") or "")
    if "game_hour" in context:
        try:
            world["_game_hour"] = int(context["game_hour"]) % 24
        except (TypeError, ValueError):
            pass
    if "player_pos" in context:
        world["_player_pos"] = str(context.get("player_pos") or "")
    world["_context_at"] = world.get("_tick", 0)   # 最后同步 tick(诊断)


def create_npc_server(npcs: Optional[Dict[str, NPC]] = None,
                      world_id: str = "") -> FastAPI:
    """构建 NPCSidekick Web 服务（工厂函数，便于测试注入）。

    npcs: {id: NPC} 多 NPC 共享世界。默认加载示例村庄。
    world_id: 本实例的世界命名空间（多世界隔离; 空 = 单世界模式,不校验）。
    后台自主循环随 FastAPI lifespan 启动 — TestClient 不进 with 上下文则不启动（测试确定性）。
    """
    if npcs is None:
        world, npcs = load_village()
    else:
        world = next(iter(npcs.values())).world   # AI Town 模式: 共享世界

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        task = asyncio.create_task(_tick_loop(world, npcs))
        # 任务书 #01：NPC_SCHEDULER 启用时挂载 LLM 调度 worker（OFF 时零动作）
        if SCHED.enabled:
            SCHED.start()
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if SCHED.started:
                await SCHED.stop()

    app = FastAPI(title="NPCSidekick", lifespan=_lifespan)

    def get_npc(npc_id: str) -> NPC:
        if npc_id not in npcs:
            raise HTTPException(status_code=404, detail=f"没有 NPC: {npc_id}")
        return npcs[npc_id]

    def guard_world(body: Dict) -> None:
        """多世界守卫: 请求声明的 world_id 与本实例不符 → 409（接错线要响亮失败）。"""
        if isinstance(body, dict) and world_mismatch(world_id, body.get("world_id")):
            raise HTTPException(
                status_code=409,
                detail=f"world_id 不匹配: 本实例服务 '{world_id}', 请求是 '{body.get('world_id')}'")

    _dynamic: set = set()   # 动态注册的 id(可反注册);cast 静态角色(cang/ali)不在其中

    # ── 观测计数（2026-08-23 §15 /api/stats）: 零侵入埋点 — 只在端点收口处记一笔。
    # 调 prompt 看延迟/错误率、容量规划看调用量, 都从这里取数; 不做持久化（重启清零）。
    _stats: Dict = {
        "started_at": time.time(),
        "talk": {"total": 0, "errors": 0, "latency_ms_total": 0,
                 "latency_ms_max": 0, "last_latency_ms": 0, "llm": 0, "rules": 0},
        "tts": {"total": 0, "errors": 0},
        "task": {"total": 0, "errors": 0},
        "memory_save": {"total": 0, "errors": 0},
        "sse_clients": 0,
        "sse_clients_peak": 0,
    }

    def _stat(bucket: str, ok: bool = True, latency_ms: Optional[int] = None) -> None:
        """单桶计数: total/errors 必记; latency 三指标可选。"""
        s = _stats[bucket]
        s["total"] += 1
        if not ok:
            s["errors"] += 1
        if latency_ms is not None:
            s["latency_ms_total"] = s.get("latency_ms_total", 0) + latency_ms
            s["latency_ms_max"] = max(s.get("latency_ms_max", 0), latency_ms)
            s["last_latency_ms"] = latency_ms

    # ── §17 子代理观测: B2/A 经钩子上报 → /api/stats["subagent"]（零反向依赖）──
    from npc import subagent as _subagent_mod
    _stats["subagent"] = {}

    def _subagent_observer(name: str, res, npc_id: str) -> None:
        b = _stats["subagent"].setdefault(
            name, {"total": 0, "errors": 0, "latency_ms_total": 0,
                   "latency_ms_max": 0, "last_latency_ms": 0})
        b["total"] += 1
        if not res.ok:
            b["errors"] += 1
        b["latency_ms_total"] += getattr(res, "latency_ms", 0)
        b["latency_ms_max"] = max(b["latency_ms_max"], getattr(res, "latency_ms", 0))
        b["last_latency_ms"] = getattr(res, "latency_ms", 0)

    _subagent_mod.set_observer(_subagent_observer)

    @app.post("/api/npc/register", dependencies=[Depends(_verify_origin)])
    async def register_npc(request: Request) -> Dict:
        """动态注册(GTA 前置 #1): ped 随刷随出热注册。幂等 — 重复注册返回 existed。

        body: {persona: {id 必填, name/identity/voice/rules... 可选},
               persistent: false=流民(despawn 即忘)/true=常驻(反注册落盘),
               use_llm: 规则模式开关(默认 true),
               world_id: 可选,多世界守卫用}
        id 白名单 [A-Za-z0-9_-]{1,32}(store 文件名由 id 拼出,防路径穿越)。
        """
        body = await request.json()
        guard_world(body)
        persona_in = body.get("persona") if isinstance(body.get("persona"), dict) else {}
        pid = persona_in.get("id") or body.get("npc_id") or ""
        persistent = bool(body.get("persistent", False))
        use_llm = bool(body.get("use_llm", True))
        if not isinstance(pid, str) or not _DYNAMIC_ID_RE.match(pid or ""):
            raise HTTPException(status_code=400,
                                detail="id 不合法: 1~32 位字母/数字/_/-")
        if pid in npcs:   # 幂等: ped 反复进出同步半径,已注册直接 OK
            return {"ok": True, "npc_id": pid, "existed": True}
        if len(_dynamic) >= MAX_DYNAMIC_NPCS:
            raise HTTPException(status_code=429, detail=f"动态 NPC 已满({MAX_DYNAMIC_NPCS})")
        # 人设合并: 兜底默认 + 游戏传入覆盖
        merged = dict(_DEFAULT_DYNAMIC_PERSONA)
        merged.update(persona_in)
        merged["id"] = pid
        if not merged.get("name"):
            merged["name"] = pid   # 没传名字 → 用 id 当名(程序化人设最少只需 id)
        from npc.persona import build_system_prompt
        from npc.world import actor_of
        store_dir = str(next(iter(npcs.values())).store_path.parent) if npcs else "npc/store"
        if persistent and (Path(store_dir) / f"{pid}_memory.json").exists():
            npc = NPC.load(pid, store_dir=store_dir)   # 常驻续前缘: 读记忆卡
            npc.persona = merged
            npc.system_prompt = merged.get("system_prompt_override") or build_system_prompt(merged)
            npc.world = world
            actor_of(world, pid)
            npc.use_llm = use_llm
            npc.ephemeral = False
        else:
            npc = NPC(persona=merged, world=world, store_dir=store_dir,
                      use_llm=use_llm, ephemeral=not persistent)
        npcs[pid] = npc
        _dynamic.add(pid)
        return {"ok": True, "npc_id": pid, "existed": False, "persistent": persistent}

    @app.post("/api/npc/unregister", dependencies=[Depends(_verify_origin)])
    async def unregister_npc(request: Request) -> Dict:
        """反注册(ped despawn): 移除 NPC + 世界 actor 槽。常驻层先落盘(下次注册续前缘);
        流民直接删(RAM 即忘)。cast 静态角色(cang/ali)不可反注册 — 404。"""
        body = await request.json()
        guard_world(body)
        pid = body.get("npc_id", "")
        if pid not in _dynamic:
            raise HTTPException(status_code=404, detail=f"没有动态 NPC: {pid}")
        npc = npcs.pop(pid)
        _dynamic.discard(pid)
        if not npc.ephemeral:
            npc.save()   # 常驻层离场落盘
        world["actors"].pop(pid, None)
        return {"ok": True, "npc_id": pid, "persistent": not npc.ephemeral}

    # ── API ──────────────────────────────────────────

    @app.get("/api/npcs", dependencies=[Depends(_verify_origin)])
    async def list_npcs() -> Dict:
        """NPC 列表（前端切换角色用）。"""
        return {"npcs": [{"id": n.persona["id"], "name": n.persona.get("name", n.persona["id"])} for n in npcs.values()]}

    @app.get("/api/npc", dependencies=[Depends(_verify_origin)])
    async def get_npc_info(npc_id: str = "cang") -> Dict:
        """人设（页面数据驱动 — 制作者换 NPC 只需换这份数据）。"""
        p = get_npc(npc_id).persona
        return {
            "id": p["id"],
            "name": p.get("name", p["id"]),
            "role": p.get("identity", ""),
            "personality": p.get("personality", ""),
            "speech": p.get("speech_style", ""),
            "taboos": "、".join(p.get("taboos", [])),
            "intro": f"你走进了{p.get('name', p['id'])}所在的地方。",
        }

    @app.get("/api/state", dependencies=[Depends(_verify_origin)])
    async def get_state() -> Dict:
        """世界状态: 全部 NPC 的位置/背包/状态 + 已交付 + tick 计数 + 事件日志尾部。

        panels(§16 数据驱动界面): 本游戏该显示哪些状态面板 — 世界 JSON 的
        "_hud": {"panels": [...]} 声明, 不声明 = 默认全开(向后兼容)。
        调试台等界面照单渲染, 换游戏不再出现"阿曼达没有耐力/背包"的错位。
        """
        first = next(iter(npcs.values()))
        _hud = first.world.get("_hud") or {}
        return {
            "world_id": world_id,
            "panels": list(_hud.get("panels") or _DEFAULT_HUD_PANELS),
            "actors": {
                nid: {
                    "position": n.world["actors"][nid]["position"],
                    "inventory": n.world["actors"][nid]["inventory"],
                    "state": n.state,                    # idle/walking/working/resting
                    "stamina": n.world["actors"][nid].get("stamina", 100),   # 耐力(2026-08-22)
                    "activity": n.activity_desc(),       # 展示串，无活动 = ""
                }
                for nid, n in npcs.items()
            },
            "delivered": first.world["delivered"],
            "tick": first.world.get("_tick", 0),
            "log_tail": first.world["log"][-10:],        # 气泡差分用（碰面说话）
        }

    # ── §18 冷层归档: 事件收集统一收口（轮询/SSE 共用）────────────────
    def _log_tail_cfg() -> int:
        """NPC_LOG_TAIL 环境变量（0=关闭轮转）; 非法值回退默认。每次现读可热切。"""
        try:
            return max(0, int(os.environ.get("NPC_LOG_TAIL", str(LOG_TAIL_DEFAULT))))
        except ValueError:
            return LOG_TAIL_DEFAULT

    def _archive_dir() -> str:
        return os.environ.get("NPC_LOG_ARCHIVE_DIR", "npc/store/log_archive")

    def _archived_events(world, since: int, offset: int) -> List[Dict]:
        """冷回放: since < offset 的段落从归档 jsonl 读回（游标契约跨轮转不破）。"""
        path = Path(_archive_dir()) / "log_archive.jsonl"
        if not path.exists():
            return []
        out: List[Dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for ln in fh:
                    try:
                        rec = json.loads(ln)
                    except Exception:
                        continue
                    i = rec.get("i")
                    if isinstance(i, int) and since <= i < offset:
                        ev = _parse_log_line(str(rec.get("text", "")))
                        if ev is not None:
                            out.append(ev)
        except Exception as exc:
            log.warning("log_archive_read_failed", error=str(exc))
        return out

    def _events_since(first, since: int):
        """先冷层轮转（超阈值搬头部进归档），再做偏移感知的增量收集。

        返回 (events[], log_count) — log_count = offset + len(log) 是**绝对流位置**,
        客户端游标语义与旧版完全一致（旧游标是绝对索引, 轮转后依然有效）。
        """
        tail = _log_tail_cfg()
        if tail > 0:
            try:
                rotate_world_log(first.world, tail=tail, archive_dir=_archive_dir())
            except Exception as exc:
                # 写失败 → 放弃本轮轮转, 内存照旧增长 — 绝不因归档丢事件
                log.warning("world_log_rotate_failed", error=str(exc))
        log_list = first.world["log"]
        offset = int(first.world.get("_log_offset", 0))
        total = offset + len(log_list)
        since = max(0, min(since, total))   # 游标边界: 负数/超界都收拢到合法区间
        events: List[Dict] = []
        if since < offset:
            events.extend(_archived_events(first.world, since, offset))
        for line in log_list[max(0, since - offset):]:
            ev = _parse_log_line(line)
            if ev is not None:
                events.append(ev)
        return events, total

    @app.get("/api/events", dependencies=[Depends(_verify_origin)])
    async def get_events(since: int = 0) -> Dict:
        """事件协议: 返回 since 之后的世界日志增量（结构化事件流）。

        Codex core/界面协议化对照: 界面只消费事件, 不做状态差分。
        客户端轮询这里拿增量事件驱动气泡/表演/交付动画, 无需自己 diff log/delivered。
        游标 = 世界日志流的绝对索引 (since 从 0 开始), 返回 log_count 供下次回传。
        §18 冷层归档: 超过 NPC_LOG_TAIL 条的头部自动搬盘, 但游标语义不变 —
        归档段(since<offset)由服务端从 log_archive.jsonl 回放, 客户端无感。

        事件类型:
          say     = 谁说了什么 (气泡)
          move    = 谁去了哪 (走路表演)
          gather  = 谁采了资源
          craft   = 谁制作了什么
          deliver = 谁把资源交给了主角 (交付动画)
          subagent= B2/A 子代理生命周期 (§17)
        """
        first = next(iter(npcs.values()))
        events, total = _events_since(first, since)
        return {
            "events": events,
            "log_count": total,
            "tick": first.world.get("_tick", 0),
            "delivered": first.world["delivered"],
        }

    @app.get("/api/events/stream", dependencies=[Depends(_verify_origin)])
    async def events_stream(request: Request, since: int = 0):
        """SSE 增量事件流（推送通道 v1, 2026-08-23）— 轮询的升级替代,按需采用。

        text/event-stream; 每条结构化事件一帧 `data: {...}`(与 /api/events 同 schema);
        每 ~15s 一条注释心跳(`: ping`)防中间层断连; 客户端断开自动收尾。
        零新依赖(uvicorn/starlette 原生); WebSocket 等真需要双向时再上。
        """
        first = next(iter(npcs.values()))

        async def gen():
            _stats["sse_clients"] += 1
            _stats["sse_clients_peak"] = max(_stats["sse_clients_peak"], _stats["sse_clients"])
            cursor = max(0, since)
            idle = 0
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    evs, total = _events_since(first, cursor)   # §18: 轮转+偏移+冷回放统一收口
                    for ev in evs:
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if total > cursor or evs:
                        cursor = total
                        idle = 0
                    else:
                        idle += 1
                        if idle % SSE_HEARTBEAT_IDLE == 0:
                            yield ": ping\n\n"
                    await asyncio.sleep(SSE_SCAN_INTERVAL)
            finally:
                _stats["sse_clients"] -= 1

        return StreamingResponse(gen(), media_type="text/event-stream")

    def _parse_log_line(line: str) -> Optional[Dict]:
        """把一条 world.log 文本解析成结构化事件。解析不到 → None。"""
        m = re.match(r"^(\S+)\s+说:\s*(.+)$", line)          # "cang 说: 你好"
        if m:
            return {"type": "say", "npc": m.group(1), "text": m.group(2)}
        m = re.match(r"^(\S+)\s+前往\s+(.+)$", line)          # "cang 前往 森林"
        if m:
            return {"type": "move", "npc": m.group(1), "dest": m.group(2)}
        m = re.match(r"^(\S+)\s+采集了\s+1\s+个(.+)$", line)  # "cang 采集了 1 个木材"
        if m:
            return {"type": "gather", "npc": m.group(1), "resource": m.group(2)}
        m = re.match(r"^(\S+)\s+制作了\s+(.+)$", line)          # "cang 制作了 木石工具"
        if m:
            return {"type": "craft", "npc": m.group(1), "product": m.group(2)}
        m = re.match(r"^(\S+)\s+将\s+(.+?)\s+交给了\s+(.+)$", line)  # "cang 将 木材 交给了 主角"
        if m:
            return {"type": "deliver", "npc": m.group(1), "resource": m.group(2), "to": m.group(3)}
        m = re.match(r"^\[(B2|A)\]\s+(\S+)\s+(.+)$", line)   # "[B2] cang b2_compiler ✓ ..."（§17）
        if m:
            return {"type": "subagent", "agent": m.group(1).lower(),
                    "npc": m.group(2), "text": m.group(3)}
        return None

    @app.post("/api/tick", dependencies=[Depends(_verify_origin)])
    async def manual_tick(request: Request) -> Dict:
        """手动推一帧自主循环（测试/演示用）。body 可选 {"seed": int} 保证确定性。"""
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        seed = body.get("seed") if isinstance(body, dict) else None
        rng = random.Random(seed) if isinstance(seed, int) else random.Random()
        events = tick_round(world, npcs, rng=rng)
        _save_on_transitions(npcs, events)
        return {"tick": world.get("_tick", 0), "events": events}

    @app.get("/api/memory", dependencies=[Depends(_verify_origin)])
    async def get_memory(npc_id: str = "cang") -> Dict:
        """记忆（记忆=可编辑文档 — 条目直接来自记忆卡）。"""
        return {"entries": get_npc(npc_id).memory.all()}

    @app.post("/api/memory", dependencies=[Depends(_verify_origin)])
    async def save_memory(request: Request) -> Dict:
        """记忆回写（2026-08-23 §15 — 「记忆=可编辑文档」的写通道, 调试台配套）。

        body: {npc_id, world_id?, entries: [{content 必填, importance 0-9?,
               category?, created_at?, id?}]} → 整表替换该 NPC 记忆并落盘。
        语义 = 调试台整卡编辑; 生产游戏端请勿用它批量改写人格记忆。
        content 截断 2000 字; 非法条目整体 400（不半套生效）。
        """
        body = await request.json()
        guard_world(body)
        npc = get_npc(body.get("npc_id", "cang"))
        entries_in = body.get("entries")
        if not isinstance(entries_in, list):
            raise HTTPException(status_code=400, detail="entries 必须是数组")
        now = time.time()
        cleaned: list = []
        for i, e in enumerate(entries_in):
            if not isinstance(e, dict):
                raise HTTPException(status_code=400, detail=f"entries[{i}] 不是对象")
            content = str(e.get("content", "")).strip()
            if not content:
                raise HTTPException(status_code=400, detail=f"entries[{i}].content 不能为空")
            try:
                imp = int(e.get("importance", 5))
            except (TypeError, ValueError):
                imp = 5
            try:
                created = float(e.get("created_at") or now)
            except (TypeError, ValueError):
                created = now
            cleaned.append({
                "id": str(e.get("id") or f"mem_{i + 1}_{int(now)}"),
                "content": content[:2000],
                "importance": max(0, min(9, imp)),
                "category": str(e.get("category") or "general"),
                "created_at": created,
            })
        npc.memory.load(cleaned)
        npc.save()
        _stat("memory_save")
        return {"ok": True, "count": len(cleaned)}

    @app.get("/api/version", dependencies=[Depends(_verify_origin)])
    async def version() -> Dict:
        """版本/特性握手（2026-08-23 §15）: 客户端启动探测一次按特性降级。

        features.sse=false 的老客户端继续轮询; multi_world=false 说明单世界模式
        （不必带 world_id）。以后加功能在这里挂 True, 老客户端不受影响。
        """
        from npc import memory as _npc_memory   # 探测可选依赖: jieba 分词可用性
        return {
            "name": "NPCSidekick Brain",
            "version": BRAIN_VERSION,
            "world_id": world_id,
            "features": {
                "events_polling": True,
                "events_sse": True,
                "multi_world": bool(world_id),
                "dynamic_lexicon": True,
                "manifest_resources": True,
                "memory_edit": True,
                "stats": True,
                "jieba_tokenize": getattr(_npc_memory, "jieba", None) is not None,
                "tts": tts_available(),
                # §17 子代理: 运行时开关状态（bat 里 NPC_SUBAGENT_B2/A=1 打开）
                "subagent_roles": {
                    "b2": _subagent_mod.subagent_enabled("B2"),
                    "a": _subagent_mod.subagent_enabled("A"),
                },
                # §18 冷层归档: 事件游标跨轮转恒有效（服务端回放冷段）
                "log_archive": True,
                # 任务书 #01：LLM 调度队列开关（NPC_SCHEDULER，默认 OFF）
                "scheduler": SCHED.enabled,
            },
        }

    @app.get("/api/stats", dependencies=[Depends(_verify_origin)])
    async def stats() -> Dict:
        """观测端点（2026-08-23 §15）: 调用量/延迟/错误/SSE 连接数。

        用途: 调 prompt 对比延迟、看 LLM vs 规则占比、容量规划。内存态, 重启清零;
        持久化/时序库等真需要时再上（先攒基线数据）。
        """
        first = next(iter(npcs.values()))
        payload: Dict = dict(_stats)
        payload.update({
            "version": BRAIN_VERSION,
            "uptime_sec": int(time.time() - _stats["started_at"]),
            "tick": first.world.get("_tick", 0),
            "mode": _effective_mode(),
            "npcs": len(npcs),
            "world_id": world_id,
            # §18 冷层: 已归档的事件条数（内存 log 只留尾部）
            "log_offset": int(first.world.get("_log_offset", 0)),
            # 任务书 #01：LLM 调度队列观测（enabled/depth/waits/timeouts/avg_wait_ms）
            "scheduler": SCHED.snapshot(),
        })
        return payload

    @app.post("/api/talk", dependencies=[Depends(_verify_origin)])
    async def talk(request: Request) -> Dict:
        body = await request.json()
        guard_world(body)
        message = body.get("message", "")
        if not message:
            raise HTTPException(status_code=400, detail="message 不能为空")
        npc = get_npc(body.get("npc_id", "cang"))
        _apply_context(npc.world, body.get("context"))   # 游戏→大脑世界同步(2026-08-22)
        _mode_at = _effective_mode()   # 请求时快照（模式可能被并发切换）
        _t0 = time.time()
        try:
            # 任务书 #01：整个 npc.talk 以 P_TALK 入队（对话永远最优先）；
            # 排队超时 → SchedulerTimeout → 落回本地规则兜底，绝不卡客户端。
            # §20未决#3 落地(2026-08-25): 排队超时可调(NPC_TALK_QUEUE_TIMEOUT 秒);
            # 缺省/非法值一律 60s —— 默认行为与旧版完全一致;
            # GTA bat 放宽到 110s(网关慢日实测 100s+, mod HTTP 上限 120s)多吃真 LLM。
            try:
                _talk_timeout = float(os.environ.get("NPC_TALK_QUEUE_TIMEOUT") or 60.0)
            except ValueError:
                _talk_timeout = 60.0
            reply = await SCHED.run(P_TALK, npc.talk, message,
                                    reasoning=body.get("thinking"), timeout=_talk_timeout)
        except SchedulerTimeout:
            log.warning("npc_talk_queue_timeout", npc=npc.actor_id)
            npc._last_thinking = ""
            reply = npc._talk_rules(message)
            _stat("talk", ok=True, latency_ms=int((time.time() - _t0) * 1000))
            _stats["talk"]["rules"] += 1
        except Exception:
            _stat("talk", ok=False)
            raise
        else:
            _stat("talk", ok=True, latency_ms=int((time.time() - _t0) * 1000))
            _stats["talk"]["llm" if _mode_at == "llm" else "rules"] += 1
        result: Dict = {"reply": reply}
        # 思考可视化: 模型思考内容(规则回退/无思考模型时为空串)
        thinking_text = getattr(npc, "_last_thinking", "") or ""
        if thinking_text:
            result["thinking_text"] = thinking_text[:2000]
        # 语音 = 按需输出通道: 请求带 voice=true 才合成（默认纯文本，保持零回归）
        if body.get("voice") and tts_available():
            audio = await _tts_synthesize_safe(reply, tts_voice_for(npc.persona, npc.actor_id))
            if audio:
                result["audio"] = tts_to_base64(audio)
        return result

    @app.post("/api/tts", dependencies=[Depends(_verify_origin)])
    async def tts_endpoint(request: Request) -> Dict:
        """语音合成（独立端点）: 游戏可给任意文本配音（含本地对话表/头顶气泡）。

        请求: {"text": "...", "npc_id": "cang"} → {"audio": "<base64 mp3>"}。
        未装 edge-tts → 503；合成失败 → audio 为空串。
        """
        if not tts_available():
            raise HTTPException(status_code=503, detail="edge-tts 未安装")
        body = await request.json()
        guard_world(body)
        text = (body.get("text") or "").strip()
        if not text:
            _stat("tts", ok=False)
            raise HTTPException(status_code=400, detail="text 不能为空")
        npc = get_npc(body.get("npc_id", "cang"))
        _stat("tts")
        audio = await _tts_synthesize_safe(text, tts_voice_for(npc.persona, npc.actor_id))
        return {"audio": tts_to_base64(audio) if audio else ""}

    def _effective_mode() -> str:
        """实际生效模式: LLM 模式但无 key/初始化失败 → 退化规则。"""
        first = next(iter(npcs.values()))
        return "llm" if (first.use_llm and first._get_llm() is not None) else "rules"

    @app.get("/api/mode", dependencies=[Depends(_verify_origin)])
    async def get_mode() -> Dict:
        """当前对话模式（规则 / LLM）— 全局设置，所有 NPC 一致。"""
        return {"mode": _effective_mode(), "requested": "llm" if next(iter(npcs.values())).use_llm else "rules"}

    @app.post("/api/mode", dependencies=[Depends(_verify_origin)])
    async def set_mode(request: Request) -> Dict:
        """切换对话模式 — 演示开关: 规则模式 ↔ LLM 模式（作用于所有 NPC）。"""
        body = await request.json()
        mode = body.get("mode", "")
        if mode not in ("rules", "llm"):
            raise HTTPException(status_code=400, detail="mode 必须是 rules 或 llm")
        for npc in npcs.values():
            npc.use_llm = (mode == "llm")
        return {"mode": _effective_mode(), "requested": mode}

    @app.get("/api/approval", dependencies=[Depends(_verify_origin)])
    async def get_approval(npc_id: str = "") -> Dict:
        """审批策略视图（Codex /approvals 对照）: 全局档位 + 各动作生效决定。
        带 npc_id = 该 NPC 粒度; 缺省 = 全局会话级。"""
        first = next(iter(npcs.values()))
        if npc_id and npc_id in npcs:
            return {"policy": npcs[npc_id].approval_policy,
                    "actions": approval_table(npcs[npc_id].approval_policy,
                                              npcs[npc_id].approval_overrides),
                    "npc_id": npc_id}
        return {"policy": first.approval_policy,
                "actions": approval_table(first.approval_policy)}

    @app.post("/api/approval", dependencies=[Depends(_verify_origin)])
    async def set_approval_api(request: Request) -> Dict:
        """运行时调整审批策略（Codex /approvals 对照）。带 npc_id = 按 NPC 粒度; 缺省 = 全局。"""
        body = await request.json()
        npc_id = body.get("npc_id", "")
        if npc_id and npc_id in npcs:
            ok = npcs[npc_id].set_approval(body.get("action", ""), body.get("decision", ""))
            if not ok:
                raise HTTPException(status_code=400, detail="action 或 decision 不合法")
            return {"ok": True, "npc_id": npc_id,
                    "actions": approval_table(npcs[npc_id].approval_policy,
                                              npcs[npc_id].approval_overrides)}
        ok = set_approval(body.get("action", ""), body.get("decision", ""))
        if not ok:
            raise HTTPException(status_code=400, detail="action 或 decision 不合法")
        first = next(iter(npcs.values()))
        return {"ok": True, "actions": approval_table(first.approval_policy)}

    @app.get("/api/manifest", dependencies=[Depends(_verify_origin)])
    async def manifest_get() -> Dict:
        """当前生效动作清单（动作集由游戏声明; 默认=本游戏动作）。"""
        return {"is_default": manifest_is_default(), "actions": get_manifest()}

    @app.post("/api/manifest", dependencies=[Depends(_verify_origin)])
    async def manifest_set(request: Request) -> Dict:
        """游戏声明自己的动作清单: {"actions": {...}} 或裸动作表; {reset: true} 恢复默认。"""
        body = await request.json()
        if body.get("reset"):
            load_manifest(None)
            return {"ok": True, "is_default": True, "actions": get_manifest()}
        try:
            parsed = parse_manifest_doc(body.get("actions") if "actions" in body else body)
            load_manifest(parsed["actions"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "is_default": manifest_is_default(), "actions": get_manifest()}

    @app.post("/api/task", dependencies=[Depends(_verify_origin)])
    async def task(request: Request) -> Dict:
        """派发采集任务（规则快路径，零 LLM）— 指定 NPC。"""
        body = await request.json()
        guard_world(body)
        resource = body.get("resource", "")
        count = int(body.get("count", 1))
        npc = get_npc(body.get("npc_id", "cang"))
        _stat("task")
        ok = npc.run_gather_task(resource, count)
        steps = npc.task_log[-1]["steps"] if npc.task_log else []
        return {"ok": ok, "steps": steps}

    # ── 静态页面（最后挂载；路径锚定代码位置）────────────────
    app.mount("/", StaticFiles(directory=str(_BASE_DIR / "web" / "static"), html=True), name="static")
    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="NPCSidekick Web 服务")
    parser.add_argument("--adapter", type=str, default="",
                        help="加载游戏适配器角色表，如 paleolithic（旧石器游戏村民）")
    parser.add_argument("--persona-dir", type=str, default="",
                        help="从目录加载人格 JSON 文件（制作者放文件即用，覆盖默认/适配器角色表）")
    parser.add_argument("--no-browser", action="store_true",
                        help="启动时不自动打开浏览器（游戏运行时用）")
    parser.add_argument("--manifest", type=str, default="",
                        help="游戏动作清单 JSON 路径(每游戏一份; 缺省=内置默认动作)")
    parser.add_argument("--port", type=int, default=8765,
                        help="监听端口(默认 8765; 多世界同开时各占一个端口)")
    parser.add_argument("--world-id", dest="world_id", default="",
                        help="世界命名空间(如 godot/gta): 独立 store 目录 + 请求 world_id 守卫")
    parser.add_argument("--store-dir", dest="store_dir", default="",
                        help="记忆卡目录(缺省: 有 world-id 用 npc/store_<id>, 否则 npc/store)")
    args = parser.parse_args()

    personas = None
    adapter_world = None   # 适配器自定义世界(有则全局共用)
    if args.persona_dir:
        from npc.persona_loader import load_personas_from_dir

        # 相对路径锚定代码位置（游戏从任意目录拉起时也能找到）
        p = Path(args.persona_dir)
        if not p.is_absolute():
            p = _BASE_DIR / p
        personas = load_personas_from_dir(str(p))
        print(f"已从 {args.persona_dir} 加载人格: {list(personas.keys())}")
        if not personas:
            print("警告: 目录没有有效 JSON，回退默认角色表")
    elif args.adapter:
        from importlib import import_module

        mod = import_module(f"npc.adapters.{args.adapter}")
        personas = mod.VILLAGERS
        print(f"已加载适配器角色表: {args.adapter}（{list(personas.keys())}）")
        # 适配器自定义世界(2026-08-22 GTA): 有 WORLD 属性就用 — 换游戏换世界,零代码
        if hasattr(mod, "WORLD"):
            adapter_world = mod.WORLD   # type: ignore[attr-defined]
            print(f"已加载适配器世界: {args.adapter}（{list(adapter_world['locations'].keys())}）")

    # 动作清单: 游戏接入点 — env NPC_MANIFEST 或 --manifest <json>, 缺省=默认动作
    # v2026-08-23 修: 正确拆 {"actions":...,"resources":...} 外壳(此前整包塞给
    # load_manifest 会校验失败静默退回默认清单); resources 段暂存待并入资源词典。
    _manifest_path = args.manifest or os.environ.get("NPC_MANIFEST", "")
    if _manifest_path:
        import json as _json
        _p = Path(_manifest_path)
        if not _p.is_absolute():
            _p = _BASE_DIR / _p
        try:
            with open(_p, "r", encoding="utf-8") as _f:
                _parsed = parse_manifest_doc(_json.load(_f))
            load_manifest(_parsed["actions"])
            set_manifest_resources(_parsed.get("resources"))
            print(f"已加载动作清单: {_p}"
                  + (f"（含资源词典 {len(_parsed['resources'])} 项）" if _parsed.get("resources") else ""))
        except (OSError, ValueError) as _e:
            print(f"警告: 动作清单加载失败({_e}), 使用默认动作")

    port = args.port
    # 多世界隔离: 独立端口 + 独立记忆卡目录（GTA 和 Godot 同开互不打架）
    if args.store_dir:
        _store = Path(args.store_dir)
    elif args.world_id:
        _store = Path(f"npc/store_{args.world_id}")
    else:
        _store = Path("npc/store")
    if not _store.is_absolute():
        _store = _BASE_DIR / _store

    world, npcs = load_village(store_dir=str(_store), personas=personas,
                                world=adapter_world)
    # 动态资源词典(2026-08-23): 世界就绪后从 locations 收集 + 清单 resources 并名 —
    # 游戏加新资源改世界 JSON/清单表即可, 对话接单立刻认识, 零代码。
    lex = load_resource_lexicon_from_world(world, extra=get_manifest_resources())
    print(f"资源词典已就绪: {sorted(lex.keys())}")

    app = create_npc_server(npcs, world_id=args.world_id)
    log.info("npc_server_started", url=f"http://127.0.0.1:{port}/npc.html")
    print(f"NPCSidekick[{args.world_id or 'default'}]: http://127.0.0.1:{port}/npc.html"
          f"  (store={_store})")
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/npc.html")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
