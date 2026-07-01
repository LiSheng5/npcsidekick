"""
记忆检索器 — 智能检索记忆，供 Planner 和 Executor 使用。

功能:
  - 多策略检索: 关键词 / 时间范围 / 语义关联
  - 支持 ReAct 式检索: 先搜 → 根据结果决定是否再搜
  - Planner 在规划时可调用此模块获取上下文
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from agent.memory.short_term import ShortTermMemory
from agent.memory.long_term import LongTermMemory


class MemoryRetriever:
    """
    记忆检索器 — Planner 使用它获取任务相关的历史上下文。

    使用方式:
      retriever = MemoryRetriever(stm, ltm)
      context = retriever.retrieve("用户在学 Python", max_items=5)
    """

    def __init__(self, stm: ShortTermMemory, ltm: LongTermMemory):
        self.stm = stm
        self.ltm = ltm

    def retrieve(self, query: str, max_items: int = 10) -> Dict[str, Any]:
        """
        根据查询检索所有相关记忆。

        返回:
          {
            "query": "...",
            "short_term": [...],    # 相关对话
            "long_term": {
              "facts": [...],
              "learnings": [...],
              "executions": [...],
            },
            "summary": "..."        # 人类可读的摘要
          }
        """
        # 短时记忆: 搜索最近的对话
        st_results = []
        for item in self.stm.recent(50):
            if query.lower() in str(item.get("content", "")).lower():
                st_results.append(item)

        # 长时记忆
        lt_results = self.ltm.search_all(query)

        return {
            "query": query,
            "short_term": st_results[-max_items:],
            "long_term": lt_results,
            "summary": self._build_summary(query, st_results, lt_results),
        }

    def retrieve_for_planning(self, user_input: str) -> str:
        """
        为 Planner 检索上下文，返回格式化的字符串。
        """
        result = self.retrieve(user_input)
        parts = []

        if result["long_term"]["facts"]:
            parts.append("### 相关长期事实")
            for f in result["long_term"]["facts"][-5:]:
                parts.append(f"- {f.get('content', '')[:200]}")

        if result["long_term"]["learnings"]:
            parts.append("### 学到的规律")
            for l in result["long_term"]["learnings"][-3:]:
                parts.append(f"- {l.get('content', '')[:200]}")

        if result["short_term"]:
            parts.append("### 相关对话")
            for item in result["short_term"][-5:]:
                parts.append(f"[{item.get('role')}] {item.get('content', '')[:150]}")

        return "\n\n".join(parts) if parts else "暂无相关上下文。"

    def _build_summary(self, query: str, st: List, lt: Dict) -> str:
        parts = [f"搜索 '{query}':"]
        if st:
            parts.append(f"  短时记忆: {len(st)} 条匹配")
        if lt.get("facts"):
            parts.append(f"  长期事实: {len(lt['facts'])} 条匹配")
        if lt.get("learnings"):
            parts.append(f"  学到的规律: {len(lt['learnings'])} 条匹配")
        return "\n".join(parts)
