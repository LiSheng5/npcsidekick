"""Web Console 管理型 API（2026-09-07 · Step 4）。

定位: Runtime 的**开发者工具层** — Visualization + Character Editor +
Runtime Monitor + LLM Playground。不是第二套引擎，也不是新的游戏协议。

铁律:
  · 不碰游戏接入三件套 /api/talk /api/state /api/task（协议零改动）
  · 人设改完热加载: 写盘 → 替换/注册 NPC 实例 → 同步世界 actor 槽
    （删人设必须清槽，否则 tick_round 遍历到孤儿槽 KeyError 全村冻结）
  · 记忆编辑只经 NPC.remember / NPCMemory → npc.save()，绕过它另写一套 = 违规
  · Runtime 没有关系数据就不假装有: /api/relationships 返回空 edges + source="none"
  · API Key 只回 masked，明文永不出现在任何响应里

挂载: npc/server.create_npc_server() 末尾调用 mount_console_api(app, ctx)。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional

from fastapi import FastAPI, HTTPException, Request

# id 白名单（与 server 动态注册同一口径: store 文件名由 id 拼出，防路径穿越）
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass
class ConsoleContext:
    """Console 端点所需的 Runtime 句柄（server 装配时注入，便于测试）。"""

    npcs: Dict
    world: Dict
    personas_path: Path
    store_dir: str = "npc/store"
    world_id: str = ""
    config_dir: Path = field(default_factory=lambda: Path("npc/config"))
    guard_world: Optional[Callable[[dict], None]] = None


# ══════════════════════════════════════════════════════════
# 人设 CRUD + 热加载
# ══════════════════════════════════════════════════════════


def _check_id(pid: str) -> str:
    if not isinstance(pid, str) or not _ID_RE.match(pid or ""):
        raise HTTPException(status_code=400, detail="id 不合法: 1~32 位字母/数字/_/-")
    return pid


def _persona_file(ctx: ConsoleContext, pid: str) -> Path:
    return Path(ctx.personas_path) / f"{pid}.json"


def _read_persona(ctx: ConsoleContext, pid: str) -> Dict:
    p = _persona_file(ctx, pid)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"没有这个人设: {pid}")
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"{p.name} 不是合法 JSON: {exc}")


def hot_reload(ctx: ConsoleContext, pid: str, persona: Dict) -> str:
    """把一份人设接入运行中的世界。返回 "created" / "updated"。

    新建: 有记忆卡就续前缘（读卡 → 以 JSON 人设为准覆盖 → 挂共享世界），
          没有就新建实例；两种都要注册世界 actor 槽。
    更新: 换 persona + 重编 system_prompt + **清 LLM 分槽缓存**
          （模型可能换了，旧客户端会一直打到老模型上）。
    """
    from npc.npc import NPC
    from npc.persona import build_system_prompt
    from npc.world import actor_of

    npc = ctx.npcs.get(pid)
    if npc is None:
        card = Path(ctx.store_dir) / f"{pid}_memory.json"
        if card.exists():
            npc = NPC.load(pid, store_dir=ctx.store_dir)
            npc.persona = persona                    # 人设以制作者 JSON 为准
            npc.world = ctx.world                    # 共享世界（AI Town 模式）
        else:
            npc = NPC(persona=persona, world=ctx.world, store_dir=ctx.store_dir)
        npc.system_prompt = persona.get("system_prompt_override") or build_system_prompt(persona)
        actor_of(ctx.world, pid)
        ctx.npcs[pid] = npc
        return "created"

    npc.persona = persona
    npc.system_prompt = persona.get("system_prompt_override") or build_system_prompt(persona)
    npc._llm = None            # 分槽缓存失效（模型/base_url 可能已变）
    npc._llm_review = None
    return "updated"


def discard_file(path: Path, trash_dir: Path) -> str:
    """把文件移进 .trash（而不是物理删除）。

    为什么不是 unlink:
      1) 制作者工具里的"删除"应该可反悔 —— 手改三天的人设不该一键蒸发；
      2) 本机安全策略会拦截一切删除动作（safe-delete fail-closed），
         os.replace 是移动不是删除，不受影响。
    .trash 是子目录，人设扫描器只 glob 顶层 *.json，不会把回收站里的角色捡回来。
    返回回收站中的文件路径（便于 UI 提示"在哪儿能找回"）。
    """
    path = Path(path)
    if not path.exists():
        return ""
    trash_dir = Path(trash_dir)
    trash_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = trash_dir / f"{path.stem}.{stamp}{path.suffix}"
    idx = 1
    while target.exists():        # 同一秒连删两次也不覆盖
        idx += 1
        target = trash_dir / f"{path.stem}.{stamp}-{idx}{path.suffix}"
    try:
        os.replace(str(path), str(target))
        return str(target)
    except OSError:
        try:
            path.unlink()        # 移动失败才真删（例如跨盘符）
            return ""
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"无法移除 {path.name}: {exc}")


def detach_npc(ctx: ConsoleContext, pid: str) -> None:
    """从运行时摘掉一个 NPC 并清世界 actor 槽（防孤儿槽 → tick KeyError）。"""
    ctx.npcs.pop(pid, None)
    try:
        ctx.world["actors"].pop(pid, None)
    except (KeyError, TypeError):
        pass


# ══════════════════════════════════════════════════════════
# 关系图（Runtime 无关系数据 → 空态明示，不造假）
# ══════════════════════════════════════════════════════════


def build_relationships(ctx: ConsoleContext) -> Dict:
    """关系图数据源: {source, nodes, edges}。

    source:
      "runtime"  — 世界声明了关系（world["_relationships"]，未来 Runtime 出口）
      "persona"  — 制作者在 persona JSON 里手填的 relations（可选字段，非 schema）
      "none"     — 没有任何关系数据 → edges 为空，UI 明确提示（不编造）
    """
    nodes: Dict[str, Dict] = {}
    for pid, npc in ctx.npcs.items():
        p = npc.persona
        nodes[pid] = {
            "id": pid,
            "type": "npc",
            "name": p.get("name", pid),
            "state": npc.state,
            "metadata": {
                "identity": p.get("identity", ""),
                "activity": npc.activity_desc(),
                "position": npc.world["actors"].get(pid, {}).get("position", ""),
            },
        }

    edges = []
    source = "none"
    raw = ctx.world.get("_relationships")
    if isinstance(raw, dict) and isinstance(raw.get("edges"), list):
        source = "runtime"
        for e in raw["edges"]:
            if isinstance(e, dict) and e.get("source") and e.get("target"):
                edges.append({
                    "source": str(e["source"]),
                    "target": str(e["target"]),
                    "type": str(e.get("type", e.get("how", "related"))),
                    "weight": e.get("weight"),
                    "metadata": {k: v for k, v in e.items()
                                 if k not in ("source", "target", "type", "weight")},
                })
    else:
        for pid, npc in ctx.npcs.items():
            rels = npc.persona.get("relations")
            if not isinstance(rels, list):
                continue
            for r in rels:
                if not isinstance(r, dict):
                    continue
                who = str(r.get("who") or r.get("target") or "")
                if not who:
                    continue
                edges.append({
                    "source": pid,
                    "target": who,
                    "type": str(r.get("how") or r.get("type") or "related"),
                    "weight": r.get("weight") or r.get("score"),
                    "metadata": {k: v for k, v in r.items()
                                 if k not in ("who", "target", "how", "type", "score", "weight")},
                })
        if edges:
            source = "persona"

    # 边里出现但不在角色表里的实体（玩家/阵营/物件…）→ 补合成节点，类型不假设
    for e in edges:
        for end in (e["source"], e["target"]):
            if end not in nodes:
                nodes[end] = {"id": end, "type": "custom", "name": end,
                              "state": "", "metadata": {"synthetic": True}}

    return {
        "source": source,
        "nodes": list(nodes.values()),
        "edges": edges,
        "note": ("Runtime 未提供关系数据" if source == "none" else ""),
    }


# ══════════════════════════════════════════════════════════
# Provider 配置（SecretStore）
# ══════════════════════════════════════════════════════════


def _store(ctx: ConsoleContext):
    from npc.secrets import SecretStore
    return SecretStore(Path(ctx.config_dir) / "providers.enc")


def apply_provider_to_runtime(ctx: ConsoleContext, prov: Dict) -> None:
    """激活某 provider: 写进进程环境（Runtime 的 key/端点/模型解析口径）+ 清 LLM 缓存。

    Runtime 侧 llm_wiring.resolve_api_key 优先读 LLM_API_KEY，
    build_client 读 LLM_BASE_URL，model_of 读 NPC_DIALOGUE_MODEL —— 与 bat 启动
    脚本同一套口径，改动即时生效，不需要重启。
    """
    store = _store(ctx)
    key = store.get_secret(prov["id"])
    if key:
        os.environ["LLM_API_KEY"] = key
    if prov.get("base_url"):
        os.environ["LLM_BASE_URL"] = prov["base_url"]
    else:
        os.environ.pop("LLM_BASE_URL", None)
    if prov.get("model"):
        os.environ["NPC_DIALOGUE_MODEL"] = prov["model"]
    for npc in ctx.npcs.values():
        npc._llm = None
        npc._llm_review = None


def env_llm_view() -> Dict:
    """当前进程实际生效的 LLM 配置（只读视图，key 永远 masked）。"""
    from npc.secrets import mask_key
    from npc import llm_wiring
    raw = os.environ.get("LLM_API_KEY", "")
    return {
        "model": llm_wiring.model_of("dialogue"),
        "review_model": llm_wiring.model_of("review"),
        "base_url": os.environ.get("LLM_BASE_URL", ""),
        "configured": bool(raw),
        "masked_key": mask_key(raw),
    }


# ══════════════════════════════════════════════════════════
# 端点挂载
# ══════════════════════════════════════════════════════════


def mount_console_api(app: FastAPI, ctx: ConsoleContext) -> None:
    """把 Console 管理端点挂到 Runtime app 上（在静态挂载之前调用）。"""

    def _guard(body) -> None:
        if ctx.guard_world and isinstance(body, dict):
            ctx.guard_world(body)

    def _npc(pid: str):
        if pid not in ctx.npcs:
            raise HTTPException(status_code=404, detail=f"没有 NPC: {pid}")
        return ctx.npcs[pid]

    # ── 人设 CRUD ────────────────────────────────────

    @app.get("/api/personas/{pid}")
    async def get_persona(pid: str) -> Dict:
        _check_id(pid)
        return {"persona": _read_persona(ctx, pid)}

    @app.put("/api/personas/{pid}")
    async def update_persona(pid: str, request: Request) -> Dict:
        """改人设: body = 完整 persona JSON → 校验 → 写盘 → **热加载进运行时**。

        id 以路径为准（body 里的 id 不一致时纠正，避免"改名改出两个文件"）。
        """
        _check_id(pid)
        body = await request.json()
        _guard(body)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
        from npc.persona_loader import validate_persona_dict
        body = dict(body)
        body["id"] = pid
        try:
            cleaned = validate_persona_dict(body, source=pid)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        target = _persona_file(ctx, pid)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        action = hot_reload(ctx, pid, cleaned)
        return {"ok": True, "npc_id": pid, "action": action,
                "path": f"npc/personas/{pid}.json", "hot_reloaded": True,
                "persona": cleaned}

    @app.delete("/api/personas/{pid}")
    async def delete_persona(pid: str, drop_memory: bool = False) -> Dict:
        """删人设: 人设文件进 .trash + 摘实例 + 清世界槽。记忆卡默认**保留**。

        drop_memory=true 才把记忆卡也送进 .trash（UI 需二次确认）。
        返回 trashed 路径，UI 可以提示"在 npc/personas/.trash/ 还能找回来"。
        """
        _check_id(pid)
        target = _persona_file(ctx, pid)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"没有这个人设: {pid}")
        trashed = discard_file(target, target.parent / ".trash")
        detach_npc(ctx, pid)          # 清世界槽: 否则 tick 扫到孤儿槽 KeyError
        dropped = False
        if drop_memory:
            card = Path(ctx.store_dir) / f"{pid}_memory.json"
            if card.exists():
                discard_file(card, card.parent / ".trash")
                dropped = True
        return {"ok": True, "npc_id": pid, "memory_dropped": dropped, "trashed": trashed}

    # ── 关系图 ────────────────────────────────────────

    @app.get("/api/relationships")
    async def relationships() -> Dict:
        return build_relationships(ctx)

    # ── 记忆单条 CRUD（走 NPCMemory → 记忆卡落盘）─────

    @app.get("/api/npcs/{pid}/memory")
    async def list_memory(pid: str, query: str = "", top_k: int = 5) -> Dict:
        npc = _npc(pid)
        entries = npc.memory.retrieve(query, top_k=top_k) if query else npc.memory.all()
        return {"npc_id": pid, "entries": list(entries), "total": len(npc.memory.all())}

    @app.post("/api/npcs/{pid}/memory")
    async def add_memory(pid: str, request: Request) -> Dict:
        npc = _npc(pid)
        body = await request.json()
        _guard(body)
        content = str(body.get("content", "")).strip()
        if not content:
            raise HTTPException(status_code=400, detail="content 不能为空")
        try:
            importance = int(body.get("importance", 5))
        except (TypeError, ValueError):
            importance = 5
        category = str(body.get("category") or "general")
        mtype = str(body.get("mtype") or "")
        # 走 NPC.remember: 安检拒收 + 去重聚合 + mtype 分类，与 Runtime 同一入口
        npc.remember(content, importance=importance, category=category, mtype=mtype)
        npc.save()
        entry = npc.memory.all()[-1] if npc.memory.all() else None
        return {"ok": True, "entry": entry, "total": len(npc.memory.all())}

    @app.put("/api/npcs/{pid}/memory/{mid}")
    async def update_memory(pid: str, mid: str, request: Request) -> Dict:
        npc = _npc(pid)
        body = await request.json()
        _guard(body)
        entries = npc.memory.all()
        hit = next((e for e in entries if str(e.get("id")) == mid), None)
        if hit is None:
            raise HTTPException(status_code=404, detail=f"没有这条记忆: {mid}")
        if "content" in body:
            content = str(body.get("content", "")).strip()
            if not content:
                raise HTTPException(status_code=400, detail="content 不能为空")
            hit["content"] = content[:2000]
        if "importance" in body:
            try:
                hit["importance"] = max(0, min(9, int(body["importance"])))
            except (TypeError, ValueError):
                pass
        if "category" in body:
            hit["category"] = str(body["category"])
        if "mtype" in body:
            hit["mtype"] = str(body["mtype"])
        npc.memory.load(entries)      # 同一份列表写回（不新建副本，保持引用一致）
        npc.save()
        return {"ok": True, "entry": hit}

    @app.delete("/api/npcs/{pid}/memory/{mid}")
    async def delete_memory(pid: str, mid: str) -> Dict:
        npc = _npc(pid)
        entries = npc.memory.all()
        rest = [e for e in entries if str(e.get("id")) != mid]
        if len(rest) == len(entries):
            raise HTTPException(status_code=404, detail=f"没有这条记忆: {mid}")
        npc.memory.load(rest)
        npc.save()
        return {"ok": True, "deleted": mid, "total": len(rest)}

    # ── Provider / API Key ────────────────────────────

    @app.get("/api/settings/providers")
    async def get_providers() -> Dict:
        store = _store(ctx)
        return {"providers": store.list_providers(),
                "active": store.active_id(),
                "encrypted": store.encrypted,
                "env": env_llm_view()}

    @app.post("/api/settings/providers")
    async def upsert_provider(request: Request) -> Dict:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
        pid = _check_id(str(body.get("id") or body.get("name") or "").strip())
        store = _store(ctx)
        prov = store.upsert_provider(
            pid,
            name=str(body.get("name") or pid),
            base_url=str(body.get("base_url") or ""),
            model=str(body.get("model") or ""),
            api_key=str(body.get("api_key") or ""),
        )
        if body.get("activate"):
            store.set_active(pid)
            apply_provider_to_runtime(ctx, prov)
            prov["active"] = True
        return {"ok": True, "provider": prov, "providers": store.list_providers()}

    @app.delete("/api/settings/providers/{pid}")
    async def delete_provider(pid: str) -> Dict:
        store = _store(ctx)
        if not store.delete_provider(_check_id(pid)):
            raise HTTPException(status_code=404, detail=f"没有这个 provider: {pid}")
        return {"ok": True, "providers": store.list_providers()}

    @app.post("/api/settings/providers/{pid}/activate")
    async def activate_provider(pid: str) -> Dict:
        store = _store(ctx)
        _check_id(pid)
        if not store.set_active(pid):
            raise HTTPException(status_code=404, detail=f"没有这个 provider: {pid}")
        prov = store.get_provider(pid) or {}
        apply_provider_to_runtime(ctx, prov)
        return {"ok": True, "active": pid, "providers": store.list_providers(),
                "env": env_llm_view()}

    @app.post("/api/llm/test")
    async def llm_test(request: Request) -> Dict:
        """拨测: 拿 provider_id（或临时 base_url/key/model）发一个最小请求。

        同步 SDK 调用挪进 to_thread —— 绝不冻事件循环（§19 教训）。
        失败也返回 200 + ok=false（前端要展示错误，不是要 HTTP 异常）。
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
        store = _store(ctx)
        pid = str(body.get("provider_id") or store.active_id() or "")
        base_url = str(body.get("base_url") or "")
        model = str(body.get("model") or "")
        api_key = str(body.get("api_key") or "")
        if pid and not api_key:
            prov = store.get_provider(pid) or {}
            api_key = store.get_secret(pid)
            base_url = base_url or prov.get("base_url", "")
            model = model or prov.get("model", "")
        if not api_key:
            return {"ok": False, "error": "未配置 API Key"}
        if not model:
            return {"ok": False, "error": "未指定模型"}
        if not base_url:
            from agent.providers.factory import detect_base_url
            base_url = detect_base_url(model)

        def _call() -> Dict:
            t0 = time.time()
            try:
                from agent.llm.client import LLMClient
                from agent.providers.factory import create_provider
                provider = create_provider(api_key=api_key, model_name=model,
                                           base_url=base_url)
                client = LLMClient(provider=provider)
                resp = client.chat([{"role": "user", "content": "ping"}], max_tokens=16)
                return {"ok": True, "model": getattr(resp, "model", model),
                        "latency_ms": int((time.time() - t0) * 1000),
                        "reply": (getattr(resp, "content", "") or "")[:200]}
            except Exception as exc:
                return {"ok": False, "model": model,
                        "latency_ms": int((time.time() - t0) * 1000),
                        "error": f"{type(exc).__name__}: {exc}"[:300]}

        return await asyncio.to_thread(_call)
