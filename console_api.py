"""控制台端点（**调试面**）—— 与给 mod 用的游戏面端点分开（本文件的端点不属于 mod 契约）。

游戏面 = mod 用的四件事（报到 / 对话 / 动作执行 / 动作结果回报）+ 状态查询；
本文件只服务于本地控制台 UI：
角色卡读写、关系网（含头像）、记忆卡增删改查、聊天记录查看。

约定：
  · 找不到资源 → 404；请求体非法 → 400（与游戏面的"字段非法 → 400"区分开）
  · 角色卡的读取/枚举统一走 core.personas（与游戏面同一实现），这里只管 HTTP 口径
  · 记忆的写操作走 memory 模块（= 把"直接手改记忆卡"这条通道做成界面），
    不新增任何绕过记忆卡的写路径
  · 头像走 data URI（`{"image": "data:image/png;base64,..."}`）而不是 multipart
    —— 免得多引一个 python-multipart 依赖

结构：`build_router` 只做装配；端点按资源分组在 `_routes_*` 工厂里
（capabilities / llm / personas / relationships / avatars / memory / chat），
共享的读取与校验小工具挂在 `_ConsoleCtx`。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from core.logging_config import log
from core.paths import resolve_store, valid_npc_id
from core.personas import PersonaError, persona_files, persona_path, read_persona
from http_util import json_body

import chatlog
import memory

# 聊天记录一次最多返回多少条
_CHAT_PAGE_MAX = 1000
_EDITABLE_FIELDS = ("content", "importance", "category", "pinned")

# ── 头像 ────────────────────────────────────────────────
_AVATAR_EXT = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp", "gif": ".gif"}
# 头像上限经 /api/state 下发给前端做上传前预检 —— 值只此一份，别在前端再抄一个 2MB
AVATAR_MAX_BYTES = 2 * 1024 * 1024
_AVATAR_DATA_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=\s]+)$")

# 删卡时这句提示会随 DELETE 响应的 kept.note 下发，也经 /api/state 播给前端
# （confirm 与按钮旁说明用）—— 文案只此一份，前端不再抄
DELETE_KEPT_NOTE = ("记忆卡与聊天记录保留（数据在 store/，重建同名卡接着用）"
                    "—— 删卡不该连带删记忆")


@dataclass
class _ConsoleCtx:
    """控制台路由的装配上下文 —— build_router 的入参袋子 + 各资源组共用的读取/校验小工具。"""

    persona_dir: Path
    lock_for: Callable[[str], Any]
    registry: Any = None
    avatar_dir: Optional[Path] = None
    store_dir: Optional[Path] = None
    llm_api: Optional[Dict[str, Callable[..., Any]]] = None

    # ── 角色卡（读取实现在 core/personas.py，这里只管 HTTP 口径）──

    def read_persona(self, npc_id: str) -> Dict[str, Any]:
        """读一张角色卡：没有这张卡 → 404；卡坏了 → 400。"""
        path = persona_path(self.persona_dir, npc_id)
        try:
            return read_persona(path)
        except PersonaError as exc:
            raise HTTPException(status_code=404 if exc.missing else 400,
                                detail=str(exc)) from exc

    def require_id(self, npc_id: str) -> str:
        """npc_id 会被拼进文件名：非法（含 `../` 穿越企图）→ 400。"""
        if not valid_npc_id(npc_id):
            raise HTTPException(status_code=400, detail=f"非法的 NPC id: {npc_id}")
        return npc_id

    # ── 头像文件（avatars/{id}.<ext>，由 server 静态挂在 /avatars）──

    def avatar_glob(self, npc_id: str) -> List[Path]:
        if self.avatar_dir is None or not valid_npc_id(npc_id) or not self.avatar_dir.is_dir():
            return []
        return [p for p in sorted(self.avatar_dir.glob(f"{npc_id}.*"))
                if p.is_file() and p.suffix.lower() in _AVATAR_EXT.values()]

    def avatar_url(self, npc_id: str) -> Optional[str]:
        """带 mtime 查询串 —— 换头像后浏览器不会拿旧缓存。"""
        files = self.avatar_glob(npc_id)
        if not files:
            return None
        try:
            stamp = int(files[0].stat().st_mtime)
        except OSError:
            stamp = 0
        return f"/avatars/{files[0].name}?v={stamp}"

    # ── 记忆条目展示 ────────────────────────────────────

    def decorate(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """给每条附上存在时长，方便控制台展示。"""
        now = time.time()
        out = []
        for e in entries:
            item = dict(e)
            item["age_hours"] = round(max(0.0, (now - e["created_at"]) / 3600.0), 1)
            out.append(item)
        return out


# ── 路由分组 ────────────────────────────────────────────
# 端点分工（别搞反了，改错会把文件 IO 搬回事件循环里）：
#   · 纯读 / 无 body 的文件端点写成**同步 def** —— FastAPI 自动把它丢进线程池，
#     天然不卡事件循环；给它加回 async 就等于把 IO 搬回循环里。
#   · 需要 await body、或要拿 lock_for（记忆卡写操作）的端点保持 async，
#     内部同步 IO 一律 `await asyncio.to_thread(...)`。
# 理由：这些端点慢的时候（记忆卡/聊天记录变大后）不能让正在进行的对话顿卡。


def _routes_capabilities(router: APIRouter, ctx: _ConsoleCtx) -> None:
    """能力清单只读视图（POST /api/capabilities 那个是 mod 报到用的，这里只是给控制台看）。"""
    if ctx.registry is None:
        return

    @router.get("/capabilities")
    async def get_capabilities():
        now = time.time()
        mods = {}
        for mod, entry in ctx.registry.dump().items():
            mods[mod] = {
                "online": ctx.registry.online(mod),
                "last_seen_s_ago": round(now - entry["last_seen"], 1),
                "actions": entry["actions"],
                "execute_url": entry.get("execute_url"),
            }
        return {"mods": mods, "heartbeat_timeout_s": ctx.registry.timeout()}


def _routes_llm(router: APIRouter, ctx: _ConsoleCtx) -> None:
    """模型 / 端点 / 思考档位（页面可改，落 store/llm_config.json）。"""
    if not ctx.llm_api:
        return

    @router.get("/llm")
    def get_llm():
        return ctx.llm_api["status"]()

    @router.put("/llm")
    async def put_llm(request: Request):
        """改设置并立即换脑；非法档位 → 400。key 不在这里改。"""
        body = await json_body(request)
        try:
            return await asyncio.to_thread(ctx.llm_api["apply"], body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/llm")
    def delete_llm():
        """清掉页面设置与落盘文件，回到环境变量那一层。"""
        return ctx.llm_api["reset"]()


def _routes_personas(router: APIRouter, ctx: _ConsoleCtx) -> None:
    """角色卡（personas/*.json）的增删改查。"""

    @router.get("/personas")
    def list_personas():
        items: List[Dict[str, Any]] = []
        for path in persona_files(ctx.persona_dir):
            item: Dict[str, Any] = {"id": path.stem, "path": str(path)}
            try:
                data = read_persona(path)
            except PersonaError as exc:          # 坏卡不连累整份列表，挂在它自己头上
                item["error"] = str(exc)
            else:
                item["name"] = data.get("name") or path.stem
                item["identity"] = data.get("identity", "")
            items.append(item)
        return {"personas": items}

    @router.get("/personas/{npc_id}")
    def get_persona(npc_id: str):
        ctx.require_id(npc_id)
        return {"npc_id": npc_id, "persona": ctx.read_persona(npc_id)}

    @router.put("/personas/{npc_id}")
    async def put_persona(npc_id: str, request: Request):
        """整份覆盖写（不存在即新建）。id 字段缺失会自动补成路径里的 id。"""
        ctx.require_id(npc_id)
        persona = await json_body(request)
        given = persona.get("id")
        if given is not None and str(given) != npc_id:
            raise HTTPException(
                status_code=400,
                detail=f"角色卡 id（{given}）与路径 id（{npc_id}）不一致")
        persona["id"] = npc_id

        path = persona_path(ctx.persona_dir, npc_id)

        def _write_card() -> None:
            """原子写（先写临时文件再替换）—— 同步实现，交给线程池。"""
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(persona, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

        await asyncio.to_thread(_write_card)
        log.info("persona_saved", npc_id=npc_id)
        return {"ok": True, "npc_id": npc_id, "persona": persona}

    @router.delete("/personas/{npc_id}")
    def delete_persona(npc_id: str):
        """删掉一张角色卡（只删 `personas/{id}.json`）。

        `_` 开头的文件是模板/说明，不算 NPC，也不许删。
        **记忆卡与聊天记录不动**（数据在 store/ 里，重建同名角色卡就能接着用 —— 删卡不该连带删记忆）；
        响应与日志都会写明保留了什么（`DELETE_KEPT_NOTE`，前端直接展示响应里的 kept.note）。
        """
        if npc_id.startswith("_"):
            raise HTTPException(status_code=400, detail=f"非法的 NPC id: {npc_id}")
        ctx.require_id(npc_id)
        path = persona_path(ctx.persona_dir, npc_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"没有这张角色卡: {npc_id}")

        try:
            kept_memory: Optional[int] = len(memory.load_card(npc_id, store_dir=ctx.store_dir))
        except Exception:          # 坏卡只影响"保留了多少"这个数字，别把删除拖挂
            kept_memory = None
        try:
            kept_chat: Optional[int] = len(chatlog.load_turns(npc_id, store_dir=ctx.store_dir))
        except Exception:
            kept_chat = None
        kept = {
            "note": DELETE_KEPT_NOTE,
            "memory_entries": kept_memory,
            "chat_messages": kept_chat,
            "store_dir": str(resolve_store(ctx.store_dir)),
        }
        try:
            path.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除失败: {exc}") from exc
        log.info("persona_deleted", npc_id=npc_id, kept_memory=kept["memory_entries"],
                 kept_chat=kept["chat_messages"], note=DELETE_KEPT_NOTE)
        return {"ok": True, "npc_id": npc_id, "kept": kept}


def _routes_relationships(router: APIRouter, ctx: _ConsoleCtx) -> None:
    """关系网（persona 的 relations 字段驱动；无数据 → 诚实空态）。"""

    @router.get("/relationships")
    def get_relationships():
        """关系图数据源 {source, nodes, edges}。

        v4 没有 world/Runtime，关系只有一个来源：`personas/{id}.json` 的可选
        `relations` 字段（制作者手填）。没填 → `source="none"` + 空 edges，
        UI 明确提示，**绝不用假数据顶替**。
        """
        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []
        for path in persona_files(ctx.persona_dir):
            pid = path.stem
            node: Dict[str, Any] = {"id": pid, "type": "npc", "name": pid,
                                    "identity": "", "error": None,
                                    "avatar": ctx.avatar_url(pid)}
            try:
                data = read_persona(path)
            except PersonaError as exc:          # 坏卡只标它自己，别拖垮整张关系网
                node["error"] = str(exc)
                nodes[pid] = node
                continue
            node["name"] = data.get("name") or pid
            node["identity"] = data.get("identity", "")
            relations = data.get("relations")
            if isinstance(relations, list):
                for rel in relations:
                    if not isinstance(rel, dict):
                        continue
                    who = rel.get("who") or rel.get("target")
                    if not isinstance(who, str) or not who.strip():
                        continue
                    who = who.strip()
                    kind = rel.get("how") or rel.get("type") or "related"
                    weight = rel.get("weight", rel.get("score"))
                    edges.append({
                        "source": pid,
                        "target": who,
                        "type": str(kind),
                        "weight": weight,
                        "metadata": {k: v for k, v in rel.items()
                                     if k not in ("who", "target", "how", "type",
                                                  "score", "weight")},
                    })
            nodes[pid] = node

        # 边里出现但不在角色卡里的对象（玩家 / 阵营 / 物件…）→ 补合成节点，不假设类型
        for edge in edges:
            for end in (edge["source"], edge["target"]):
                if end not in nodes:
                    nodes[end] = {"id": end, "type": "custom", "name": end,
                                  "identity": "", "error": None, "synthetic": True,
                                  "avatar": ctx.avatar_url(end)}

        source = "persona" if edges else "none"
        return {
            "source": source,
            "nodes": list(nodes.values()),
            "edges": edges,
            "note": ("还没有任何关系数据 —— 在角色卡里加可选字段 "
                     "relations: [{\"who\": \"<对方 id>\", \"how\": \"关系\", \"weight\": 1}]"
                     if source == "none" else ""),
        }


def _routes_avatars(router: APIRouter, ctx: _ConsoleCtx) -> None:
    """头像（关系网节点用；角色卡与合成节点都可挂）。"""

    @router.post("/npcs/{npc_id}/avatar")
    async def put_avatar(npc_id: str, request: Request):
        """存头像：前端读文件成 data URI 传来，这里落成 `avatars/{id}.<ext>`。

        换头像直接覆盖旧的（扩展名可能不同，先清干净）。
        """
        ctx.require_id(npc_id)
        if ctx.avatar_dir is None:
            raise HTTPException(status_code=500, detail="服务器未配置头像目录")
        body = await json_body(request)
        image = body.get("image")
        if not isinstance(image, str):
            raise HTTPException(status_code=400,
                                detail="字段 image 必填（data:image/...;base64,... 字符串）")
        matched = _AVATAR_DATA_RE.match(image.strip())
        if matched is None:
            raise HTTPException(
                status_code=400,
                detail="image 必须是 data:image/(png|jpeg|webp|gif);base64,... 形式")
        try:
            raw = base64.b64decode(matched.group(2), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="image 的 base64 数据无法解码") from exc
        if not raw:
            raise HTTPException(status_code=400, detail="图片内容为空")
        if len(raw) > AVATAR_MAX_BYTES:
            raise HTTPException(status_code=400,
                                detail=f"图片太大（{len(raw)} 字节，上限 {AVATAR_MAX_BYTES}）")

        def _save_avatar() -> Optional[str]:
            """落盘 + 返回带 mtime 的 URL（换头像要清旧文件）—— 同步实现，交给线程池。"""
            ctx.avatar_dir.mkdir(parents=True, exist_ok=True)
            for old in ctx.avatar_glob(npc_id):
                old.unlink()
            target = ctx.avatar_dir / f"{npc_id}{_AVATAR_EXT[matched.group(1)]}"
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(target)
            return ctx.avatar_url(npc_id)

        avatar = await asyncio.to_thread(_save_avatar)
        log.info("avatar_saved", npc_id=npc_id, bytes=len(raw))
        return {"ok": True, "npc_id": npc_id, "bytes": len(raw), "avatar": avatar}

    @router.delete("/npcs/{npc_id}/avatar")
    def delete_avatar(npc_id: str):
        ctx.require_id(npc_id)
        files = ctx.avatar_glob(npc_id)
        if not files:
            raise HTTPException(status_code=404, detail=f"该节点还没有头像: {npc_id}")
        for path in files:
            path.unlink()
        log.info("avatar_removed", npc_id=npc_id)
        return {"ok": True, "npc_id": npc_id, "removed": len(files), "avatar": None}


def _routes_memory(router: APIRouter, ctx: _ConsoleCtx) -> None:
    """记忆卡（store/{id}_memory.json）的增删改查 —— 写操作与 talk 共用 per-NPC 锁。"""

    @router.get("/npcs/{npc_id}/memory")
    def list_memory(npc_id: str):
        ctx.require_id(npc_id)
        entries = memory.load_card(npc_id, store_dir=ctx.store_dir)
        return {
            "npc_id": npc_id,
            "entries": ctx.decorate(entries),
            "count": len(entries),
        }

    @router.post("/npcs/{npc_id}/memory")
    async def add_memory(npc_id: str, request: Request):
        ctx.require_id(npc_id)
        body = await json_body(request)
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(status_code=400, detail="字段 content 必填且必须是非空字符串")
        importance = body.get("importance", memory.DEFAULT_IMPORTANCE)
        category = body.get("category") or memory.DEFAULT_CATEGORY
        pinned = bool(body.get("pinned"))
        async with ctx.lock_for(npc_id):
            try:
                entry = await asyncio.to_thread(
                    memory.add_entry, npc_id, content,
                    importance=importance, category=category, pinned=pinned,
                    store_dir=ctx.store_dir)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "entry": ctx.decorate([entry])[0]}

    @router.put("/npcs/{npc_id}/memory/{entry_id}")
    async def update_memory(npc_id: str, entry_id: str, request: Request):
        ctx.require_id(npc_id)
        body = await json_body(request)
        fields = {k: v for k, v in body.items() if k in _EDITABLE_FIELDS}
        if not fields:
            raise HTTPException(status_code=400,
                                detail=f"至少要给一个可改字段: {'/'.join(_EDITABLE_FIELDS)}")
        async with ctx.lock_for(npc_id):
            try:
                entry = await asyncio.to_thread(
                    lambda: memory.update_entry(npc_id, entry_id,
                                                store_dir=ctx.store_dir, **fields))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if entry is None:
            raise HTTPException(status_code=404, detail=f"未找到记忆条目: {entry_id}")
        return {"ok": True, "entry": ctx.decorate([entry])[0]}

    @router.delete("/npcs/{npc_id}/memory/{entry_id}")
    async def delete_memory(npc_id: str, entry_id: str):
        ctx.require_id(npc_id)
        async with ctx.lock_for(npc_id):
            ok = await asyncio.to_thread(memory.delete_entry, npc_id, entry_id,
                                         store_dir=ctx.store_dir)
        if not ok:
            raise HTTPException(status_code=404, detail=f"未找到记忆条目: {entry_id}")
        return {"ok": True, "deleted": entry_id}


def _routes_chat(router: APIRouter, ctx: _ConsoleCtx) -> None:
    """聊天记录（store/{id}_chat.jsonl）只读查看。"""

    @router.get("/npcs/{npc_id}/chat")
    def get_chat(npc_id: str, limit: int = 200, offset: int = 0):
        """从最新往前取窗口：offset = 已看过的最新条数，limit = 本页条数。"""
        ctx.require_id(npc_id)
        limit = max(1, min(int(limit), _CHAT_PAGE_MAX))
        offset = max(0, int(offset))
        messages = chatlog.load_turns(npc_id, store_dir=ctx.store_dir)
        total = len(messages)
        end = max(0, total - offset)
        start = max(0, end - limit)
        return {
            "npc_id": npc_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "turns": messages[start:end],
            "estimated_tokens": sum(chatlog.estimate_tokens(m["content"]) for m in messages),
            "summary_threshold": chatlog.summary_limit(store_dir=ctx.store_dir),
            "summary": chatlog.load_summary(npc_id, store_dir=ctx.store_dir),
        }


def build_router(persona_dir: Path,
                 lock_for: Callable[[str], Any],
                 registry: Any = None,
                 avatar_dir: Optional[Path] = None,
                 store_dir: Optional[Path] = None,
                 llm_api: Optional[Dict[str, Callable[..., Any]]] = None) -> APIRouter:
    """组装控制台路由。lock_for = 每 NPC 的 asyncio.Lock（与 talk 共用，防并发写覆盖）。

    registry = server 的 Registry（能力清单 + 心跳）；给了才挂 GET /api/capabilities。
    avatar_dir = 头像存放目录（server 里静态挂在 /avatars）；None 表示不启用头像。
    store_dir = 记忆卡/聊天记录的运行时数据目录（None = 工程默认 store/）。
    llm_api = 模型设置的读写回调 {status, apply, reset}；给了才挂 /api/llm 三个端点。
    可改字段：model / base_url / api_key / reasoning_effort / thinking_unsupported / context_window；
    其中 api_key **明文落 store/llm_config.json**（页面已带醒目提示），接口永远只回掩码。
    token / authorization / secret / password 这类字段一律 400。

    端点本体按资源分组在 `_routes_*` 工厂（见上），这里只做装配。
    """
    ctx = _ConsoleCtx(persona_dir=persona_dir, lock_for=lock_for, registry=registry,
                      avatar_dir=avatar_dir, store_dir=store_dir, llm_api=llm_api)
    router = APIRouter(prefix="/api", tags=["console"])
    for mount in (_routes_capabilities, _routes_llm, _routes_personas,
                  _routes_relationships, _routes_avatars, _routes_memory, _routes_chat):
        mount(router, ctx)
    return router
