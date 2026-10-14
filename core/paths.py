"""路径注入 —— 运行时数据目录的单一来源（P-24 阶段 2）。

  · `Paths` 只收**运行时数据**目录（store / personas / avatars），
    由调用方显式注入（`server.create_app(paths=...)`），测试传 `Paths(root=tmp_path)`，
    不再 monkeypatch 模块级路径常量
  · 控制台静态资源（web/console）与 `api_key.txt` 属于**代码/工程文件**，不在此列
  · memory / chatlog / llm_runtime 的函数接受单个目录参数（store_dir），
    None = 工程默认（default_paths()）
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Paths:
    """一次注入的运行时数据目录集合（root 通常是工程根，测试里是 tmp_path）。"""

    root: Path

    @property
    def store_dir(self) -> Path:
        """记忆卡 / 聊天记录 / llm_config.json（运行时数据，gitignore）。"""
        return self.root / "store"

    @property
    def persona_dir(self) -> Path:
        """角色卡目录（加一个文件就多一个 NPC）。"""
        return self.root / "personas"

    @property
    def avatar_dir(self) -> Path:
        """关系网节点头像（avatars/{id}.<ext>，静态挂在 /avatars）。"""
        return self.root / "avatars"

    def llm_config_path(self) -> Path:
        """页面保存的 LLM 设置落盘位置（store/llm_config.json）。"""
        return self.store_dir / "llm_config.json"


def default_paths() -> Paths:
    """工程默认目录（core/ 的上一级 = 工程根）。"""
    return Paths(root=Path(__file__).resolve().parent.parent)


def resolve_store(store_dir: Optional[Path]) -> Path:
    """store_dir 为 None → 工程默认 store/（各模块磁盘函数的兜底口径）。"""
    return default_paths().store_dir if store_dir is None else Path(store_dir)
