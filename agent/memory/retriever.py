"""
记忆检索器 — 语义搜索 + 关键词回退，供 Planner 和 Executor 使用。

检索策略:
  1. 优先: 向量语义搜索 (ChromaDB)
  2. 回退: 关键词子串匹配
  3. 始终: 最近 N 条对话历史
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING
from agent.memory.short_term import ShortTermMemory
from agent.memory.long_term import LongTermMemory

if TYPE_CHECKING:
    from agent.memory.vector_store import VectorStore


class MemoryRetriever:
    """
    记忆检索器 — Planner 使用它获取任务相关的历史上下文。

    使用方式:
      retriever = MemoryRetriever(stm, ltm, vector_store)
      context = retriever.retrieve("用户在学 Python", max_items=5)
    """

    def __init__(self, stm: ShortTermMemory, ltm: LongTermMemory,
                 vector_store: Optional["VectorStore"] = None):
        self.stm = stm
        self.ltm = ltm
        self.vs = vector_store

    def retrieve(self, query: str, max_items: int = 10) -> Dict[str, Any]:
        """根据查询检索相关记忆 (语义搜索优先，关键词回退)。"""
        st_results = []
        lt_facts = []
        lt_learnings = []

        # ── 1. 向量语义搜索 ──────────────────────────
        if self.vs and self.vs.available:
            results = self.vs.search(query, top_k=15)
            for r in results:
                source = r.metadata.get("source", "")
                if source == "message":
                    idx = int(r.metadata.get("index", -1))
                    if 0 <= idx < len(self.stm.history):
                        st_results.append(self.stm.history[idx])
                elif source == "fact":
                    fact_id = r.metadata.get("id", "")
                    fact = self.ltm.get_fact(fact_id)
                    if fact:
                        lt_facts.append(fact)
                elif source == "learning":
                    learn_id = r.metadata.get("id", "")
                    for l in self.ltm.learnings:
                        if l["id"] == learn_id:
                            lt_learnings.append(l)
                            break

        # ── 2. 关键词回退 (结果不足时) ────────────────
        total_hits = len(st_results) + len(lt_facts) + len(lt_learnings)
        if total_hits < 5:
            # 补充关键词搜索
            q = query.lower()
            if len(st_results) < 3:
                for item in self.stm.recent(50):
                    if q in str(item.get("content", "")).lower():
                        if item not in st_results:
                            st_results.append(item)
            lt_keyword = self.ltm.search_all(query)
            for f in (lt_keyword.get("facts") or []):
                if f not in lt_facts:
                    lt_facts.append(f)
            for l in (lt_keyword.get("learnings") or []):
                if l not in lt_learnings:
                    lt_learnings.append(l)

        return {
            "query": query,
            "short_term": st_results[-max_items:],
            "long_term": {
                "facts": lt_facts[-max_items:],
                "learnings": lt_learnings[-max_items:],
                "executions": [],
            },
            "summary": self._build_summary(query, st_results, lt_facts, lt_learnings),
        }

    def retrieve_for_planning(self, user_input: str) -> str:
        """为 Planner 检索上下文，返回格式化字符串。"""
        result = self.retrieve(user_input)
        parts = []

        # 首要: 最近对话历史
        conversation_history = self._format_conversation_history(max_messages=20)
        if conversation_history:
            parts.append("## 对话历史 (最近)")
            parts.append(conversation_history)

        # ── 次要: 语义检索到的长期记忆 ──────────────────
        if result["long_term"]["facts"]:
            parts.append("## 相关长期事实")
            for f in result["long_term"]["facts"][-5:]:
                parts.append(f"- {f.get('content', '')[:200]}")

        if result["long_term"]["learnings"]:
            parts.append("## 学到的规律")
            for l in result["long_term"]["learnings"][-3:]:
                parts.append(f"- {l.get('content', '')[:200]}")

        # 语义匹配到的对话片段 (可能与上面重复，但提供更多上下文)
        if result["short_term"]:
            parts.append("## 语义相关对话")
            for item in result["short_term"][-5:]:
                parts.append(f"[{item.get('role')}] {item.get('content', '')[:150]}")

        return "\n\n".join(parts) if parts else "暂无相关上下文。"

    # ── 对话历史格式化 ──────────────────────────────────

    def get_conversation_history(self, max_messages: int = 20,
                                  max_chars: int = 4000) -> str:
        """获取格式化的对话历史，供 Planner 和 Executor 使用。"""
        return self._format_conversation_history(max_messages, max_chars)

    def _format_conversation_history(self, max_messages: int = 20,
                                      max_chars: int = 4000) -> str:
        """格式化对话历史为字符串。优先保留最新消息。"""
        messages = self.stm.recent(max_messages)
        if not messages:
            return ""

        per_msg_limit = max(100, max_chars // max_messages)

        # 格式化每条消息 (从旧到新)
        formatted = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if len(content) > per_msg_limit:
                content = content[:per_msg_limit] + "..."
            formatted.append(f"[{role}] {content}")

        # 如果总字符数超限，从旧端丢弃 (保留最新消息)
        total = sum(len(line) + 1 for line in formatted)  # +1 for \n
        while total > max_chars and len(formatted) > 1:
            removed = formatted.pop(0)  # 丢弃最旧
            total -= (len(removed) + 1)

        return "\n".join(formatted)

    # ── internal ────────────────────────────────────────

    def _build_summary(self, query: str, st: List, facts: List, learns: List) -> str:
        parts = [f"搜索 '{query}':"]
        if st:
            parts.append(f"  短时记忆: {len(st)} 条匹配")
        if facts:
            parts.append(f"  长期事实: {len(facts)} 条匹配")
        if learns:
            parts.append(f"  学到的规律: {len(learns)} 条匹配")
        return "\n".join(parts)
