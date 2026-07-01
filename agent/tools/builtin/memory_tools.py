"""
记忆工具 — 笔记 / 事实 / 搜索记忆
"""
import json
from datetime import datetime
from typing import Any

from agent.tools.schema import ToolProtocol, ToolSchema, ToolCall, ToolResult, ToolResultStatus
import config


class SaveNoteTool(ToolProtocol):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="save_note",
            description="将一条信息保存到本地笔记文件中。",
            parameters={"properties": {"note": {"type": "string", "description": "要保存的内容"}}, "required": ["note"]},
            category="memory", tags=["note", "save"],
            is_readonly=False,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        note = call.input.get("note", "")
        if not note:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error="笔记内容为空")
        with open(config.NOTES_FILE, "a", encoding="utf-8") as f:
            f.write(note + "\n")
        return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.SUCCESS, data={"saved_to": str(config.NOTES_FILE)})


class ListNotesTool(ToolProtocol):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_notes",
            description="查看所有已保存的笔记。",
            parameters={"type": "object", "properties": {}, "required": []},
            category="memory", tags=["note", "list"],
            is_readonly=True,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        if not config.NOTES_FILE.exists():
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.SUCCESS, data={"notes": []})
        with open(config.NOTES_FILE, "r", encoding="utf-8") as f:
            notes = [line.rstrip() for line in f if line.strip()]
        return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.SUCCESS, data={"notes": notes, "count": len(notes)})


class RememberFactTool(ToolProtocol):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="remember_fact",
            description="将重要事实写入长期记忆。",
            parameters={"properties": {"fact": {"type": "string", "description": "要记住的事实"}}, "required": ["fact"]},
            category="memory", tags=["memory", "remember", "long-term"],
            is_readonly=False,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        fact = call.input.get("fact", "")
        if not fact:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error="缺少事实内容")

        # 写入长期记忆文件
        memory_data = {}
        if config.LONG_TERM_FILE.exists():
            try:
                memory_data = json.loads(config.LONG_TERM_FILE.read_text(encoding="utf-8"))
            except Exception:
                memory_data = {}
        memory_data.setdefault("facts", []).append({
            "fact": fact,
            "timestamp": datetime.now().isoformat(),
        })
        if len(memory_data["facts"]) > config.MAX_LONG_TERM_ITEMS:
            memory_data["facts"] = memory_data["facts"][-config.MAX_LONG_TERM_ITEMS:]
        config.LONG_TERM_FILE.write_text(json.dumps(memory_data, ensure_ascii=False, indent=2), encoding="utf-8")

        return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.SUCCESS, data={"saved_fact": fact})


class SearchMemoryTool(ToolProtocol):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="search_memory",
            description="搜索长期记忆或对话历史中的相关内容。",
            parameters={"properties": {"query": {"type": "string", "description": "搜索关键词"}}, "required": ["query"]},
            category="memory", tags=["memory", "search", "retrieve"],
            is_readonly=True,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        query = call.input.get("query", "").lower()
        if not query:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error="缺少搜索关键词")

        results = {"facts": [], "history": []}

        # 搜索长期记忆
        if config.LONG_TERM_FILE.exists():
            try:
                data = json.loads(config.LONG_TERM_FILE.read_text(encoding="utf-8"))
                for f in data.get("facts", []):
                    if query in f.get("fact", "").lower():
                        results["facts"].append(f)
            except Exception:
                pass

        # 搜索短时记忆
        if config.SHORT_TERM_FILE.exists():
            try:
                data = json.loads(config.SHORT_TERM_FILE.read_text(encoding="utf-8"))
                for h in data.get("history", []):
                    if query in str(h.get("content", "")).lower():
                        results["history"].append(h)
            except Exception:
                pass

        return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.SUCCESS, data={"matches": results})


class SummarizeContextTool(ToolProtocol):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="summarize_context",
            description="根据当前记忆和历史生成简短摘要。",
            parameters={"properties": {"query": {"type": "string", "description": "可选的关注点关键词"}}, "required": []},
            category="memory", tags=["memory", "summarize"],
            is_readonly=True,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        query = call.input.get("query", "")
        facts_list = []
        history_list = []

        if config.LONG_TERM_FILE.exists():
            try:
                data = json.loads(config.LONG_TERM_FILE.read_text(encoding="utf-8"))
                facts_list = [f.get("fact", "") for f in data.get("facts", [])]
            except Exception:
                pass

        if config.SHORT_TERM_FILE.exists():
            try:
                data = json.loads(config.SHORT_TERM_FILE.read_text(encoding="utf-8"))
                history_list = [h.get("content", "") for h in data.get("history", [])[-12:]]
            except Exception:
                pass

        if query:
            ql = query.lower()
            facts_list = [f for f in facts_list if ql in f.lower()]
            history_list = [h for h in history_list if ql in h.lower()]

        return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.SUCCESS, data={"summary": {"facts": facts_list[-10:], "recent_history": history_list[-8:], "query": query}})
