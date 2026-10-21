"""记忆卡 — 条目 + 相关度检索。

记忆卡的写入口只有三个：remember 工具（remember_entry）、动作结果回报
（add_action_result）、人工手改 JSON 文件。本模块不提供其他写通道。

存储格式：store/{id}_memory.json —— JSON 条目列表，可直接手改。
条目: {id, content, importance, category, created_at, pinned?}
  · id 用 uuid；pinned 只影响预取优先级（回合开始优先塞进记忆预算）
  · 兼容读旧版记忆卡（content / importance / created_at 字段相同）
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config_flags import env_num
from core.logging_config import log
from core.paths import resolve_store

# ── 记忆卡事件文案单一来源（写卡文案只在此处定义）──────────
EV_DONE = "完成: "
EV_FAIL = "没做成: "

# ── recall 检索参数（排序权重是自己定的初值，等评测集再调）──
RECALL_MIN_REL = 0.2        # 环境变量 NPC_MEM_RECALL_MIN_REL，现读可热切
_W_REL, _W_IMP = 1.0, 0.3   # 排序权重：相关度为主、重要度为辅

# 条目短编号（id 前 6 位）：模型用它填 remember 的 replaces
_SHORT_ID_LEN = 6

# 动作结果写卡的默认重要度（成功=正事、失败=待办感）
_IMPORTANCE_ACTION_OK = 6
_IMPORTANCE_ACTION_FAIL = 4

DEFAULT_IMPORTANCE = 5
DEFAULT_CATEGORY = "general"

# "参数未显式传入"哨兵（与显式传默认值区分开，remember_entry 用）
_UNSET: Any = object()

# ── 分词（jieba 可选依赖，缺失时优雅降级 —— 零惩罚哲学）──────
try:                                    # pragma: no cover - 取决于环境
    import jieba  # type: ignore
except ImportError:                     # pragma: no cover
    jieba = None  # type: ignore

_PUNCT = "，。？！、；：,.;:!? \t\r\n（）()「」『』\"'“”‘’—…·/\\"


def _fallback_tokens(text: str) -> List[str]:
    """没装 jieba 时的兜底切词：按标点和空白切段后——
    连续的 ASCII 字母数字当一个词，连续的中日韩字符取字二元组
    （长度 1 的片段保留该单字）。
    """
    tokens: List[str] = []
    for seg in text.split():
        runs: List[tuple] = []
        cur, kind = "", None
        for ch in seg:
            k = ("ascii" if ch.isascii() and ch.isalnum()
                 else "cjk" if not ch.isascii() else None)
            if k is None:
                if cur:
                    runs.append((kind, cur))
                    cur, kind = "", None
                continue
            if k == kind:
                cur += ch
            else:
                if cur:
                    runs.append((kind, cur))
                cur, kind = ch, k
        if cur:
            runs.append((kind, cur))
        for k, chunk in runs:
            if k == "ascii" or len(chunk) == 1:
                tokens.append(chunk)
            else:
                tokens.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return tokens


def _tokenize(text: str) -> List[str]:
    """中文感知切词：有 jieba 用 jieba，没装用字二元组兜底。"""
    cleaned = text
    for p in _PUNCT:
        cleaned = cleaned.replace(p, " ")
    if jieba is not None:
        return [w.strip() for w in jieba.lcut(cleaned) if w.strip()]
    return _fallback_tokens(cleaned)


# ── 同义词族（声明驱动：由角色卡 entity_synonyms 声明，引擎保持游戏无关）──
# 没有进程级全局表：检索用的同义词表**每次显式传入**（取自该 NPC 的角色卡），
# 多 NPC 并发对话互不污染。


def _normalize_synonyms(synonyms: Optional[Dict[str, Any]]) -> Dict[str, frozenset]:
    """归一同义词表：{规范词: 别名列表/单个别名} → {规范词: frozenset(别名)}。

    声明方自己控制别名粒度（单字别名也会参与子串匹配）—— 引擎不预设游戏词表。
    非法条目跳过；整表不是 dict / 传 None → 空表（等于没有同义词召回）。
    """
    table: Dict[str, frozenset] = {}
    if isinstance(synonyms, dict):
        for canon, aliases in synonyms.items():
            if not isinstance(canon, str) or not canon.strip():
                continue
            if isinstance(aliases, (list, tuple, set, frozenset)):
                words = frozenset(str(a) for a in aliases if str(a).strip())
            elif isinstance(aliases, str):
                words = frozenset({aliases})
            else:
                continue
            if words:
                table[canon.strip()] = words
    return table


def _canonical_terms(text: str, table: Optional[Dict[str, Any]]) -> set:
    """抽取文本命中的规范词（同义词族归一）。table 为空 → 无同义词召回。"""
    if not table or not text:
        return set()
    found = set()
    for canon, aliases in table.items():
        if any(a in text for a in aliases):
            found.add(canon)
    return found


def _relevance_score(content: str, query: str,
                     query_tokens: Optional[List[str]] = None,
                     q_canon: Optional[set] = None,
                     c_canon: Optional[set] = None,
                     table: Optional[Dict[str, Any]] = None) -> float:
    """相关度 = 分词命中比例 + 同义词族命中比例（归一 0~1）。

    table = 本次检索用的同义词表（显式传入）；q_canon / c_canon 没预算时才在这里算。
    """
    tokens = query_tokens if query_tokens is not None else _tokenize(query)
    hit = sum(1 for w in tokens if w in content) / len(tokens) if tokens else 0.0
    if q_canon is None:
        q_canon = _canonical_terms(query, table)
    syn = 0.0
    if q_canon:
        if c_canon is None:
            c_canon = _canonical_terms(content, table)
        syn = len(q_canon & c_canon) / len(q_canon)
    return min(1.0, hit + syn)


# ── 路径与读写 ───────────────────────────────────────────

def card_path(npc_id: str, store_dir: Optional[Path] = None) -> Path:
    """记忆卡路径：store/{id}_memory.json。store_dir=None → 工程默认 store/。"""
    return resolve_store(store_dir) / f"{npc_id}_memory.json"


def _normalize_entry(raw: Any, index: int) -> Dict[str, Any]:
    """归一单条记忆：补 uuid / 默认字段，兼容旧版卡。"""
    if not isinstance(raw, dict):
        raise ValueError(f"记忆卡第 {index} 条不是 JSON 对象: {type(raw).__name__}")
    content = raw.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"记忆卡第 {index} 条缺少 content")
    entry = dict(raw)
    entry["content"] = content
    eid = raw.get("id")
    entry["id"] = eid if isinstance(eid, str) and eid.strip() else uuid.uuid4().hex
    imp = raw.get("importance", DEFAULT_IMPORTANCE)
    if isinstance(imp, bool) or not isinstance(imp, (int, float)):
        imp = DEFAULT_IMPORTANCE
    entry["importance"] = max(0, min(9, int(imp)))
    cat = raw.get("category")
    entry["category"] = cat if isinstance(cat, str) and cat.strip() else DEFAULT_CATEGORY
    created = raw.get("created_at", time.time())
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        created = time.time()
    entry["created_at"] = float(created)
    # pinned 只在为真时保留（可选键：为假就不落这个键）
    if not raw.get("pinned"):
        entry.pop("pinned", None)
    return entry


def load_card(npc_id: str, store_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """读记忆卡。文件不存在 → 空列表；内容损坏 → 响亮失败（ValueError）。"""
    path = card_path(npc_id, store_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"记忆卡 JSON 损坏，无法解析: {path} ({exc})") from exc
    if not isinstance(data, list):
        raise ValueError(f"记忆卡必须是 JSON 数组: {path}")
    return [_normalize_entry(e, i) for i, e in enumerate(data)]


def save_card(npc_id: str, entries: List[Dict[str, Any]],
              store_dir: Optional[Path] = None) -> None:
    """原子写记忆卡（先写临时文件再替换），保持可手改的缩进格式。"""
    path = card_path(npc_id, store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ── 写入口（三个通道中的两个：remember 工具 / 动作结果回报）─

def add_entry(npc_id: str, content: str, importance: int = DEFAULT_IMPORTANCE,
              category: str = DEFAULT_CATEGORY, pinned: bool = False,
              store_dir: Optional[Path] = None) -> Dict[str, Any]:
    """记一条记忆（remember 工具 / 人工调用）。importance 0-9。"""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("记忆内容不能为空")
    entry = _normalize_entry({
        "id": uuid.uuid4().hex,
        "content": content,
        "importance": importance,
        "category": category,
        "created_at": time.time(),
        "pinned": bool(pinned),
    }, 0)
    entries = load_card(npc_id, store_dir=store_dir)
    entries.append(entry)
    save_card(npc_id, entries, store_dir=store_dir)
    log.debug("memory_added", npc_id=npc_id, entry_id=entry["id"],
              importance=entry["importance"], category=entry["category"])
    return entry


def add_action_result(npc_id: str, action: str, ok: bool, note: str = "",
                      store_dir: Optional[Path] = None) -> Dict[str, Any]:
    """动作结果确定性写卡："完成: …" / "没做成: …"。"""
    text = (note or "").strip() or action
    content = f"{EV_DONE if ok else EV_FAIL}{text}"
    return add_entry(
        npc_id,
        content,
        importance=_IMPORTANCE_ACTION_OK if ok else _IMPORTANCE_ACTION_FAIL,
        category="action",
        store_dir=store_dir,
    )


# ── 条目短编号与 replaces（remember 工具的更新通道）────────

def resolve_id(npc_id: str, prefix: str,
               store_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """按短编号前缀找条目：在非 category == "action" 的条目里**恰好唯一匹配**才返回。

    没有匹配 / 多个匹配 / 指向 action 条目 → None（调用方自行决定当新记忆记）。
    """
    p = (prefix or "").strip()
    if not p:
        return None
    matches = [e for e in load_card(npc_id, store_dir=store_dir)
               if e.get("category") != "action" and str(e["id"]).startswith(p)]
    return matches[0] if len(matches) == 1 else None


def remember_entry(npc_id: str, content: str,
                   importance: Any = _UNSET, category: Any = _UNSET,
                   replaces: Optional[str] = None,
                   store_dir: Optional[Path] = None) -> Tuple[Dict[str, Any], bool]:
    """remember 工具写卡：replaces 命中旧条目就原地更新，否则新增。返回 (条目, 是否更新)。

    - replaces 能解析到条目（resolve_id 唯一命中）→ 原地更新 content；
      importance / category 仅在调用方显式传了才改；保留原 id / created_at / pinned；
      返回 (entry, True)。
    - replaces 为空 → 等同 add_entry（importance / category 未显式传时用默认值）；
      返回 (entry, False)。
    - replaces 给了但解析不到（没匹配 / 多个匹配 / 指向 action 条目）→ 当新记忆记下，
      返回 (entry, False)，不报错。
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("记忆内容不能为空")
    old = resolve_id(npc_id, replaces, store_dir=store_dir) if replaces else None
    if old is not None:
        fields: Dict[str, Any] = {"content": content}
        if importance is not _UNSET:
            fields["importance"] = importance
        if category is not _UNSET:
            fields["category"] = category
        entry = update_entry(npc_id, old["id"], store_dir=store_dir, **fields)
        log.debug("memory_replaced", npc_id=npc_id, entry_id=old["id"])
        return entry, True
    return add_entry(npc_id, content,
                     importance=DEFAULT_IMPORTANCE if importance is _UNSET else importance,
                     category=DEFAULT_CATEGORY if category is _UNSET else category,
                     store_dir=store_dir), False


# ── 检索（相关度门槛 + 相关度为主、重要度为辅）─────────────

def _recall_min_rel() -> float:
    """recall 相关度门槛（现读，可热切）：NPC_MEM_RECALL_MIN_REL，默认 RECALL_MIN_REL。"""
    return max(0.0, float(env_num("NPC_MEM_RECALL_MIN_REL", RECALL_MIN_REL)))


def retrieve(npc_id: str, query: str, top_k: int = 5,
             synonyms: Optional[Dict[str, Any]] = None,
             store_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """相关度检索：候选 = 相关度 ≥ 门槛，排序 = 相关度为主、重要度为辅，返回前 top_k 条。

    不相关的条目不返回（哪怕重要度高）；全不相关 → 空列表。
    synonyms = 本次检索的同义词表（通常来自该 NPC 的角色卡 entity_synonyms）；
    不传 = 没有同义词召回（没有进程级全局表，多 NPC 并发互不污染）。
    """
    entries = load_card(npc_id, store_dir=store_dir)
    if not entries:
        return []
    table = _normalize_synonyms(synonyms)
    q_canon = _canonical_terms(query, table)
    tokens = _tokenize(query)
    min_rel = _recall_min_rel()

    scored = []
    for e in entries:
        rel = _relevance_score(e["content"], query, tokens, q_canon,
                               _canonical_terms(e["content"], table), table=table)
        if rel < min_rel:
            continue
        rank = rel * _W_REL + (e["importance"] / 9) * _W_IMP
        scored.append((rank, e))
    scored.sort(key=lambda x: (-x[0], -x[1]["created_at"]))
    return [e for _, e in scored[:max(0, top_k)]]


def format_for_context(entries: List[Dict[str, Any]]) -> str:
    """把检索结果格式化成给模型的逐字文本（回忆类回答照原文说，不改写）。

    每行带条目 id 的 6 位短编号，供 remember 的 replaces 使用。
    """
    if not entries:
        return "（没有想起相关的事）"
    return "\n".join(f"- [{e['id'][:_SHORT_ID_LEN]}] {e['content']}" for e in entries)


# ── 条目级人工编辑（控制台用的就是"直接手改记忆卡"这条通道）─

def find_entry(npc_id: str, entry_id: str,
               store_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    for e in load_card(npc_id, store_dir=store_dir):
        if e["id"] == entry_id:
            return e
    return None


def update_entry(npc_id: str, entry_id: str,
                 store_dir: Optional[Path] = None, **fields) -> Optional[Dict[str, Any]]:
    """按 id 改一条（content / importance / category / pinned）。找不到 → None。

    pinned=False 会把键删掉（与 add_entry 同款：只有为真才落这个键）。
    """
    editable = ("content", "importance", "category", "pinned")
    entries = load_card(npc_id, store_dir=store_dir)
    for i, e in enumerate(entries):
        if e["id"] != entry_id:
            continue
        merged = dict(e)
        merged.update({k: v for k, v in fields.items() if k in editable})
        entries[i] = _normalize_entry(merged, i)
        save_card(npc_id, entries, store_dir=store_dir)
        log.debug("memory_updated", npc_id=npc_id, entry_id=entry_id)
        return entries[i]
    return None


def delete_entry(npc_id: str, entry_id: str,
                 store_dir: Optional[Path] = None) -> bool:
    """按 id 删一条。删掉了 → True。"""
    entries = load_card(npc_id, store_dir=store_dir)
    keep = [e for e in entries if e["id"] != entry_id]
    if len(keep) == len(entries):
        return False
    save_card(npc_id, keep, store_dir=store_dir)
    log.debug("memory_deleted", npc_id=npc_id, entry_id=entry_id)
    return True
