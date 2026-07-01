"""
测试 MemoryManager — 统一记忆管理接口。
"""

import json
import tempfile
from pathlib import Path
import pytest

from agent.memory.memory_manager import MemoryManager
from agent.memory.short_term import ShortTermMemory
from agent.memory.long_term import LongTermMemory
from agent.memory.retriever import MemoryRetriever


# ── Helpers ────────────────────────────────────────────────


def _temp_stm_path():
    """创建临时文件路径供 ShortTermMemory 使用。"""
    return Path(tempfile.mktemp(suffix=".json"))


def _make_stm():
    """创建使用临时文件路径的 ShortTermMemory。"""
    return ShortTermMemory(file_path=_temp_stm_path())


class TestMemoryManagerBasic:
    """MemoryManager 基本功能测试。"""

    def test_initialization(self):
        """MemoryManager 应该正确初始化所有子组件。"""
        mm = MemoryManager()

        assert mm.short_term is not None
        assert mm.long_term is not None
        assert mm.retriever is not None
        # VectorStore always created, but may not have chromadb installed
        assert mm.vector_store is not None

    def test_add_and_retrieve_message(self):
        """添加消息后应该能被检索到。"""
        mm = MemoryManager()

        mm.add_message("user", "你好，请问 Python 的列表如何反转？")
        mm.add_message("assistant", "你可以使用 list.reverse() 方法。")

        recent = mm.recent_messages(2)
        assert len(recent) == 2
        assert recent[0]["role"] == "user"
        assert recent[1]["role"] == "assistant"

    def test_get_history_for_context(self):
        """get_history_for_context 返回格式化历史。"""
        mm = MemoryManager()

        mm.add_message("user", "问题 1")
        mm.add_message("assistant", "回答 1")

        history = mm.get_history_for_context(max_messages=5)
        assert "[user]" in history
        assert "问题 1" in history

    def test_retrieve_for_planning(self):
        """retrieve_for_planning 返回含对话历史的上下文。"""
        mm = MemoryManager()

        mm.add_message("user", "帮我调试代码")
        mm.add_message("assistant", "好的，让我看看代码")

        context = mm.retrieve_for_planning("调试代码")

        assert "对话历史" in context or "[user]" in context

    def test_remember_and_recall(self):
        """remember + recall 长期记忆往返。"""
        mm = MemoryManager()

        mm.remember("用户名叫 Li", category="user_info")
        results = mm.recall("Li")

        assert len(results) >= 1
        assert any("Li" in f.get("content", "") for f in results)

    def test_learn(self):
        """learn 添加学习规律。"""
        mm = MemoryManager()

        mm.learn("当用户说 Python 时，优先推荐标准库方案", source="inference")

        # 通过检索验证 — 使用关键词匹配能命中的 query
        result = mm.retrieve("Python")
        assert len(result["long_term"]["learnings"]) >= 1

    def test_log_execution(self):
        """log_execution 记录执行日志。"""
        mm = MemoryManager()

        mm.log_execution({
            "task_id": "task_001",
            "goal": "测试任务",
            "steps_completed": 3,
            "duration_sec": 1.5,
        })

        recents = mm.recent_executions(5)
        assert len(recents) >= 1
        assert recents[-1]["task_id"] == "task_001"

    def test_clear(self):
        """clear 应该清空所有记忆。"""
        mm = MemoryManager()

        mm.add_message("user", "测试")
        mm.remember("测试事实")

        mm.clear()

        recent = mm.recent_messages(100)
        assert len(recent) == 0

    def test_summarize(self):
        """summarize 返回记忆摘要。"""
        mm = MemoryManager()

        mm.add_message("user", "你好")

        summary = mm.summarize()
        assert "短时记忆" in summary


class TestShortTermMemory:
    """ShortTermMemory 单元测试。"""

    def test_add_and_retrieve(self):
        stm = _make_stm()

        stm.add("user", "测试消息 1")
        stm.add("assistant", "回复 1")

        assert len(stm.history) == 2
        recent = stm.recent(1)
        assert len(recent) == 1
        assert recent[0]["role"] == "assistant"

    def test_to_openai_messages(self):
        stm = _make_stm()

        stm.add("user", "你好")
        messages = stm.to_openai_messages(system_prompt="你是一个助手")

        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "你是一个助手"
        assert messages[1]["role"] == "user"

    def test_to_context_string_truncation(self):
        stm = _make_stm()

        long_msg = "x" * 500
        stm.add("user", long_msg)

        ctx = stm.to_context_string(max_chars=100)
        assert len(ctx) <= 100

    def test_estimated_tokens(self):
        stm = _make_stm()

        stm.add("user", "Hello world")
        tokens = stm.estimated_tokens

        assert tokens > 0

    def test_needs_compression_false_initially(self):
        stm = _make_stm()

        assert stm.needs_compression is False

    def test_clear(self):
        stm = _make_stm()
        stm.add("user", "消息")
        stm.clear()

        assert len(stm) == 0


class TestMemoryRetriever:
    """MemoryRetriever 测试。"""

    def test_format_conversation_history(self):
        stm = _make_stm()
        ltm = LongTermMemory()
        retriever = MemoryRetriever(stm, ltm)

        stm.add("user", "消息 1")
        stm.add("assistant", "回复 1")
        stm.add("user", "消息 2")

        history = retriever._format_conversation_history(max_messages=5, max_chars=4000)

        assert "[user] 消息 1" in history
        assert "[assistant] 回复 1" in history

    def test_format_conversation_history_char_limit(self):
        """字符超限时从旧端丢弃。"""
        stm = _make_stm()
        ltm = LongTermMemory()
        retriever = MemoryRetriever(stm, ltm)

        # 添加多条消息使其超过字符限制 (small max_chars forces truncation)
        for i in range(15):
            stm.add("user", f"消息编号_{i}_" + "x" * 80)

        history = retriever._format_conversation_history(max_messages=20, max_chars=300)

        assert len(history) <= 400  # 允许一些 buffer
        # 最新消息应该在
        assert "编号_14" in history
        # 最旧消息应该被丢弃
        assert "编号_0" not in history

    def test_get_conversation_history(self):
        stm = _make_stm()
        ltm = LongTermMemory()
        retriever = MemoryRetriever(stm, ltm)

        stm.add("user", "你好")
        history = retriever.get_conversation_history()

        assert "[user] 你好" in history

    def test_retrieve_empty(self):
        stm = _make_stm()
        ltm = LongTermMemory()
        retriever = MemoryRetriever(stm, ltm)

        result = retriever.retrieve("查询")

        assert result["query"] == "查询"
        assert isinstance(result["short_term"], list)
        assert isinstance(result["long_term"], dict)

    def test_build_summary(self):
        stm = _make_stm()
        ltm = LongTermMemory()
        retriever = MemoryRetriever(stm, ltm)

        summary = retriever._build_summary("test query", [], [], [])

        assert "test query" in summary
