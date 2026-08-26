"""
NPCSidekick — NPC 记忆（加权检索）。

记忆加权公式参考 AI Town (a16z, MIT License, https://github.com/a16z-infra/ai-town):
  score = recency × gw[0] + relevance × gw[1] + importance × gw[2]
  gw = [0.5, 3, 2]; recency = 0.99^小时

设计点 #4: 记忆 = 可编辑文档 — 全部条目存 JSON，用户可打开直接改（改 importance/内容）。

v2026-08-23 中文分词: _relevance_score 切词从"按空格"升级为 jieba（可选依赖）—
中文整句不再糊成一个词。jieba 未安装时优雅回退空格切词（零惩罚哲学:
装不装 jieba 服务都能跑，只是召回质量差异）。
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

try:   # 可选依赖: pip install jieba 后自动启用（缺失不影响启动）
    import jieba  # type: ignore
except ImportError:   # pragma: no cover
    jieba = None  # type: ignore

# AI Town 记忆加权参数 (MIT, a16z-infra) — 见模块 docstring
_GW = (0.5, 3, 2)          # (recency, relevance, importance) 权重
_DECAY = 0.99              # 每小时衰减
_HOUR_SECONDS = 3600

# ── 记忆卡事件文案单一来源(P0-2·2026-08-25)─────────────────
# 协议格式 = 半角冒号+空格。迁移脚本 migrate_memory_cards.R2 的垃圾签名
# 与本常量联动(旧全角存量兼容扫描)。imp 语义: 玩家正事>=8 / 日常=5 / 失败=4或6。
EV_DONE = "完成: "
EV_FAIL = "没做成: "

# 阶段② 轻量海马体: 规范词 → 同义词族（中文同义召回，不依赖 embedding）
# 只收游戏世界的稳定名词（资源/地点/角色），避免过度匹配
_ENTITY_SYNONYMS: Dict[str, frozenset] = {
    "木材": frozenset({"木材", "木头", "柴", "木料", "原木", "木", "树"}),
    "浆果": frozenset({"浆果", "果子", "野果", "果实"}),
    "石头": frozenset({"石头", "岩石", "石块", "石"}),
    "工具": frozenset({"工具", "木石工具", "石器"}),
    "麻绳": frozenset({"麻绳", "结实麻绳", "绳"}),
    "村庄": frozenset({"村庄", "村子", "村里", "家"}),
    "森林": frozenset({"森林", "树林", "林子"}),
    "矿洞": frozenset({"矿洞", "矿山", "洞里"}),
    "河边": frozenset({"河边", "河岸", "河"}),
    "苍": frozenset({"苍", "老猎手", "猎手"}),
    "阿黎": frozenset({"阿黎", "采集者"}),
    "主角": frozenset({"主角", "玩家"}),
}
# 一跳关联加分: 与 query 实体共现的实体所链接的记忆 +0.8/实体（HippoRAG 思想轻量版）
_ASSOCIATION_WEIGHT = 0.8
# 阶段③ 遗忘: 记忆强度半衰期（小时）— 强度 = importance × 0.5^(小时/半衰期)
_HALF_LIFE_HOURS = 72.0

# 切词时剔除的标点/分隔（中英文都收）
_PUNCT = ("，", "。", "？", "！", "、", "；", "：", ",", ".", "?", "!", ";", ":")


def _tokenize(text: str) -> List[str]:
    """中文感知切词: 有 jieba 用 jieba(整句不再糊成一个词), 没装退回空格切。

    返回去空白后的 token 列表; 纯标点 token 已剔除。
    """
    cleaned = text
    for p in _PUNCT:
        cleaned = cleaned.replace(p, " ")
    if jieba is not None:
        return [w.strip() for w in jieba.lcut(cleaned) if w.strip()]
    return [w for w in cleaned.split() if w]


def _canonical_terms(text: str) -> set:
    """抽取文本里命中的规范词（同义词族归一到规范词）。阶段② 轻量海马体。"""
    found = set()
    for canon, aliases in _ENTITY_SYNONYMS.items():
        if any(a in text for a in aliases):
            found.add(canon)
    return found


def _recency_score(created_at: float, now: float) -> float:
    hours = max(0.0, (now - created_at) / _HOUR_SECONDS)
    return _DECAY ** hours


def _relevance_score(content: str, query: str) -> float:
    """相关度: 原词命中 + 同义词召回（阶段② 轻量海马体）。

    原词命中比例保留（确定性基线）；叠加规范词(同义词族)命中，
    解决"柴" vs "木材"这类中文同义漏检。0-1。
    v2026-08-23: 切词经 _tokenize — 中文查询按词命中而不是整句,
    "昨天森林里的木头真粗" 能按 木头→木材 同义族+分词正常计分。
    """
    words = _tokenize(query)
    raw = sum(1 for w in words if w in content) / len(words) if words else 0.0
    q_canon = _canonical_terms(query)
    syn = 0.0
    if q_canon:
        syn = len(q_canon & _canonical_terms(content)) / len(q_canon)
    return min(1.0, raw * 0.6 + syn * 0.8)


class NPCMemory:
    """NPC 记忆: 条目 + 加权检索 + 持久化。"""

    def __init__(self) -> None:
        self.entries: List[Dict] = []   # [{id, content, importance, created_at}]

    # ── 写入 ─────────────────────────────────────────

    def add(self, content: str, importance: int = 5, category: str = "general") -> str:
        """记一条记忆。importance 0-9（可由 LLM 评分，Day 2 起默认手动/规则）。"""
        entry = {
            "id": f"mem_{len(self.entries) + 1}_{int(time.time())}",
            "content": content,
            "importance": max(0, min(9, importance)),
            "category": category,
            "created_at": time.time(),
        }
        self.entries.append(entry)
        return entry["id"]

    # ── 检索（AI Town 加权公式）────────────────────────

    def retrieve(self, query: str = "", top_k: int = 5) -> List[Dict]:
        """加权检索 + 一跳关联（阶段② 轻量海马体）。

        score = recency×0.5 + relevance×3 + importance×2 + 关联加分。
        关联: 与 query 实体共现的实体所链接的记忆 +0.8/实体（拐弯找相关）。
        """
        now = time.time()
        q_canon = _canonical_terms(query)
        assoc: set = set()
        if q_canon:
            for e in self.entries:
                e_canon = _canonical_terms(e["content"])
                if q_canon & e_canon:
                    assoc |= e_canon
            assoc -= q_canon
        scored = []
        for e in self.entries:
            score = (
                _recency_score(e["created_at"], now) * _GW[0]
                + _relevance_score(e["content"], query) * _GW[1]
                + e["importance"] * _GW[2]
            )
            if assoc:
                score += len(_canonical_terms(e["content"]) & assoc) * _ASSOCIATION_WEIGHT
            scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def all(self) -> List[Dict]:
        return list(self.entries)

    # ── 持久化（记忆卡 = 可编辑文档）──────────────────

    def to_dict(self) -> List[Dict]:
        return self.entries

    def load(self, data: List[Dict]) -> None:
        self.entries = list(data)

    def consolidate(self, min_group: int = 3, prune_strength: float = 1.0) -> int:
        """遗忘合并（阶段③）: 重复主题合并成一条 + 弱旧记忆修剪。

        - 合并: 同主题(规范词交集)的非反思记忆 ≥ min_group 条 → 合并成一条
          （"模式留存，流水账淡去" — hippo-memory sleep 思想）。只复述事实（次数+最近一次），不编造。
        - 修剪: 强度 = importance × 0.5^(小时/半衰期)，低于 prune_strength 的旧条目移除；
          反思/合并条目与高重要度(≥8)保留。
        - 返回被移除（含被合并）的条目数。
        """
        now = time.time()
        removed = 0
        # ── 1. 同主题合并 ──
        groups: Dict[frozenset, List[int]] = {}
        for i, e in enumerate(self.entries):
            key = frozenset(_canonical_terms(e["content"]))
            if key:
                groups.setdefault(key, []).append(i)
        drop = set()
        for key, idxs in groups.items():
            cand = [i for i in idxs if self.entries[i].get("category") != "reflection"]
            if len(cand) < min_group:
                continue
            top = max(cand, key=lambda i: self.entries[i].get("importance", 5))
            topic = "、".join(sorted(key))
            summary = f"（已合并）关于{topic}的经历共 {len(cand)} 次；最近一次：{self.entries[top]['content']}"
            self.entries.append({
                "id": f"mem_c_{len(self.entries) + 1}_{int(now)}",
                "content": summary,
                "importance": max(8, self.entries[top].get("importance", 5)),
                "category": "consolidated",
                "created_at": self.entries[top]["created_at"],
            })
            for i in cand:
                drop.add(i)
            removed += len(cand)
        if drop:
            self.entries = [e for i, e in enumerate(self.entries) if i not in drop]
        # ── 2. 弱旧修剪 ──
        keep = []
        for e in self.entries:
            if e.get("category") in ("reflection", "consolidated"):
                keep.append(e)
                continue
            if e.get("importance", 5) >= 8:
                keep.append(e)
                continue
            hours = max(0.0, (now - e["created_at"]) / _HOUR_SECONDS)
            strength = e.get("importance", 5) * (0.5 ** (hours / _HALF_LIFE_HOURS))
            if strength >= prune_strength:
                keep.append(e)
            else:
                removed += 1
        self.entries = keep
        return removed

    def format_for_context(self, entries: Optional[List[Dict]] = None) -> str:
        """把记忆条目格式化成模型上下文文本。"""
        items = entries if entries is not None else self.entries[-5:]
        if not items:
            return "（还没有记忆）"
        return "\n".join(f"- {e['content']}" for e in items)
