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
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from pathlib import Path

import config

if TYPE_CHECKING:
    from agent.memory.vector_store import VectorStore

# 入口截流 (Codex middle-truncation 对照, 2026-08):
# 单条超长消息(如工具输出)在进入历史时就保留头尾、裁掉中间
ENTRY_HEAD_CHARS = 500
ENTRY_TAIL_CHARS = 200


class ShortTermMemory:
    """
    短时记忆 — 滑动窗口式的对话上下文。

    使用方式:
      stm = ShortTermMemory()
      stm.add("user", "现在几点?")
      stm.add("assistant", "当前时间是...")
      recent = stm.recent(10)  # 最后 10 条消息
    """

    def __init__(self, file_path: Optional[Path] = None,
                 vector_store: Optional["VectorStore"] = None):
        self.file_path = file_path or config.SHORT_TERM_FILE
        self.history: List[Dict[str, str]] = []
        self.working_context: Dict[str, Any] = {}  # 当前任务的中间状态
        self._vector_store = vector_store
        self._load()

    # ── 读写 ──────────────────────────────────────────

    def add(self, role: str, content: str) -> None:
        """添加一条消息到历史（入口截流: 超长内容保留头尾）。"""
        content = _entry_truncate(content)
        self.history.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        })
        self._trim()
        # 写入向量索引
        idx = len(self.history) - 1
        self._add_to_vector(idx, role, content)

    def add_batch(self, messages: List[Dict[str, str]]) -> None:
        """批量添加消息。"""
        now = datetime.now().isoformat()
        for msg in messages:
            content = _entry_truncate(msg.get("content", ""))
            self.history.append({
                "role": msg.get("role", "system"),
                "content": content,
                "timestamp": now,
            })
        self._trim()
        # 批量写入向量索引
        if self._vector_store:
            items = []
            for i in range(max(0, len(self.history) - len(messages)), len(self.history)):
                msg = self.history[i]
                items.append((
                    f"msg_{i}",
                    msg["content"],
                    {"source": "message", "role": msg["role"], "index": str(i)},
                ))
            self._vector_store.add_batch(items)

    def recent(self, n: int = 20) -> List[Dict[str, str]]:
        """获取最后 n 条消息。"""
        return self.history[-n:]

    def clear(self) -> None:
        """清空短时记忆。"""
        self.history.clear()
        self.working_context.clear()
        self._save()
        if self._vector_store:
            self._vector_store.delete_by_prefix("msg_")

    def trim_oldest(self, count: int) -> List[Dict[str, str]]:
        """移除最旧的 count 条消息，返回被移除的消息列表。
        同时从向量索引中删除。"""
        count = min(count, len(self.history))
        removed = self.history[:count]
        self.history = self.history[count:]
        self._save()
        # 重建消息的向量索引 (因为索引变了)
        self._rebuild_message_vectors()
        return removed

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
            removed_count = len(self.history) - config.MAX_HISTORY_ITEMS
            self.history = self.history[-config.MAX_HISTORY_ITEMS:]
            self._save()
            # 向量索引重建 (索引已变)
            if self._vector_store:
                self._rebuild_message_vectors()

    # ── Token / 压缩 ──────────────────────────────────

    @property
    def estimated_tokens(self) -> int:
        """估算当前历史消息的总 token 数。"""
        try:
            from agent.memory.token_counter import count_message_tokens
            return count_message_tokens(self.history)
        except ImportError:
            return len(str(self.history)) // 3  # 粗略估计: ~3 字符/token

    @property
    def needs_compression(self) -> bool:
        """检查是否需要压缩。"""
        return (
            len(self.history) >= config.COMPRESSION_MIN_MESSAGES
            and self.estimated_tokens > config.COMPRESSION_TOKEN_THRESHOLD
        )

    # ── 向量索引 ──────────────────────────────────────

    def rebuild_index(self) -> None:
        """从已有 JSON 数据重建完整的向量索引（首次迁移时调用）。"""
        if not self._vector_store:
            return
        self._rebuild_message_vectors()

    def _rebuild_message_vectors(self) -> None:
        """重建所有消息的向量索引。"""
        if not self._vector_store:
            return
        self._vector_store.delete_by_prefix("msg_")
        items = []
        for i, msg in enumerate(self.history):
            content = msg.get("content", "")
            if content.strip():
                items.append((
                    f"msg_{i}",
                    content,
                    {"source": "message", "role": msg.get("role", ""), "index": str(i)},
                ))
        if items:
            self._vector_store.add_batch(items)

    def _add_to_vector(self, index: int, role: str, content: str) -> None:
        """添加单条消息到向量存储。"""
        if not self._vector_store or not content.strip():
            return
        self._vector_store.add(
            f"msg_{index}",
            content,
            {"source": "message", "role": role, "index": str(index)},
        )

    def __len__(self) -> int:
        return len(self.history)


def _entry_truncate(content: str) -> str:
    """入口截流: 单条超长内容保留头尾、裁掉中间 (避免工具输出吃满窗口)。"""
    if len(content) <= ENTRY_HEAD_CHARS + ENTRY_TAIL_CHARS + 3:
        return content
    return content[:ENTRY_HEAD_CHARS] + "..." + content[-ENTRY_TAIL_CHARS:]
