"""
Memory Manager — 统一记忆管理接口。

组合 ShortTermMemory + LongTermMemory + MemoryRetriever + VectorStore，
提供一个统一的 API 给 Planner / Executor / Orchestrator 使用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.memory.short_term import ShortTermMemory
from agent.memory.long_term import LongTermMemory
from agent.memory.retriever import MemoryRetriever
from agent.memory.vector_store import VectorStore


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
        # 向量存储 (可选 — 未安装 chromadb 时降级)
        self.vector_store = VectorStore()

        # 短时 + 长时记忆
        self.short_term = ShortTermMemory(vector_store=self.vector_store)
        self.long_term = LongTermMemory(vector_store=self.vector_store)

        # 检索器
        self.retriever = MemoryRetriever(
            self.short_term, self.long_term, self.vector_store,
        )

        # 迁移: 首次运行时从 JSON 重建向量索引
        self._maybe_rebuild_index()

        # 上下文压缩器 (延迟初始化)
        self._compressor = None

    # ── 对话历史 (short-term) ─────────────────────────

    def add_message(self, role: str, content: str) -> None:
        self.short_term.add(role, content)

    def add_messages(self, messages: List[Dict[str, str]]) -> None:
        self.short_term.add_batch(messages)

    def recent_messages(self, n: int = 20) -> List[Dict[str, str]]:
        return self.short_term.recent(n)

    def get_conversation_context(self, max_chars: int = 4000) -> str:
        return self.short_term.to_context_string(max_chars)

    def get_history_for_context(self, max_messages: int = 20,
                                 max_chars: int = 4000) -> str:
        """
        获取格式化的对话历史，供 Planner/Executor 注入 LLM 上下文。
        不经过语义过滤，直接返回最近消息。
        """
        return self.retriever.get_conversation_history(max_messages, max_chars)

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

    # ── 上下文压缩 ────────────────────────────────────

    def maybe_compress(self, llm) -> int:
        """检查是否需要压缩，需要时执行。返回压缩的消息数。"""
        if not self.short_term.needs_compression:
            return 0
        if not self._compressor:
            from agent.memory.compressor import ContextCompressor
            self._compressor = ContextCompressor(llm, self)
        return self._compressor.compress()

    # ── 生命周期 ──────────────────────────────────────

    def save(self) -> None:
        self.short_term.save()
        self.long_term.save()

    def clear(self) -> None:
        self.short_term.clear()
        self.long_term.facts.clear()
        self.long_term.learnings.clear()
        self.long_term.save()
        if self.vector_store:
            self.vector_store.reset()

    def summarize(self) -> str:
        st_summary = f"短时记忆: {len(self.short_term)} 条消息"
        lt_summary = self.long_term.summarize()
        vs_count = self.vector_store.count() if self.vector_store else 0
        vs_summary = f"向量索引: {vs_count} 条" if vs_count > 0 else ""
        return f"{st_summary}\n{lt_summary}\n{vs_summary}".strip()

    # ── internal ──────────────────────────────────────

    def _maybe_rebuild_index(self) -> None:
        """如果向量存储为空但 JSON 中有数据，重建索引。"""
        if not self.vector_store or not self.vector_store.available:
            return
        if self.vector_store.count() == 0:
            if len(self.short_term) > 0:
                self.short_term.rebuild_index()
            if len(self.long_term) > 0:
                self.long_term.rebuild_index()
