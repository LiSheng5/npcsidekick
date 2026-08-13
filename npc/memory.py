"""
NPCSidekick — NPC 记忆（加权检索）。

记忆加权公式参考 AI Town (a16z, MIT License, https://github.com/a16z-infra/ai-town):
  score = recency × gw[0] + relevance × gw[1] + importance × gw[2]
  gw = [0.5, 3, 2]; recency = 0.99^小时

设计点 #4: 记忆 = 可编辑文档 — 全部条目存 JSON，用户可打开直接改（改 importance/内容）。
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

# AI Town 记忆加权参数 (MIT, a16z-infra) — 见模块 docstring
_GW = (0.5, 3, 2)          # (recency, relevance, importance) 权重
_DECAY = 0.99              # 每小时衰减
_HOUR_SECONDS = 3600


def _recency_score(created_at: float, now: float) -> float:
    hours = max(0.0, (now - created_at) / _HOUR_SECONDS)
    return _DECAY ** hours


def _relevance_score(content: str, query: str) -> float:
    """关键词相关度: 查询词在记忆中出现的比例 (0-1)。文本世界足够, 向量检索在 P1。"""
    words = [w for w in query.replace("，", " ").replace("。", " ").split() if w]
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in content)
    return hits / len(words)


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
        """加权检索: score = recency×0.5 + relevance×3 + importance×2。"""
        now = time.time()
        scored = []
        for e in self.entries:
            score = (
                _recency_score(e["created_at"], now) * _GW[0]
                + _relevance_score(e["content"], query) * _GW[1]
                + e["importance"] * _GW[2]
            )
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

    def format_for_context(self, entries: Optional[List[Dict]] = None) -> str:
        """把记忆条目格式化成模型上下文文本。"""
        items = entries if entries is not None else self.entries[-5:]
        if not items:
            return "（还没有记忆）"
        return "\n".join(f"- {e['content']}" for e in items)
