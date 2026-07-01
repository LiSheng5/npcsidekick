"""
Memory Manager — 统一记忆管理接口。

组合 ShortTermMemory + LongTermMemory + MemoryRetriever，
提供一个统一的 API 给 Planner / Executor / Orchestrator 使用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.memory.short_term import ShortTermMemory
from agent.memory.long_term import LongTermMemory
from agent.memory.retriever import MemoryRetriever


class MemoryManager:
    """
    统一记忆管理。

    使用方式:
      mm = MemoryManager()
      mm.add_message("user", "你好")
      mm.remember("用户名是 Li")
      context = mm.retrieve_for_planning("用户想学 Python")
    """

    def __init__(self):
        self.short_term = ShortTermMemory()
        self.long_term = LongTermMemory()
        self.retriever = MemoryRetriever(self.short_term, self.long_term)

    # ── 对话历史 (short-term) ─────────────────────────

    def add_message(self, role: str, content: str) -> None:
        self.short_term.add(role, content)

    def add_messages(self, messages: List[Dict[str, str]]) -> None:
        self.short_term.add_batch(messages)

    def recent_messages(self, n: int = 20) -> List[Dict[str, str]]:
        return self.short_term.recent(n)

    def get_conversation_context(self, max_chars: int = 4000) -> str:
        return self.short_term.to_context_string(max_chars)

    # ── 事实 / 偏好 (long-term) ───────────────────────

    def remember(self, fact: str, category: str = "general") -> str:
        return self.long_term.add_fact(fact, category)

    def forget(self, fact_id: str) -> bool:
        return self.long_term.forget_fact(fact_id)

    def recall(self, query: str) -> List[Dict]:
        return self.long_term.search_facts(query)

    # ── 学到的规律 ────────────────────────────────────

    def learn(self, content: str, source: str = "inference") -> str:
        return self.long_term.add_learning(content, source)

    # ── 执行日志 ──────────────────────────────────────

    def log_execution(self, entry: Dict[str, Any]) -> None:
        self.long_term.log_execution(entry)

    def recent_executions(self, n: int = 5) -> List[Dict]:
        return self.long_term.recent_executions(n)

    # ── 检索 (unified) ────────────────────────────────

    def retrieve(self, query: str, max_items: int = 10) -> Dict[str, Any]:
        return self.retriever.retrieve(query, max_items)

    def retrieve_for_planning(self, user_input: str) -> str:
        return self.retriever.retrieve_for_planning(user_input)

    # ── 生命周期 ──────────────────────────────────────

    def save(self) -> None:
        self.short_term.save()
        self.long_term.save()

    def clear(self) -> None:
        self.short_term.clear()
        self.long_term.facts.clear()
        self.long_term.learnings.clear()
        self.long_term.save()

    def summarize(self) -> str:
        st_summary = f"短时记忆: {len(self.short_term)} 条消息"
        lt_summary = self.long_term.summarize()
        return f"{st_summary}\n{lt_summary}"
