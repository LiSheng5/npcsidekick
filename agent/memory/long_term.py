"""
长时记忆 (Persistent Memory) — 跨会话持久化的事实、用户偏好、学到的知识。

存储内容:
  - 事实 (facts): 用户说过的重要信息
  - 执行日志 (execution_log): 过去的任务执行记录
  - 学到的规律 (learnings): 从交互中总结的模式

检索:
  - 语义搜索 (向量相似度)
  - 关键词搜索 (子串匹配, 作为回退)
  - 标签/类别检索
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Iterator, TYPE_CHECKING
from pathlib import Path

import config

if TYPE_CHECKING:
    from agent.memory.vector_store import VectorStore


class LongTermMemory:
    """
    长时记忆 — 跨会话持久化存储。

    使用方式:
      ltm = LongTermMemory()
      ltm.add_fact("用户喜欢吃辣")
      results = ltm.search("吃辣")
      ltm.add_learning("用户在周末更活跃")
    """

    def __init__(self, file_path: Optional[Path] = None,
                 vector_store: Optional["VectorStore"] = None):
        self.file_path = file_path or config.LONG_TERM_FILE
        self.facts: List[Dict[str, Any]] = []
        self.execution_log: List[Dict[str, Any]] = []
        self.learnings: List[Dict[str, Any]] = []
        self._vector_store = vector_store
        self._load()

    # ── Facts ─────────────────────────────────────────

    def add_fact(self, content: str, category: str = "general") -> str:
        """添加事实, 返回事实 ID。"""
        fact = {
            "id": f"fact_{len(self.facts) + 1}_{datetime.now().timestamp():.0f}",
            "content": content,
            "category": category,
            "created_at": datetime.now().isoformat(),
        }
        self.facts.append(fact)
        self._trim_facts()
        self._save()
        # 写入向量索引
        if self._vector_store:
            self._vector_store.add(
                f"fact_{fact['id']}",
                content,
                {"source": "fact", "category": category, "id": fact["id"]},
            )
        return fact["id"]

    def get_fact(self, fact_id: str) -> Optional[Dict]:
        for f in self.facts:
            if f["id"] == fact_id:
                return f
        return None

    def search_facts(self, query: str) -> List[Dict]:
        """关键词搜索事实。"""
        q = query.lower()
        results = [f for f in self.facts if q in f.get("content", "").lower()]
        return results[-20:]  # 返回最近 20 条

    def list_facts(self, category: Optional[str] = None) -> List[Dict]:
        """列出事实, 可按类别过滤。"""
        if category:
            return [f for f in self.facts if f.get("category") == category]
        return list(self.facts)

    def forget_fact(self, fact_id: str) -> bool:
        """删除事实。"""
        before = len(self.facts)
        self.facts = [f for f in self.facts if f["id"] != fact_id]
        if len(self.facts) < before:
            self._save()
            # 从向量索引中删除
            if self._vector_store:
                self._vector_store.delete(f"fact_{fact_id}")
            return True
        return False

    # ── Execution Log ─────────────────────────────────

    def log_execution(self, entry: Dict[str, Any]) -> None:
        """记录一次任务执行。"""
        entry["logged_at"] = datetime.now().isoformat()
        self.execution_log.append(entry)
        if len(self.execution_log) > 100:
            self.execution_log = self.execution_log[-100:]
        self._save()

    def recent_executions(self, n: int = 5) -> List[Dict]:
        return self.execution_log[-n:]

    def search_executions(self, query: str) -> List[Dict]:
        q = query.lower()
        return [e for e in self.execution_log if q in json.dumps(e, ensure_ascii=False).lower()][-20:]

    # ── Learnings ─────────────────────────────────────

    def add_learning(self, content: str, source: str = "inference") -> str:
        """记录从交互中学到的规律/模式。"""
        learning = {
            "id": f"learn_{len(self.learnings) + 1}",
            "content": content,
            "source": source,
            "created_at": datetime.now().isoformat(),
        }
        self.learnings.append(learning)
        if len(self.learnings) > 100:
            self.learnings = self.learnings[-100:]
        self._save()
        # 写入向量索引
        if self._vector_store:
            self._vector_store.add(
                f"learn_{learning['id']}",
                content,
                {"source": "learning", "id": learning["id"]},
            )
        return learning["id"]

    def search_learnings(self, query: str) -> List[Dict]:
        q = query.lower()
        return [l for l in self.learnings if q in l.get("content", "").lower()][-20:]

    # ── 全局搜索 ──────────────────────────────────────

    def search_all(self, query: str) -> Dict[str, List]:
        """在所有存储中搜索。"""
        return {
            "facts": self.search_facts(query),
            "executions": self.search_executions(query),
            "learnings": self.search_learnings(query),
        }

    # ── 摘要 ──────────────────────────────────────────

    def summarize(self) -> str:
        """生成长时记忆的可读摘要。"""
        parts = []
        if self.facts:
            parts.append(f"事实 ({len(self.facts)} 条):")
            for f in self.facts[-5:]:
                parts.append(f"  - {f['content'][:100]}")
        if self.learnings:
            parts.append(f"学到的规律 ({len(self.learnings)} 条):")
            for l in self.learnings[-3:]:
                parts.append(f"  - {l['content'][:100]}")
        return "\n".join(parts) if parts else "暂无长期记忆。"

    # ── 持久化 ────────────────────────────────────────

    def save(self) -> None:
        self._save()

    def _save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "facts": self.facts,
            "execution_log": self.execution_log,
            "learnings": self.learnings,
        }
        self.file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> None:
        if self.file_path.exists():
            try:
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                self.facts = data.get("facts", [])
                self.execution_log = data.get("execution_log", [])
                self.learnings = data.get("learnings", [])
            except Exception:
                self.facts = []
                self.execution_log = []
                self.learnings = []

    def _trim_facts(self) -> None:
        if len(self.facts) > config.MAX_LONG_TERM_ITEMS:
            removed = self.facts[:len(self.facts) - config.MAX_LONG_TERM_ITEMS]
            self.facts = self.facts[-config.MAX_LONG_TERM_ITEMS:]
            # 从向量索引中删除被裁剪的事实
            if self._vector_store:
                for f in removed:
                    self._vector_store.delete(f"fact_{f['id']}")

    # ── 向量索引 ──────────────────────────────────────

    def rebuild_index(self) -> None:
        """从已有 JSON 数据重建完整的向量索引（首次迁移时调用）。"""
        if not self._vector_store:
            return
        self._vector_store.delete_by_prefix("fact_")
        self._vector_store.delete_by_prefix("learn_")
        items = []
        for f in self.facts:
            content = f.get("content", "")
            if content.strip():
                items.append((
                    f"fact_{f['id']}",
                    content,
                    {"source": "fact", "category": f.get("category", ""), "id": f["id"]},
                ))
        for l in self.learnings:
            content = l.get("content", "")
            if content.strip():
                items.append((
                    f"learn_{l['id']}",
                    content,
                    {"source": "learning", "id": l["id"]},
                ))
        if items:
            self._vector_store.add_batch(items)

    def __len__(self) -> int:
        return len(self.facts) + len(self.learnings)
