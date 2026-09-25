"""控制台端点（**调试面**）—— 与 mod 契约（游戏面）分开（协议.md §8）。

游戏面 = 协议.md 规定的 4 件事 + 状态查询；本文件只服务于本地控制台 UI：
角色卡读写、关系网（含头像）、记忆卡增删改查、聊天记录查看、手动修剪。

约定：
  · 找不到资源 → 404；请求体非法 → 400（与游戏面的"字段非法 → 400"区分开）
  · 记忆的写操作走 memory 模块（= 设计.md §3.1 的"人工手改"通道界面化），
    不新增任何绕过记忆卡的写路径
  · 头像走 data URI（`{"image": "data:image/png;base64,..."}`）而不是 multipart
    —— 免得多引一个 python-multipart 依赖
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from core.logging_config import log

import chatlog
import memory

# 聊天记录一次最多返回多少条
_CHAT_PAGE_MAX = 1000
_EDITABLE_FIELDS = ("content", "importance", "category", "pinned")

# ── 头像 ────────────────────────────────────────────────
_AVATAR_EXT = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp", "gif": ".gif"}
_AVATAR_MAX_BYTES = 2 * 1024 * 1024
_AVATAR_DATA_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=\s]+)$")
# NPC id 会被拼进文件名，必须挡掉路径穿越（中文 id 也要放行）
_ID_RE = re.compile(r"^[\w.-]{1,64}$", re.UNICODE)


def build_router(persona_dir: Path,
                 lock_for: Callable[[str], Any],
                 registry: Any = None,
                 avatar_dir: Optional[Path] = None) -> APIRouter:
    """组装控制台路由。lock_for = 每 NPC 的 asyncio.Lock（与 talk 共用，防并发写覆盖）。

    registry = server 的 Registry（能力清单 + 心跳）；给了才挂 GET /api/capabilities。
    avatar_dir = 头像存放目录（server 里静态挂在 /avatars）；None 表示不启用头像。
    """
    router = APIRouter(prefix="/api", tags=["console"])

    # ── 通用 ────────────────────────────────────────────

    async def _body(request: Request) -> Dict[str, Any]:
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        return data

    def _persona_path(npc_id: str) -> Path:
        return persona_dir / f"{npc_id}.json"

    def _read_persona(npc_id: str) -> Dict[str, Any]:
        path = _persona_path(npc_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"未找到 NPC 角色卡: {path}")
        try:
            data = json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"角色卡 JSON 损坏: {path} ({exc})") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail=f"角色卡必须是 JSON 对象: {path}")
        return data

    # ── 头像文件（avatars/{id}.<ext>，由 server 静态挂在 /avatars）──

    def _valid_id(npc_id: str) -> bool:
        return bool(_ID_RE.match(npc_id)) and npc_id not in (".", "..")

    def _avatar_glob(npc_id: str):
        if avatar_dir is None or not _valid_id(npc_id) or not avatar_dir.is_dir():
            return []
        return [p for p in sorted(avatar_dir.glob(f"{npc_id}.*"))
                if p.is_file() and p.suffix.lower() in _AVATAR_EXT.values()]

    def _avatar_file(npc_id: str) -> Optional[Path]:
        files = _avatar_glob(npc_id)
        return files[0] if files else None

    def _avatar_url(npc_id: str) -> Optional[str]:
        """带 mtime 查询串 —— 换头像后浏览器不会拿旧缓存。"""
        path = _avatar_file(npc_id)
        if path is None:
            return None
        try:
            stamp = int(path.stat().st_mtime)
        except OSError:
            stamp = 0
        return f"/avatars/{path.name}?v={stamp}"

    # ── 能力清单只读视图（协议.md §1 的 POST 是 mod 报到，这里是给控制台看）──

    if registry is not None:
        @router.get("/capabilities")
        async def get_capabilities():
            now = time.time()
            mods = {}
            for mod, entry in getattr(registry, "_mods", {}).items():
                mods[mod] = {
                    "online": registry.online(mod),
                    "last_seen_s_ago": round(now - entry["last_seen"], 1),
                    "actions": entry["actions"],
                }
            return {"mods": mods, "heartbeat_timeout_s": registry.timeout()}

    # ── 角色卡（personas/*.json）───────────────────────────

    @router.get("/personas")
    async def list_personas():
        items: List[Dict[str, Any]] = []
        if persona_dir.exists():
            for path in sorted(p for p in persona_dir.glob("*.json") if not p.name.startswith("_")):
                item: Dict[str, Any] = {"id": path.stem, "path": str(path)}
                try:
                    data = json.loads(path.read_text("utf-8"))
                    if isinstance(data, dict):
                        item["name"] = data.get("name") or path.stem
                        item["identity"] = data.get("identity", "")
                    else:
                        item["error"] = "角色卡必须是 JSON 对象"
                except json.JSONDecodeError as exc:
                    item["error"] = f"JSON 损坏: {exc}"
                items.append(item)
        return {"personas": items}

    @router.get("/personas/{npc_id}")
    async def get_persona(npc_id: str):
        return {"npc_id": npc_id, "persona": _read_persona(npc_id)}

    @router.put("/personas/{npc_id}")
    async def put_persona(npc_id: str, request: Request):
        """整份覆盖写（不存在即新建）。id 字段缺失会自动补成路径里的 id。"""
        persona = await _body(request)
        given = persona.get("id")
        if given is not None and str(given) != npc_id:
            raise HTTPException(
                status_code=400,
                detail=f"角色卡 id（{given}）与路径 id（{npc_id}）不一致")
        persona["id"] = npc_id

        path = _persona_path(npc_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(persona, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        log.info("persona_saved", npc_id=npc_id)
        return {"ok": True, "npc_id": npc_id, "persona": persona}

    # ── 关系网（persona 的 relations 字段驱动；无数据 → 诚实空态）──

    @router.get("/relationships")
    async def get_relationships():
        """关系图数据源 {source, nodes, edges}。

        v4 没有 world/Runtime，关系只有一个来源：`personas/{id}.json` 的可选
        `relations` 字段（制作者手填）。没填 → `source="none"` + 空 edges，
        UI 明确提示，**绝不用假数据顶替**。
        """
        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []
        if persona_dir.exists():
            for path in sorted(p for p in persona_dir.glob("*.json") if not p.name.startswith("_")):
                pid = path.stem
                node: Dict[str, Any] = {"id": pid, "type": "npc", "name": pid,
                                        "identity": "", "error": None,
                                        "avatar": _avatar_url(pid)}
                try:
                    data = json.loads(path.read_text("utf-8"))
                except json.JSONDecodeError as exc:
                    node["error"] = f"JSON 损坏: {exc}"
                    nodes[pid] = node
                    continue
                if isinstance(data, dict):
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
                                  "avatar": _avatar_url(end)}

        source = "persona" if edges else "none"
        return {
            "source": source,
            "nodes": list(nodes.values()),
            "edges": edges,
            "note": ("还没有任何关系数据 —— 在角色卡里加可选字段 "
                     "relations: [{\"who\": \"<对方 id>\", \"how\": \"关系\", \"weight\": 1}]"
                     if source == "none" else ""),
        }

    # ── 头像（关系网节点用；角色卡与合成节点都可挂）──────

    @router.post("/npcs/{npc_id}/avatar")
    async def put_avatar(npc_id: str, request: Request):
        """存头像：前端读文件成 data URI 传来，这里落成 `avatars/{id}.<ext>`。

        换头像直接覆盖旧的（扩展名可能不同，先清干净）。
        """
        if not _valid_id(npc_id):
            raise HTTPException(status_code=400, detail=f"非法的 NPC id: {npc_id}")
        if avatar_dir is None:
            raise HTTPException(status_code=500, detail="服务器未配置头像目录")
        body = await _body(request)
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
        if len(raw) > _AVATAR_MAX_BYTES:
            raise HTTPException(status_code=400,
                                detail=f"图片太大（{len(raw)} 字节，上限 {_AVATAR_MAX_BYTES}）")

        avatar_dir.mkdir(parents=True, exist_ok=True)
        for old in _avatar_glob(npc_id):
            old.unlink()
        target = avatar_dir / f"{npc_id}{_AVATAR_EXT[matched.group(1)]}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
        log.info("avatar_saved", npc_id=npc_id, bytes=len(raw))
        return {"ok": True, "npc_id": npc_id, "bytes": len(raw), "avatar": _avatar_url(npc_id)}

    @router.delete("/npcs/{npc_id}/avatar")
    async def delete_avatar(npc_id: str):
        if not _valid_id(npc_id):
            raise HTTPException(status_code=400, detail=f"非法的 NPC id: {npc_id}")
        files = _avatar_glob(npc_id)
        if not files:
            raise HTTPException(status_code=404, detail=f"该节点还没有头像: {npc_id}")
        for path in files:
            path.unlink()
        log.info("avatar_removed", npc_id=npc_id)
        return {"ok": True, "npc_id": npc_id, "removed": len(files), "avatar": None}

    # ── 记忆卡（store/{id}_memory.json）──────────────────

    def _decorate(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """给每条附上"当前强度 / 是否豁免修剪"，让控制台与修剪口径一致。"""
        now = time.time()
        out = []
        for e in entries:
            item = dict(e)
            item["strength"] = round(memory.strength_of(e, now), 3)
            item["exempt"] = memory.is_exempt(e)
            item["age_hours"] = round(max(0.0, (now - e["created_at"]) / 3600.0), 1)
            out.append(item)
        return out

    @router.get("/npcs/{npc_id}/memory")
    async def list_memory(npc_id: str):
        entries = memory.load_card(npc_id)
        return {
            "npc_id": npc_id,
            "entries": _decorate(entries),
            "count": len(entries),
            "prune_threshold": 1.0,
            "exempt_importance": 8,
        }

    @router.post("/npcs/{npc_id}/memory")
    async def add_memory(npc_id: str, request: Request):
        body = await _body(request)
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(status_code=400, detail="字段 content 必填且必须是非空字符串")
        importance = body.get("importance", memory.DEFAULT_IMPORTANCE)
        category = body.get("category") or memory.DEFAULT_CATEGORY
        pinned = bool(body.get("pinned"))
        async with lock_for(npc_id):
            try:
                entry = memory.add_entry(npc_id, content, importance=importance,
                                         category=category, pinned=pinned)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "entry": _decorate([entry])[0]}

    @router.put("/npcs/{npc_id}/memory/{entry_id}")
    async def update_memory(npc_id: str, entry_id: str, request: Request):
        body = await _body(request)
        fields = {k: v for k, v in body.items() if k in _EDITABLE_FIELDS}
        if not fields:
            raise HTTPException(status_code=400,
                                detail=f"至少要给一个可改字段: {'/'.join(_EDITABLE_FIELDS)}")
        async with lock_for(npc_id):
            try:
                entry = memory.update_entry(npc_id, entry_id, **fields)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if entry is None:
            raise HTTPException(status_code=404, detail=f"未找到记忆条目: {entry_id}")
        return {"ok": True, "entry": _decorate([entry])[0]}

    @router.delete("/npcs/{npc_id}/memory/{entry_id}")
    async def delete_memory(npc_id: str, entry_id: str):
        async with lock_for(npc_id):
            ok = memory.delete_entry(npc_id, entry_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"未找到记忆条目: {entry_id}")
        return {"ok": True, "deleted": entry_id}

    @router.post("/npcs/{npc_id}/prune")
    async def prune_memory(npc_id: str):
        """手动触发确定性修剪（与对话开始时自动跑的是同一个函数）。"""
        async with lock_for(npc_id):
            removed = memory.prune(npc_id)
        return {"ok": True, "npc_id": npc_id, "removed": removed,
                "remaining": len(memory.load_card(npc_id))}

    # ── 聊天记录（store/{id}_chat.jsonl）─────────────────

    @router.get("/npcs/{npc_id}/chat")
    async def get_chat(npc_id: str, limit: int = 200, offset: int = 0):
        """从最新往前取窗口：offset = 已看过的最新条数，limit = 本页条数。"""
        limit = max(1, min(int(limit), _CHAT_PAGE_MAX))
        offset = max(0, int(offset))
        messages = chatlog.load_turns(npc_id)
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
            "summary_threshold": chatlog.token_threshold(),
            "summary": chatlog.load_summary(npc_id),
        }

    return router
