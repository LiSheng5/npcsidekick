"""记忆卡 — 条目 + 加权检索 + 半衰期修剪。

加权公式与半衰期思想的出处（合规留档）：
  · 加权检索公式与参数 —— AI Town (a16z, MIT License,
    https://github.com/a16z-infra/ai-town):
        score = recency × 0.5 + relevance × 3 + importance × 2
        recency = 0.99 ^ 小时
  · 半衰期遗忘思想 —— hippo-memory (https://github.com/kitfunso/hippo-memory):
        强度 = importance × 0.5 ^ (小时 / 72)，强度 < 1.0 的旧条目移除

记忆卡的写入口只有三个：remember 工具、动作结果回报
（add_action_result）、人工手改 JSON 文件。本模块不提供其他写通道。

存储格式：store/{id}_memory.json —— JSON 条目列表，可直接手改。
条目: {id, content, importance, category, created_at, pinned?}
  · id 用 uuid；pinned 为 True 时豁免一切自动修剪（红线）
  · 兼容读旧版记忆卡（content / importance / created_at 字段相同）
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logging_config import log

# 运行时数据目录（工程根/store；测试用 monkeypatch 替换）
STORE_DIR = Path(__file__).resolve().parent / "store"

# ── 记忆卡事件文案单一来源（写卡文案只在此处定义）──────────
EV_DONE = "完成: "
EV_FAIL = "没做成: "

# ── AI Town 加权参数（MIT, a16z-infra）—— 出处见模块 docstring ──
_GW_RECENCY, _GW_RELEVANCE, _GW_IMPORTANCE = 0.5, 3.0, 2.0
_DECAY = 0.99              # 每小时衰减
_HOUR_SECONDS = 3600.0

# ── 半衰期修剪（hippo-memory 思想）──
_HALF_LIFE_HOURS = 72.0
_PRUNE_STRENGTH = 1.0

# 修剪豁免：高重要度 / 人工钉住 / 反思与合并产物
_EXEMPT_CATEGORIES = frozenset({"reflection", "consolidated"})
_EXEMPT_IMPORTANCE = 8

# 动作结果写卡的默认重要度（成功=正事、失败=待办感）
_IMPORTANCE_ACTION_OK = 6
_IMPORTANCE_ACTION_FAIL = 4

DEFAULT_IMPORTANCE = 5
DEFAULT_CATEGORY = "general"

# ── 分词（jieba 可选依赖，缺失时优雅降级 —— 零惩罚哲学）──────
try:                                    # pragma: no cover - 取决于环境
    import jieba  # type: ignore
except ImportError:                     # pragma: no cover
    jieba = None  # type: ignore

_PUNCT = "，。？！、；：,.;:!? \t\r\n（）()「」『』\"'“”‘’—…·/\\"


def _tokenize(text: str) -> List[str]:
    """中文感知切词：有 jieba 用 jieba，没装退回空白切分。"""
    cleaned = text
    for p in _PUNCT:
        cleaned = cleaned.replace(p, " ")
    if jieba is not None:
        return [w.strip() for w in jieba.lcut(cleaned) if w.strip()]
    return [w for w in cleaned.split() if w]


# ── 同义词族（声明驱动：由角色卡 entity_synonyms 声明，引擎保持游戏无关）──
_ACTIVE_SYNONYMS: Dict[str, frozenset] = {}


def set_synonyms(synonyms: Optional[Dict[str, Any]]) -> None:
    """设置生效的同义词族表：{规范词: [别名...]}。

    声明方自己控制别名粒度（单字别名也会参与子串匹配）—— 引擎不预设游戏词表。
    传 None / 空 → 清空（等于没有同义词召回）。
    """
    global _ACTIVE_SYNONYMS
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
    _ACTIVE_SYNONYMS = table


def _canonical_terms(text: str, synonyms: Optional[Dict[str, Any]] = None) -> set:
    """抽取文本命中的规范词（同义词族归一）。"""
    table = _ACTIVE_SYNONYMS if synonyms is None else synonyms
    if not table or not text:
        return set()
    found = set()
    for canon, aliases in table.items():
        if any(a in text for a in aliases):
            found.add(canon)
    return found


def _recency_score(created_at: float, now: float) -> float:
    """AI Town 时效分：0.99 ^ 小时。"""
    hours = max(0.0, (now - created_at) / _HOUR_SECONDS)
    return _DECAY ** hours


def _relevance_score(content: str, query: str,
                     query_tokens: Optional[List[str]] = None,
                     q_canon: Optional[set] = None,
                     c_canon: Optional[set] = None) -> float:
    """相关度 = 分词命中比例 + 同义词族命中比例（归一 0~1）。"""
    tokens = query_tokens if query_tokens is not None else _tokenize(query)
    hit = sum(1 for w in tokens if w in content) / len(tokens) if tokens else 0.0
    if q_canon is None:
        q_canon = _canonical_terms(query)
    syn = 0.0
    if q_canon:
        if c_canon is None:
            c_canon = _canonical_terms(content)
        syn = len(q_canon & c_canon) / len(q_canon)
    return min(1.0, hit + syn)


# ── 路径与读写 ───────────────────────────────────────────

def card_path(npc_id: str) -> Path:
    """记忆卡路径：store/{id}_memory.json。"""
    return STORE_DIR / f"{npc_id}_memory.json"


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


def load_card(npc_id: str) -> List[Dict[str, Any]]:
    """读记忆卡。文件不存在 → 空列表；内容损坏 → 响亮失败（ValueError）。"""
    path = card_path(npc_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"记忆卡 JSON 损坏，无法解析: {path} ({exc})") from exc
    if not isinstance(data, list):
        raise ValueError(f"记忆卡必须是 JSON 数组: {path}")
    return [_normalize_entry(e, i) for i, e in enumerate(data)]


def save_card(npc_id: str, entries: List[Dict[str, Any]]) -> None:
    """原子写记忆卡（先写临时文件再替换），保持可手改的缩进格式。"""
    path = card_path(npc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ── 写入口（三个通道中的两个：remember 工具 / 动作结果回报）─

def add_entry(npc_id: str, content: str, importance: int = DEFAULT_IMPORTANCE,
              category: str = DEFAULT_CATEGORY, pinned: bool = False) -> Dict[str, Any]:
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
    entries = load_card(npc_id)
    entries.append(entry)
    save_card(npc_id, entries)
    log.debug("memory_added", npc_id=npc_id, entry_id=entry["id"],
              importance=entry["importance"], category=entry["category"])
    return entry


def add_action_result(npc_id: str, action: str, ok: bool, note: str = "") -> Dict[str, Any]:
    """动作结果确定性写卡："完成: …" / "没做成: …"。"""
    text = (note or "").strip() or action
    content = f"{EV_DONE if ok else EV_FAIL}{text}"
    return add_entry(
        npc_id,
        content,
        importance=_IMPORTANCE_ACTION_OK if ok else _IMPORTANCE_ACTION_FAIL,
        category="action",
    )


# ── 检索（AI Town 加权公式）───────────────────────────────

def retrieve(npc_id: str, query: str, top_k: int = 5,
             synonyms: Optional[Dict[str, Any]] = None,
             now: Optional[float] = None) -> List[Dict[str, Any]]:
    """加权检索：recency×0.5 + relevance×3 + importance×2，返回前 top_k 条。"""
    entries = load_card(npc_id)
    if not entries:
        return []
    now = time.time() if now is None else now
    table = _ACTIVE_SYNONYMS if synonyms is None else synonyms
    q_canon = _canonical_terms(query, table)
    tokens = _tokenize(query)

    scored = []
    for e in entries:
        rel = _relevance_score(e["content"], query, tokens, q_canon,
                               _canonical_terms(e["content"], table))
        score = (_recency_score(e["created_at"], now) * _GW_RECENCY
                 + rel * _GW_RELEVANCE
                 + e["importance"] * _GW_IMPORTANCE)
        scored.append((score, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:max(0, top_k)]]


def format_for_context(entries: List[Dict[str, Any]]) -> str:
    """把检索结果格式化成给模型的逐字文本（回忆类回答照原文说，不改写）。"""
    if not entries:
        return "（没有想起相关的事）"
    return "\n".join(f"- {e['content']}" for e in entries)


# ── 遗忘（确定性半衰期修剪，不走 LLM）─────────────────────

def is_exempt(entry: Dict[str, Any]) -> bool:
    """是否豁免自动修剪：人工钉住 / importance ≥ 8 / 反思与合并产物。"""
    return (bool(entry.get("pinned"))
            or int(entry.get("importance", DEFAULT_IMPORTANCE)) >= _EXEMPT_IMPORTANCE
            or entry.get("category") in _EXEMPT_CATEGORIES)


def strength_of(entry: Dict[str, Any], now: Optional[float] = None) -> float:
    """当前记忆强度 = importance × 0.5 ^ (小时 / 72)（控制台展示用，与修剪同一口径）。"""
    now = time.time() if now is None else now
    hours = max(0.0, (now - float(entry.get("created_at", now))) / _HOUR_SECONDS)
    return float(entry.get("importance", DEFAULT_IMPORTANCE)) * (0.5 ** (hours / _HALF_LIFE_HOURS))


def prune(npc_id: str, now: Optional[float] = None) -> int:
    """移除强度 < 1.0 的旧条目，返回移除条数。

    强度 = importance × 0.5 ^ (小时 / 72)；豁免：importance ≥ 8、pinned、反思/合并产物。
    """
    entries = load_card(npc_id)
    if not entries:
        return 0
    now = time.time() if now is None else now
    keep = [e for e in entries
            if is_exempt(e) or strength_of(e, now) >= _PRUNE_STRENGTH]
    removed = len(entries) - len(keep)
    if removed:
        save_card(npc_id, keep)
        log.info("memory_pruned", npc_id=npc_id, removed=removed, kept=len(keep))
    return removed


# ── 条目级人工编辑（控制台用的就是"直接手改记忆卡"这条通道）─

def find_entry(npc_id: str, entry_id: str) -> Optional[Dict[str, Any]]:
    for e in load_card(npc_id):
        if e["id"] == entry_id:
            return e
    return None


def update_entry(npc_id: str, entry_id: str, **fields) -> Optional[Dict[str, Any]]:
    """按 id 改一条（content / importance / category / pinned）。找不到 → None。

    pinned=False 会把键删掉（与 add_entry 同款：只有为真才落这个键）。
    """
    editable = ("content", "importance", "category", "pinned")
    entries = load_card(npc_id)
    for i, e in enumerate(entries):
        if e["id"] != entry_id:
            continue
        merged = dict(e)
        merged.update({k: v for k, v in fields.items() if k in editable})
        entries[i] = _normalize_entry(merged, i)
        save_card(npc_id, entries)
        log.debug("memory_updated", npc_id=npc_id, entry_id=entry_id)
        return entries[i]
    return None


def delete_entry(npc_id: str, entry_id: str) -> bool:
    """按 id 删一条。删掉了 → True。"""
    entries = load_card(npc_id)
    keep = [e for e in entries if e["id"] != entry_id]
    if len(keep) == len(entries):
        return False
    save_card(npc_id, keep)
    log.debug("memory_deleted", npc_id=npc_id, entry_id=entry_id)
    return True
