"""
短时记忆 (Working Memory) — 当前会话的对话历史和工作上下文。

存储内容:
  - 对话历史 (user / assistant / tool 消息)
  - 当前任务计划 (TaskPlan)
  - 中间执行结果
  - 最近 N 条消息 (滑动窗口, 默认 200 条)

持久化: JSON 文件, 会话间可恢复。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from pathlib import Path

import config


class ShortTermMemory:
    """
    短时记忆 — 滑动窗口式的对话上下文。

    使用方式:
      stm = ShortTermMemory()
      stm.add("user", "现在几点?")
      stm.add("assistant", "当前时间是...")
      recent = stm.recent(10)  # 最后 10 条消息
    """

    def __init__(self, file_path: Optional[Path] = None):
        self.file_path = file_path or config.SHORT_TERM_FILE
        self.history: List[Dict[str, str]] = []
        self.working_context: Dict[str, Any] = {}  # 当前任务的中间状态
        self._load()

    # ── 读写 ──────────────────────────────────────────

    def add(self, role: str, content: str) -> None:
        """添加一条消息到历史。"""
        self.history.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        })
        self._trim()

    def add_batch(self, messages: List[Dict[str, str]]) -> None:
        """批量添加消息。"""
        now = datetime.now().isoformat()
        for msg in messages:
            self.history.append({
                "role": msg.get("role", "system"),
                "content": msg.get("content", ""),
                "timestamp": now,
            })
        self._trim()

    def recent(self, n: int = 20) -> List[Dict[str, str]]:
        """获取最后 n 条消息。"""
        return self.history[-n:]

    def clear(self) -> None:
        """清空短时记忆。"""
        self.history.clear()
        self.working_context.clear()
        self._save()

    # ── 工作上下文 ────────────────────────────────────

    def set_context(self, key: str, value: Any) -> None:
        """设置工作上下文中的键值。"""
        self.working_context[key] = value

    def get_context(self, key: str, default: Any = None) -> Any:
        return self.working_context.get(key, default)

    def clear_context(self) -> None:
        self.working_context.clear()

    # ── LLM 格式 ──────────────────────────────────────

    def to_openai_messages(self, system_prompt: Optional[str] = None) -> List[Dict[str, str]]:
        """导出为 OpenAI messages 格式。"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for item in self.history:
            messages.append({"role": item["role"], "content": item["content"]})
        return messages

    def to_context_string(self, max_chars: int = 4000) -> str:
        """生成人类可读的上下文字符串。"""
        parts = []
        for item in self.history[-20:]:
            role = item["role"]
            content = item["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            parts.append(f"[{role}] {content}")
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = "..." + text[-(max_chars - 3):]
        return text

    # ── 持久化 ────────────────────────────────────────

    def save(self) -> None:
        self._save()

    def _save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "history": self.history,
            "working_context": self.working_context,
        }
        self.file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> None:
        if self.file_path.exists():
            try:
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                self.history = data.get("history", [])
                self.working_context = data.get("working_context", {})
            except Exception:
                self.history = []
                self.working_context = {}

    def _trim(self) -> None:
        """保持历史不超过 MAX_HISTORY_ITEMS。"""
        if len(self.history) > config.MAX_HISTORY_ITEMS:
            self.history = self.history[-config.MAX_HISTORY_ITEMS:]

    def __len__(self) -> int:
        return len(self.history)
