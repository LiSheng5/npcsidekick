"""
NPCSidekick — Web 服务（制作者体验优先，参考 AI Town 的 one-command 启动）。

一条命令:  python -m npc.server [--adapter gta] [--port 8765] [--world-id gta]
  - 自动打开浏览器 http://127.0.0.1:8765/ （根路径指到 Web Console `/console/`;
    dist 未构建时返回提示 JSON, 不再回落旧 npc.html — 该页 2026-09-08 已下架）
  - API 全量以代码为准(40 条): 本文件(游戏面) + npc/console_api.py(Console 面);
    游戏接入最少三条: /api/talk /api/state /api/task

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
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Dict, List, Optional

# 代码位置锚定的项目根（cwd 无关 — 游戏自动拉起时从任意目录启动都能找到角色/记忆卡/页面）
_BASE_DIR = Path(__file__).resolve().parents[1]

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent.logging_config import log
from agent.llm.client import llm_retry_enabled
from npc import safety as _safety
from npc import taskloop as _taskloop
from npc import events_archive
from npc import housekeeper as _hk
from npc.memory import EV_DONE, EV_FAIL
from npc.npc import NPC, memory_dedup_enabled
from npc.reviewer import (approval_table, set_approval, get_manifest,
                          load_manifest, manifest_is_default,
                          parse_manifest_doc, set_manifest_places,
                          set_manifest_resources, get_manifest_resources)
from npc.scheduler import SCHED, P_TALK, SchedulerTimeout, tick_round

from npc.tts import available as tts_available
from npc.tts import synthesize as tts_synthesize
from npc.tts import to_base64 as tts_to_base64
from npc.tts import voice_for as tts_voice_for

from npc.console_api import ConsoleContext, mount_console_api

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


def pump_task_dispatch(world_obj: Dict) -> int:
    """世界推进一帧时的任务派发泵（任务书#05·A）: 驱动 booked→dispatched 转换,
    并把转换事件落进世界日志。

    派发不再依赖客户端 poll /api/state —— 世界推进(tick_loop 每帧 / 手动 tick)
    自己就会派发并记账, mod 断连或只走 /api/events 也丢不了事件。
    返回本次落盘的日志条数。
    """
    _taskloop.LEDGER.dispatch_view()
    return flush_task_events(world_obj)


def flush_task_events(world_obj: Dict) -> int:
    """把账本派发事件队列落进世界日志（任务书#05·A）。

    派发(booked→dispatched)在账本里只入队不写日志 —— 日志落盘改由消费方驱动:
      · 主 flush 点 = _tick_loop 每帧 → mod 断连 / 客户端只走 /api/events 也不丢;
      · 即时 flush 点 = 事件流端点(/api/events + SSE) → 事件零延迟可见。
    pop 即消费 → 多客户端 poll / 断连重连都只写一次日志, 不重复。
    日志行格式与 events_archive.parse_log_line 的 task 正则对齐（协议零破坏）。
    账本故障降级: 不抛异常, 最多是这一帧少一条日志。
    """
    try:
        events = _taskloop.LEDGER.pop_dispatch_events()
    except Exception:
        return 0
    if not events:
        return 0
    log_list = world_obj.setdefault("log", [])
    for ev in events:
        log_list.append(f"{ev['npc_id']} 接下任务: {ev['desc']}")
    return len(events)

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


def _game_hour_now(world: Dict) -> int:
    """当前游戏小时（0-23）: 真实同步优先, 未同步回退 tick 自推 (8 + tick//60) % 24。"""
    gh = world.get("_game_hour")
    if isinstance(gh, (int, float)) and int(gh) >= 0:
        return int(gh) % 24
    return (8 + int(world.get("_tick", 0)) // 60) % 24


def _is_dawn_boundary(prev_hour, now_hour) -> bool:
    """黎明边界: 小时从 5 跨向 6 → True（每游戏日只在那一帧触发）。"""
    return (prev_hour is not None and prev_hour != now_hour
            and now_hour == 6 and prev_hour == 5)


async def _tick_loop(world, npcs: Dict[str, NPC]) -> None:
    """后台自主循环（村民日常）: 每帧 tick_round + 转换点落盘。单帧异常不杀循环。"""
    tick_count = 0
    hk_gate = _hk.TriggerState()   # 任务书#04: 管家触发器状态机(每个循环独立)
    while True:
        await asyncio.sleep(_tick_interval())
        tick_count += 1
        hk_on = _hk.enabled()      # 每 tick 读一次(简洁性 review: 不再 3 次重复读)
        # 任务书#05·A: 每帧驱动派发 + 落日志 — 不依赖客户端 poll, mod 断连也不丢
        pump_task_dispatch(world)
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
            # 黎明整理(§26): 画像层是 LLM 慢变层 — 一日才修订一次。触发=
            # 游戏小时跨向 6:00 的边界帧（_is_dawn_boundary）; 管家开启时升级为
            # 全量大整理(压缩+归纳+画像), 关闭时保持旧行为(仅画像, 同一份实现
            # housekeeper.persona_batch)。与反思同款纪律: 含 LLM 调用必须整批
            # 挪进 to_thread, 不得冻事件循环。
            _gh = _game_hour_now(world)
            if _is_dawn_boundary(world.get("_prev_hour"), _gh):
                if hk_on:
                    await asyncio.to_thread(_hk.dawn, world, npcs)
                else:
                    await asyncio.to_thread(_hk.persona_batch, npcs)
            world["_prev_hour"] = _gh
            # 记忆管家(任务书#04, NPC_HOUSEKEEPER=1): 🍃 空闲小整理(零 LLM) +
            # ⏰ 快满应急(LLM 过 SCHED → 整批 to_thread, §19 冻循环教训)。
            # 触发判定收在 housekeeper.TriggerState(简洁性 review): 本层只接线。
            if hk_on:
                want = hk_gate.on_tick(busy=any(SCHED.snapshot()["depth"].values()))
                if want == "minor":
                    try:
                        await asyncio.to_thread(_hk.minor, world)
                    except Exception as exc:
                        log.warning("housekeeper_minor_failed", error=str(exc))
                elif want == "emergency":
                    limit = _hk.token_limit()
                    urgent = [n for n in npcs.values()
                              if not getattr(n, "ephemeral", False)
                              and _hk.should_emergency(n.memory, max_tokens=limit)]
                    if urgent:
                        await asyncio.to_thread(_hk.emergency_batch, urgent)
            # 协议 v1: 僵尸账回收(dispatched 超 NPC_TASK_TIMEOUT 未销账 → failed+商议)
            try:
                _taskloop.LEDGER.reap_zombies()
            except Exception as exc:
                log.warning("task_zombie_reap_failed", error=str(exc))
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
                      world_id: str = "",
                      personas_dir: str = "",
                      config_dir: str = "") -> FastAPI:
    """构建 NPCSidekick Web 服务（工厂函数，便于测试注入）。

    npcs: {id: NPC} 多 NPC 共享世界。默认加载示例村庄。
    world_id: 本实例的世界命名空间（多世界隔离; 空 = 单世界模式,不校验）。
    personas_dir: 制作者人设目录（npc/personas/<id>.json; 空 = 项目默认, 测试注入临时目录）。
    config_dir: Web Console 的 provider/密钥目录（空 = npc/config; 测试注入临时目录）。
    后台自主循环随 FastAPI lifespan 启动 — TestClient 不进 with 上下文则不启动（测试确定性）。
    """
    if npcs is None:
        world, npcs = load_village()
    else:
        world = next(iter(npcs.values())).world   # AI Town 模式: 共享世界

    personas_path = Path(personas_dir or _BASE_DIR / "npc" / "personas")

    def _console_ctx() -> ConsoleContext:
        """Web Console 的运行时上下文（每次现造 —— npcs/world 会被热加载增删）。

        供 create_persona（新建后热加载）与 mount_console_api（管理端点）共用，
        避免两处各写一份而漂移。
        """
        return ConsoleContext(
            npcs=npcs,
            world=world,
            personas_path=personas_path,
            store_dir=str(next(iter(npcs.values())).store_path.parent)
            if npcs else str(_BASE_DIR / "npc" / "store"),
            world_id=world_id,
            config_dir=Path(config_dir) if config_dir else (_BASE_DIR / "npc" / "config"),
            guard_world=lambda body: guard_world(body),
        )

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

    class _WorldHandle:
        """世界句柄占位: NPC 全删光时（Console 允许）事件/状态端点仍能读世界，
        不再 StopIteration。所有 NPC 共享同一个 world 对象，语义完全等价。"""

        def __init__(self, w: Dict):
            self.world = w

    def _world_handle():
        """取一个能拿到 .world 的句柄: 有 NPC 就用第一个，没有就用占位。"""
        return next(iter(npcs.values()), None) or _WorldHandle(world)

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
        """NPC 列表（前端切换角色用）。

        Web Console 扩展（2026-09-07，纯 additive — 老客户端只读 id/name 不受影响）:
        附带 identity/state/activity/position/memory_count，让 Character 列表
        不用再逐个人肉拉一遍。
        """
        return {"npcs": [_npc_brief(nid, n) for nid, n in npcs.items()]}

    def _npc_brief(nid: str, n: NPC) -> Dict:
        """单个 NPC 的列表视图（运行时现状，随取随新）。"""
        actor = n.world["actors"].get(nid, {})
        return {
            "id": n.persona["id"],
            "name": n.persona.get("name", n.persona["id"]),
            # ── 以下为 Console 扩展字段 ──
            "identity": n.persona.get("identity", ""),
            "state": n.state,
            "activity": n.activity_desc(),
            "position": actor.get("position", ""),
            "stamina": actor.get("stamina", 100),
            "memory_count": len(n.memory.all()),
            "has_memory_card": n.store_path.exists(),
            "ephemeral": getattr(n, "ephemeral", False),
            "use_llm": bool(getattr(n, "use_llm", True)),
        }

    # ── 制作者人设 CRUD（2026-08-27 关系网 "新建 NPC"）─────────────────────

    @app.get("/api/personas", dependencies=[Depends(_verify_origin)])
    async def list_personas() -> Dict:
        """人设清单（关系网画布数据源）: npc/personas/*.json → 全量 dict 列表。"""
        from npc.persona_loader import load_personas_from_dir
        return {"personas": list(load_personas_from_dir(str(personas_path)).values())}

    @app.post("/api/personas", dependencies=[Depends(_verify_origin)])
    async def create_persona(request: Request) -> Dict:
        """新建人设: body = persona JSON → 校验 → 写 <personas>/<id>.json。

        与 cang.json/ali.json 同格式（REQUIRED_FIELDS 缺一不可）。返回值 path 可见,
        关系网提示"重启大脑服务器生效"。已存在 → 409（防误覆盖制作者手改的人设）。
        """
        body = await request.json()
        pid = body.get("id", "") if isinstance(body, dict) else ""
        if not isinstance(pid, str) or not _DYNAMIC_ID_RE.match(pid):
            raise HTTPException(status_code=400,
                                detail="id 不合法: 1~32 位字母/数字/_/-")
        target = personas_path / f"{pid}.json"
        if target.exists():
            raise HTTPException(status_code=409, detail=f"人设已存在: {pid}（要改请直接编辑文件）")
        from npc.persona_loader import validate_persona_dict
        try:
            cleaned = validate_persona_dict(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        personas_path.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
        # 2026-09-08 (Web Console Step 5): 与 PUT /api/personas/{id} 口径一致 ——
        # 新建完立刻热加载进运行时。旧版返回"重启大脑服务器后生效"，
        # 对制作者工具等于"点了没反应"，Console 不该这样。
        # 注意: 写盘已成功 —— 热加载失败也不该让请求 500（用户的编辑不能丢），
        # 故降级为 hot_reloaded=false + 原因，让 UI 明说。
        from npc.console_api import hot_reload
        try:
            action = hot_reload(_console_ctx(), pid, cleaned)
            hot_reloaded = True
            err = ""
        except Exception as exc:                       # 绝不因热加载失败吞掉写盘结果
            action, hot_reloaded, err = "failed", False, str(exc)
        return {"ok": True, "npc_id": pid, "path": f"npc/personas/{pid}.json",
                "action": action, "hot_reloaded": hot_reloaded, "error": err}

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
    async def get_state(request: Request) -> Dict:
        """世界状态: 全部 NPC 的位置/背包/状态 + 已交付 + tick 计数 + 事件日志尾部。

        panels(§16 数据驱动界面): 本游戏该显示哪些状态面板 — 世界 JSON 的
        "_hud": {"panels": [...]} 声明, 不声明 = 默认全开(向后兼容)。
        调试台等界面照单渲染, 换游戏不再出现"阿曼达没有耐力/背包"的错位。
        """
        _consumer = request.query_params.get("consumer")
        if _consumer:
            _taskloop.REGISTRY.touch(_consumer)   # 轮询即心跳(可选 query 参数)
        _hud = world.get("_hud") or {}
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
            "delivered": world["delivered"],
            "tick": world.get("_tick", 0),
            "log_tail": world["log"][-10:],        # 气泡差分用（碰面说话）
            # 协议 v1·任务下发(booked→dispatched 首派标记) — mod 认领执行后 POST /api/task_done
            # 任务书#05·A: 本端点纯读无写副作用 —— 派发事件由账本入队,
            # _tick_loop 每帧 / 事件流端点 flush 进世界日志(断连也丢不了)。
            "pending_tasks": _taskloop.LEDGER.dispatch_view(),
        }


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
        flush_task_events(world)   # 任务书#05·A: 返回增量前落盘, 事件零延迟
        events, total = events_archive.events_since(_world_handle(), since)
        return {
            "events": events,
            "log_count": total,
            "tick": world.get("_tick", 0),
            "delivered": world["delivered"],
        }

    @app.get("/api/events/stream", dependencies=[Depends(_verify_origin)])
    async def events_stream(request: Request, since: int = 0):
        """SSE 增量事件流（推送通道 v1, 2026-08-23）— 轮询的升级替代,按需采用。

        text/event-stream; 每条结构化事件一帧 `data: {...}`(与 /api/events 同 schema);
        每 ~15s 一条注释心跳(`: ping`)防中间层断连; 客户端断开自动收尾。
        零新依赖(uvicorn/starlette 原生); WebSocket 等真需要双向时再上。
        """
        world_handle = _world_handle()   # 连接期内固定（NPC 增删不影响游标语义）

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
                    flush_task_events(world)   # 任务书#05·A
                    # 修复(2026-09-07): 原名 _events_since 未定义 → 一连 SSE 就 NameError。
                    # 统一走 events_archive.events_since(与 /api/events 同一实现)。
                    evs, total = events_archive.events_since(world_handle, cursor)
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
        pump_task_dispatch(world)   # 任务书#05·A: 推帧即派发(与 tick_loop 同款)
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

    @app.post("/api/consumer/hello", dependencies=[Depends(_verify_origin)])
    async def consumer_hello(request: Request) -> Dict:
        """协议v1·能力协商(M1): 消费者启动报到+心跳, 声明可执行动词表。

        mod 每 ≤30s 重发一次即心跳(NPC_CONSUMER_TTL 默认60s判死);
        B2 白名单 = manifest ∩ 活跃消费者并集, 空集时 Task 类请求拒绝编译(诚实回话)。
        """
        body = await request.json()
        guard_world(body)
        return _taskloop.REGISTRY.hello(str(body.get("name", "")),
                                        str(body.get("version", "")),
                                        body.get("verbs"))

    @app.post("/api/task_done", dependencies=[Depends(_verify_origin)])
    async def task_done(request: Request) -> Dict:
        """协议v1·销账(M1): mod 干完活回报。

        completed → 清 pending_task + 记忆卡("完成: …", imp=8);
        failed → 整链取消(账本内) + 记忆卡("没做成: …") + say 字幕推玩家商议。
        """
        body = await request.json()
        guard_world(body)
        t = _taskloop.LEDGER.settle(str(body.get("task_id", "")),
                                    str(body.get("status", "")),
                                    str(body.get("detail", "") or body.get("error", "") or ""))
        if t is None:
            return {"ok": False, "error": "unknown_or_terminal_task"}
        npc = npcs.get(t["npc_id"])
        if npc is not None:
            pt = getattr(npc, "pending_task", None)
            if isinstance(pt, dict) and pt.get("action") == t["action"]:
                npc.pending_task = None
            if t["state"] == "completed":
                npc.remember(f"{EV_DONE}{t['desc']}", importance=8)
                npc.world["log"].append(f"{t['npc_id']} 完成任务: {t['desc']}")
            elif t["state"] == "failed":
                npc.remember(f"{EV_FAIL}{t['desc']}（{t['error']}）", importance=6)
                for d in _taskloop.LEDGER.discussions(t["npc_id"])[-1:]:
                    npc.world["log"].append(f"{t['npc_id']} 说: {d['text']}")
        return {"ok": True, "task": t}

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
                    "a": False,   # §22 已退役(2026-08-25): 恒 False, 键保留防老客户端破坏
                },
                # §18 冷层归档: 事件游标跨轮转恒有效（服务端回放冷段）
                "log_archive": True,
                # 任务书 #01：LLM 调度队列开关（NPC_SCHEDULER，默认 OFF）
                "scheduler": SCHED.enabled,
                # 协议 v1·任务执行面(M1): hello 能力协商 + 任务账本 + 链式派发销账
                "task_loop": True,
                # P1-1 运维可观测(2026-08-25): 开关生效态一屏可见 —— bat 是否把
                # 开关带进进程不再靠猜(家规"默认关+选择加入"的运维闭环)
                "flags": {
                    "safety_gate": _safety.enabled(),
                    "scheduler_queue": SCHED.enabled,
                    "memory_dedup": memory_dedup_enabled(),
                    "task_loop_gate": _taskloop.gate_enabled(),
                    "llm_retry": llm_retry_enabled(),
                    "approval_policy": os.environ.get("NPC_APPROVAL_POLICY", "auto"),
                    "dialogue_model": os.environ.get("NPC_DIALOGUE_MODEL",
                                                     "deepseek-v4-flash"),
                    "review_model": os.environ.get("NPC_REVIEW_MODEL",
                                                   "deepseek-v4-flash"),
                    "tick_interval_sec": _tick_interval(),
                },
            },
        }

    @app.get("/api/stats", dependencies=[Depends(_verify_origin)])
    async def stats() -> Dict:
        """观测端点（2026-08-23 §15）: 调用量/延迟/错误/SSE 连接数。

        用途: 调 prompt 对比延迟、看 LLM vs 规则占比、容量规划。内存态, 重启清零;
        持久化/时序库等真需要时再上（先攒基线数据）。
        """
        first = _world_handle()
        payload: Dict = dict(_stats)
        payload.update({
            "version": BRAIN_VERSION,
            "uptime_sec": int(time.time() - _stats["started_at"]),
            "tick": world.get("_tick", 0),
            "mode": _effective_mode(),
            "npcs": len(npcs),
            "world_id": world_id,
            # §18 冷层: 已归档的事件条数（内存 log 只留尾部）
            "log_offset": int(world.get("_log_offset", 0)),
            # 任务书 #01：LLM 调度队列观测（enabled/depth/waits/timeouts/avg_wait_ms）
            "scheduler": SCHED.snapshot(),
            # 协议 v1·任务账本观测(total/by_state/pending_discussions)
            "task_loop": _taskloop.LEDGER.stats(),
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
        first = next(iter(npcs.values()), None)
        if first is None:
            return "rules"        # 没有 NPC 就没有 LLM 客户端可言
        return "llm" if (first.use_llm and first._get_llm() is not None) else "rules"

    @app.get("/api/mode", dependencies=[Depends(_verify_origin)])
    async def get_mode() -> Dict:
        """当前对话模式（规则 / LLM）— 全局设置，所有 NPC 一致。"""
        first = next(iter(npcs.values()), None)
        requested = "llm" if (first is not None and first.use_llm) else "rules"
        return {"mode": _effective_mode(), "requested": requested}

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
        first = next(iter(npcs.values()), None)
        if npc_id and npc_id in npcs:
            return {"policy": npcs[npc_id].approval_policy,
                    "actions": approval_table(npcs[npc_id].approval_policy,
                                              npcs[npc_id].approval_overrides),
                    "npc_id": npc_id}
        policy = first.approval_policy if first is not None else "auto"
        return {"policy": policy,
                "actions": approval_table(policy)}

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
        first = next(iter(npcs.values()), None)
        policy = first.approval_policy if first is not None else "auto"
        return {"ok": True, "actions": approval_table(policy)}

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
            set_manifest_resources(None)
            set_manifest_places(None)   # 任务书#05·C
            return {"ok": True, "is_default": True, "actions": get_manifest()}
        try:
            parsed = parse_manifest_doc(body.get("actions") if "actions" in body else body)
            load_manifest(parsed["actions"])
            # 词典段暂存: 世界就绪时由 bootstrap 并入(与 resources 同款)
            set_manifest_resources(parsed.get("resources"))
            set_manifest_places(parsed.get("places"))
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

    # ── 根路径: 一条命令启动后打开 127.0.0.1:8765 就能用（不用记子路径）──
    # Console 已构建 → 进 Console；没构建 → 返回提示（旧关系网页 npc.html 已下架，
    # 2026-09-08 连同 web/server.py / agent_static 一起移到桌面归档，不再兜底）。
    @app.get("/", include_in_schema=False)
    async def root():
        if (_BASE_DIR / "web" / "console" / "dist").is_dir():
            return RedirectResponse(url="/console/")
        return JSONResponse({"name": "NPCSidekick", "hint":
                             "Console 未构建：在 web/console 下运行 npm run build 后访问 /console/"})

    # ── Web Console（2026-09-07）────────────────────────
    # 开发者工具层: 人设 CRUD + 热加载 / 关系图 / 记忆单条编辑 / Provider 配置。
    # 游戏协议三件套一个没动；本层端点全是新增的 /api/* 路径。
    mount_console_api(app, _console_ctx())

    # ── 静态页面（最后挂载；路径锚定代码位置）────────────────
    # Console 构建产物（web/console/dist）挂 /console/; 未构建时静默跳过，
    # 不影响既有页面与游戏接入（dist 由 `npm run build` 产出，不进 Git）。
    # 旧 web/static（npc.html）已下架（2026-09-08 移到桌面归档），不再挂根静态目录。
    _console_dist = _BASE_DIR / "web" / "console" / "dist"
    if _console_dist.is_dir():
        app.mount("/console", StaticFiles(directory=str(_console_dist), html=True),
                  name="console")
    return app


def main() -> None:
    """入口(P2 拆分序③): 实现迁 npc/bootstrap.py —— 本壳保住
    pyproject 脚本 npc-server = npc.server:main 与 python -m npc.server 契约。"""
    from npc.bootstrap import main as _bootstrap_main
    _bootstrap_main()


if __name__ == "__main__":
    main()
