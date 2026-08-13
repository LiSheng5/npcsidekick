"""
NPCSidekick — Web 服务（制作者体验优先，参考 AI Town 的 one-command 启动）。

一条命令:  python -m npc.server
  - 自动打开浏览器 http://127.0.0.1:8765/npc.html
  - API: /api/npc /api/talk /api/state /api/memory /api/task

制作者契约（体验设计）:
  - 不写任何后端代码 — 只改 JSON 配置（人设/世界），后端全部隐形
  - 无 API key 也能跑 — LLM 缺失时自动退化规则模式（苍照样聊天干活）
  - 单一命令启动，自动开浏览器

安全: 仅绑定 127.0.0.1 + Origin 校验（本地单机工具；去掉 token —
修复 I13"前端不发 token 导致全部 401"的问题，代价是仅限本机使用）。
"""
from __future__ import annotations

import asyncio
import os
import random
import webbrowser
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Dict, Optional

# 代码位置锚定的项目根（cwd 无关 — 游戏自动拉起时从任意目录启动都能找到角色/记忆卡/页面）
_BASE_DIR = Path(__file__).resolve().parents[1]

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agent.logging_config import log
from npc.npc import NPC
from npc.scheduler import tick_round

_ALLOWED_ORIGIN_PREFIXES = ("http://127.0.0.1", "http://localhost")

# 自主循环帧间隔（秒）— 环境变量 NPC_TICK_INTERVAL 可覆盖
TICK_INTERVAL = 3.0


def _tick_interval() -> float:
    try:
        return float(os.environ.get("NPC_TICK_INTERVAL", TICK_INTERVAL))
    except ValueError:
        return TICK_INTERVAL


def _save_on_transitions(npcs: Dict[str, NPC], events: Dict) -> None:
    """save 只在转换点（started/completed/failed）— 活动进行中的 tick 零落盘。"""
    for actor_id, ev in events.items():
        if ev:
            npcs[actor_id].save()


async def _tick_loop(world, npcs: Dict[str, NPC]) -> None:
    """后台自主循环（村民日常）: 每帧 tick_round + 转换点落盘。单帧异常不杀循环。"""
    while True:
        await asyncio.sleep(_tick_interval())
        try:
            events = tick_round(world, npcs, rng=random.Random())
            _save_on_transitions(npcs, events)
        except Exception as exc:
            log.warning("npc_tick_error", error=str(exc))


def _verify_origin(request: Request) -> None:
    """Origin 校验: 只允许本机（DNS-rebind 防护）。"""
    origin = request.headers.get("origin", "")
    if origin and not any(origin.startswith(p) for p in _ALLOWED_ORIGIN_PREFIXES):
        raise HTTPException(status_code=403, detail="不允许的 Origin")


def load_village(store_dir: str = "npc/store", personas: Optional[Dict] = None):
    """加载示例村庄: 共享世界 + 全部示例 NPC。

    personas: {id: persona} 角色表 — 默认角色表（苍/阿黎）；
    传入适配器角色表（如 paleolithic.VILLAGERS）即得到该游戏的村民。
    从记忆卡恢复（若有），否则新建。多 NPC 共用一个世界（AI Town 模式）。
    """
    from pathlib import Path

    from npc.persona import SAMPLE_NPCS, build_system_prompt
    from npc.world import actor_of, default_world

    cast = personas or SAMPLE_NPCS
    loaded: Dict[str, NPC] = {}
    world = None
    for pid, persona in cast.items():
        path = Path(store_dir) / f"{pid}_memory.json"
        if path.exists():
            npc = NPC.load(pid, store_dir=store_dir)
            # 人设以制作者 JSON 为准 — 记忆卡只存记忆/世界/任务日志。
            # （否则制作者改了 cang.json 却因旧记忆卡不生效，是制作者体验陷阱）
            npc.persona = persona
            npc.system_prompt = persona.get("system_prompt_override") or build_system_prompt(persona)
            if world is None:
                world = npc.world
            else:
                npc.world = world
                actor_of(world, pid)
        else:
            if world is None:
                world = default_world()
            npc = NPC(persona=persona, world=world, store_dir=store_dir)
        loaded[pid] = npc
    # 清理孤儿槽: 记忆卡世界快照可能含当前角色表(cast)没有的 NPC(如删了 ali.json 的残留),
    # tick_round 会遍历 world["actors"] 全部槽做 npcs[actor_id] 索引 → 孤儿槽 KeyError → 全村冻结。
    for orphan in [a for a in world["actors"] if a not in loaded]:
        del world["actors"][orphan]
    return world, loaded


def create_npc_server(npcs: Optional[Dict[str, NPC]] = None) -> FastAPI:
    """构建 NPCSidekick Web 服务（工厂函数，便于测试注入）。

    npcs: {id: NPC} 多 NPC 共享世界。默认加载示例村庄。
    后台自主循环随 FastAPI lifespan 启动 — TestClient 不进 with 上下文则不启动（测试确定性）。
    """
    if npcs is None:
        world, npcs = load_village()
    else:
        world = next(iter(npcs.values())).world   # AI Town 模式: 共享世界

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        task = asyncio.create_task(_tick_loop(world, npcs))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="NPCSidekick", lifespan=_lifespan)

    def get_npc(npc_id: str) -> NPC:
        if npc_id not in npcs:
            raise HTTPException(status_code=404, detail=f"没有 NPC: {npc_id}")
        return npcs[npc_id]

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
        """世界状态: 全部 NPC 的位置/背包/状态 + 已交付 + tick 计数 + 事件日志尾部。"""
        first = next(iter(npcs.values()))
        return {
            "actors": {
                nid: {
                    "position": n.world["actors"][nid]["position"],
                    "inventory": n.world["actors"][nid]["inventory"],
                    "state": n.state,                    # idle/walking/working/resting
                    "activity": n.activity_desc(),       # 展示串，无活动 = ""
                }
                for nid, n in npcs.items()
            },
            "delivered": first.world["delivered"],
            "tick": first.world.get("_tick", 0),
            "log_tail": first.world["log"][-10:],        # 气泡差分用（碰面说话）
        }

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

    @app.post("/api/talk", dependencies=[Depends(_verify_origin)])
    async def talk(request: Request) -> Dict:
        body = await request.json()
        message = body.get("message", "")
        if not message:
            raise HTTPException(status_code=400, detail="message 不能为空")
        reply = get_npc(body.get("npc_id", "cang")).talk(message)
        return {"reply": reply}

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

    @app.post("/api/task", dependencies=[Depends(_verify_origin)])
    async def task(request: Request) -> Dict:
        """派发采集任务（规则快路径，零 LLM）— 指定 NPC。"""
        body = await request.json()
        resource = body.get("resource", "")
        count = int(body.get("count", 1))
        npc = get_npc(body.get("npc_id", "cang"))
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
    args = parser.parse_args()

    personas = None
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

    port = 8765
    world, npcs = load_village(store_dir=str(_BASE_DIR / "npc" / "store"), personas=personas)
    app = create_npc_server(npcs)
    log.info("npc_server_started", url=f"http://127.0.0.1:{port}/npc.html")
    print(f"NPCSidekick: http://127.0.0.1:{port}/npc.html")
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/npc.html")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
