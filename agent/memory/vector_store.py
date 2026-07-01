"""
向量存储 — ChromaDB 封装，提供语义相似度搜索。

替代原有的关键词子字符串匹配，让 "几点" 能匹配 "时间" 相关记忆。
使用 ChromaDB 内置 ONNX 嵌入模型 (all-MiniLM-L6-v2)，无需 PyTorch。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import config

# ChromaDB 是可选依赖 — 未安装时降级为关键词匹配
try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    HAS_CHROMADB = True
except ImportError:
    HAS_CHROMADB = False


@dataclass
class VectorResult:
    """向量搜索结果。"""
    doc_id: str
    text: str
    metadata: dict
    score: float


class VectorStore:
    """ChromaDB 向量存储封装。

    统一 collection "agent_memory"，用 metadata["source"] 区分类型:
      - "message"  — 对话消息
      - "fact"     — 长期事实
      - "learning" — 学到的规律

    如果 chromadb 未安装，所有操作降级为空操作。
    """

    def __init__(self, persist_dir: Optional[Path] = None):
        persist_dir = persist_dir or config.VECTOR_STORE_DIR
        self._persist_dir = Path(persist_dir)
        self._available = HAS_CHROMADB
        self._client = None
        self._collection = None

        if not HAS_CHROMADB:
            print("[!] chromadb 未安装，向量检索不可用。使用 'pip install chromadb' 安装。")
            return

        try:
            # Suppress ChromaDB ONNX download logs
            import logging
            logging.getLogger("chromadb").setLevel(logging.WARNING)
            logging.getLogger("chromadb.telemetry").setLevel(logging.ERROR)

            self._persist_dir.mkdir(parents=True, exist_ok=True)

            self._client = chromadb.PersistentClient(
                path=str(self._persist_dir),
                settings=ChromaSettings(anonymized_telemetry=False),
            )

            self._collection = self._client.get_or_create_collection(
                name="agent_memory",
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            print(f"[!] ChromaDB 初始化失败，降级为关键词检索: {e}")
            self._available = False

    # ── Query ──────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._available and self._collection is not None

    def count(self) -> int:
        """返回已索引文档数。"""
        if not self.available:
            return 0
        return self._collection.count()

    # ── Write ───────────────────────────────────────────

    def add(self, doc_id: str, text: str, metadata: dict) -> None:
        """添加或更新单个文档。"""
        if not self.available:
            return
        if not text or not text.strip():
            return
        metadata = {k: str(v)[:200] for k, v in metadata.items()}
        try:
            self._collection.upsert(
                ids=[doc_id],
                documents=[text],
                metadatas=[metadata],
            )
        except Exception:
            pass  # 静默失败 — 向量索引不应中断主流程

    def add_batch(self, items: list[tuple[str, str, dict]]) -> None:
        """批量添加文档。每项: (doc_id, text, metadata)。"""
        if not self.available or not items:
            return
        ids, docs, metas = [], [], []
        for doc_id, text, meta in items:
            if not text or not text.strip():
                continue
            ids.append(doc_id)
            docs.append(text)
            metas.append({k: str(v)[:200] for k, v in meta.items()})
        if ids:
            try:
                self._collection.upsert(ids=ids, documents=docs, metadatas=metas)
            except Exception:
                pass

    # ── Search ──────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        threshold: Optional[float] = None,
    ) -> list[VectorResult]:
        """语义搜索，返回按相似度排序的结果。"""
        if not self.available or not query.strip():
            return []

        threshold = threshold if threshold is not None else config.VECTOR_SIMILARITY_THRESHOLD
        top_k = top_k or config.VECTOR_SEARCH_TOP_K

        try:
            raw = self._collection.query(
                query_texts=[query],
                n_results=min(top_k, self._collection.count()),
            )
        except Exception:
            return []

        results = []
        if not raw["ids"] or not raw["ids"][0]:
            return results

        for i, doc_id in enumerate(raw["ids"][0]):
            doc = raw["documents"][0][i] if raw.get("documents") and raw["documents"][0] else ""
            meta = raw["metadatas"][0][i] if raw.get("metadatas") and raw["metadatas"][0] else {}
            distance = raw["distances"][0][i] if raw.get("distances") and raw["distances"][0] else 1.0
            score = 1.0 - distance  # cosine distance → similarity

            if score >= threshold:
                results.append(VectorResult(
                    doc_id=doc_id,
                    text=doc,
                    metadata=meta,
                    score=score,
                ))

        return results

    # ── Delete ──────────────────────────────────────────

    def delete(self, doc_id: str) -> None:
        """删除单个文档。"""
        if not self.available:
            return
        try:
            self._collection.delete(ids=[doc_id])
        except Exception:
            pass

    def delete_by_prefix(self, prefix: str) -> None:
        """删除所有 id 以 prefix 开头的文档。"""
        if not self.available:
            return
        try:
            all_ids = self._collection.get()["ids"]
            to_delete = [i for i in all_ids if i.startswith(prefix)]
            if to_delete:
                self._collection.delete(ids=to_delete)
        except Exception:
            pass

    def reset(self) -> None:
        """清空并重建 collection。"""
        if not self.available:
            return
        try:
            self._client.delete_collection("agent_memory")
            self._collection = self._client.get_or_create_collection(
                name="agent_memory",
                metadata={"hnsw:space": "cosine"},
            )
        except Exception:
            pass
