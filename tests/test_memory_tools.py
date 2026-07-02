"""
Tests for agent.tools.builtin.memory_tools — 5 memory tools.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.tools.schema import ToolCall, ToolResultStatus


# ═══════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════

@pytest.fixture
def notes_file(tmp_path):
    return tmp_path / "notes.txt"


@pytest.fixture
def long_term_file(tmp_path):
    return tmp_path / "long_term.json"


@pytest.fixture
def short_term_file(tmp_path):
    return tmp_path / "short_term.json"


def _patch_config(notes_file, long_term_file, short_term_file, max_items=100):
    """Patch config module attributes for isolated testing."""
    return patch.multiple(
        'config',
        NOTES_FILE=notes_file,
        LONG_TERM_FILE=long_term_file,
        SHORT_TERM_FILE=short_term_file,
        MAX_LONG_TERM_ITEMS=max_items,
    )


# ═══════════════════════════════════════════════════════
# SaveNoteTool
# ═══════════════════════════════════════════════════════

class TestSaveNoteTool:
    def test_save_note_writes_to_file(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SaveNoteTool
        tool = SaveNoteTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="save_note", input={"note": "Hello World"}))

        assert result.status == ToolResultStatus.SUCCESS
        assert notes_file.exists()
        content = notes_file.read_text(encoding="utf-8")
        assert "Hello World" in content

    def test_save_note_empty_content(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SaveNoteTool
        tool = SaveNoteTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="save_note", input={"note": ""}))

        assert result.status == ToolResultStatus.ERROR
        assert "空" in result.error

    def test_save_note_missing_key(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SaveNoteTool
        tool = SaveNoteTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="save_note", input={}))

        assert result.status == ToolResultStatus.ERROR

    def test_save_note_appends_multiple(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SaveNoteTool
        tool = SaveNoteTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            tool.execute(ToolCall(tool="save_note", input={"note": "Note 1"}))
            tool.execute(ToolCall(tool="save_note", input={"note": "Note 2"}))

        lines = notes_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert lines[0] == "Note 1"
        assert lines[1] == "Note 2"


# ═══════════════════════════════════════════════════════
# ListNotesTool
# ═══════════════════════════════════════════════════════

class TestListNotesTool:
    def test_list_notes_empty_when_file_missing(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import ListNotesTool
        tool = ListNotesTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="list_notes"))

        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["notes"] == []

    def test_list_notes_returns_all(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import ListNotesTool
        tool = ListNotesTool()
        notes_file.write_text("Note A\nNote B\nNote C\n", encoding="utf-8")

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="list_notes"))

        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["notes"] == ["Note A", "Note B", "Note C"]
        assert result.data["count"] == 3

    def test_list_notes_filters_blank_lines(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import ListNotesTool
        tool = ListNotesTool()
        notes_file.write_text("Note A\n\n  \nNote B\n", encoding="utf-8")

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="list_notes"))

        assert result.data["notes"] == ["Note A", "Note B"]


# ═══════════════════════════════════════════════════════
# RememberFactTool
# ═══════════════════════════════════════════════════════

class TestRememberFactTool:
    def test_remember_fact_creates_file(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import RememberFactTool
        tool = RememberFactTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="remember_fact", input={"fact": "用户名叫 Li"}))

        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["saved_fact"] == "用户名叫 Li"
        assert long_term_file.exists()
        data = json.loads(long_term_file.read_text(encoding="utf-8"))
        assert len(data["facts"]) == 1
        assert data["facts"][0]["fact"] == "用户名叫 Li"
        assert "timestamp" in data["facts"][0]

    def test_remember_fact_empty(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import RememberFactTool
        tool = RememberFactTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="remember_fact", input={"fact": ""}))

        assert result.status == ToolResultStatus.ERROR
        assert "缺少" in result.error

    def test_remember_fact_multiple(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import RememberFactTool
        tool = RememberFactTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            tool.execute(ToolCall(tool="remember_fact", input={"fact": "事实1"}))
            tool.execute(ToolCall(tool="remember_fact", input={"fact": "事实2"}))

        data = json.loads(long_term_file.read_text(encoding="utf-8"))
        assert len(data["facts"]) == 2

    def test_remember_fact_truncates_oldest(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import RememberFactTool
        tool = RememberFactTool()

        with _patch_config(notes_file, long_term_file, short_term_file, max_items=3):
            for i in range(5):
                tool.execute(ToolCall(tool="remember_fact", input={"fact": f"事实{i}"}))

        data = json.loads(long_term_file.read_text(encoding="utf-8"))
        assert len(data["facts"]) == 3
        # Should keep last 3 (fact2, fact3, fact4)
        facts = [f["fact"] for f in data["facts"]]
        assert facts == ["事实2", "事实3", "事实4"]

    def test_remember_fact_corrupted_json_resets(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import RememberFactTool
        tool = RememberFactTool()
        long_term_file.write_text("not valid json {{{", encoding="utf-8")

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="remember_fact", input={"fact": "新事实"}))

        assert result.status == ToolResultStatus.SUCCESS
        data = json.loads(long_term_file.read_text(encoding="utf-8"))
        assert len(data["facts"]) == 1
        assert data["facts"][0]["fact"] == "新事实"


# ═══════════════════════════════════════════════════════
# SearchMemoryTool
# ═══════════════════════════════════════════════════════

class TestSearchMemoryTool:
    def test_search_empty_query(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SearchMemoryTool
        tool = SearchMemoryTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="search_memory", input={"query": ""}))

        assert result.status == ToolResultStatus.ERROR
        assert "缺少" in result.error or "关键词" in result.error

    def test_search_no_files(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SearchMemoryTool
        tool = SearchMemoryTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="search_memory", input={"query": "Python"}))

        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["matches"]["facts"] == []
        assert result.data["matches"]["history"] == []

    def test_search_finds_fact(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SearchMemoryTool
        tool = SearchMemoryTool()
        long_term_file.write_text(json.dumps({"facts": [
            {"fact": "用户喜欢 Python", "timestamp": "2024-01-01T00:00:00"},
            {"fact": "用户住在北京", "timestamp": "2024-01-02T00:00:00"},
        ]}), encoding="utf-8")

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="search_memory", input={"query": "python"}))

        assert len(result.data["matches"]["facts"]) == 1
        assert result.data["matches"]["facts"][0]["fact"] == "用户喜欢 Python"

    def test_search_case_insensitive(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SearchMemoryTool
        tool = SearchMemoryTool()
        long_term_file.write_text(json.dumps({"facts": [
            {"fact": "PYTHON is great"},
        ]}), encoding="utf-8")

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="search_memory", input={"query": "python"}))

        assert len(result.data["matches"]["facts"]) == 1

    def test_search_corrupted_file_silent(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SearchMemoryTool
        tool = SearchMemoryTool()
        long_term_file.write_text("{{{bad json", encoding="utf-8")

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="search_memory", input={"query": "test"}))

        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["matches"]["facts"] == []


# ═══════════════════════════════════════════════════════
# SummarizeContextTool
# ═══════════════════════════════════════════════════════

class TestSummarizeContextTool:
    def test_summarize_no_files(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SummarizeContextTool
        tool = SummarizeContextTool()

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="summarize_context"))

        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["summary"]["facts"] == []
        assert result.data["summary"]["recent_history"] == []

    def test_summarize_with_facts(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SummarizeContextTool
        tool = SummarizeContextTool()
        long_term_file.write_text(json.dumps({"facts": [
            {"fact": "事实A"}, {"fact": "事实B"}, {"fact": "事实C"},
        ]}), encoding="utf-8")

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="summarize_context"))

        assert result.data["summary"]["facts"] == ["事实A", "事实B", "事实C"]

    def test_summarize_with_query_filter(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SummarizeContextTool
        tool = SummarizeContextTool()
        long_term_file.write_text(json.dumps({"facts": [
            {"fact": "Python相关"},
            {"fact": "Java相关"},
            {"fact": "Python进阶"},
        ]}), encoding="utf-8")

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="summarize_context", input={"query": "python"}))

        facts = result.data["summary"]["facts"]
        assert len(facts) == 2  # Both contain "python" (case-insensitive)

    def test_summarize_truncates_to_limits(self, notes_file, long_term_file, short_term_file):
        from agent.tools.builtin.memory_tools import SummarizeContextTool
        tool = SummarizeContextTool()
        # Create 15 facts
        facts = [{"fact": f"事实{i}"} for i in range(15)]
        long_term_file.write_text(json.dumps({"facts": facts}), encoding="utf-8")

        with _patch_config(notes_file, long_term_file, short_term_file):
            result = tool.execute(ToolCall(tool="summarize_context"))

        # Should return last 10
        returned = result.data["summary"]["facts"]
        assert len(returned) == 10
        assert returned[0] == "事实5"
        assert returned[-1] == "事实14"
