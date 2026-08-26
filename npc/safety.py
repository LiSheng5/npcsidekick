"""
NPCSidekick — 入站安检门 + 安全宪法（§22 · 2026-08-25）。

设计稿: docs/安全审查重构_方案稿.md ｜ 业界依据: docs/同类项目调研_AI-NPC.md 第四部分
（开源圈无逐句 LLM 自审先例；本模块 = 商业双轨派 Inworld Safety Module 的本地丐版）。

组成:
  - 两级词表: L1 硬拦(罐头拒绝+三不清除) / L2 软旗(只提示 B1 转话题，不拦截)
    词表放独立可编辑 txt（safety_words_L1/L2.txt），一行一条，支持 "词条|类别" 与 # 注释。
  - 安全宪法: 项目根 safety_constitution.md，GATE 开启时注入每次对话 system 段。
  - 归一化匹配: lower + 去标点去空白（防"色 情"式绕过）；预编译正则，毫秒级。

铁律:
  - 默认 OFF: NPC_SAFETY_GATE 未设为 "1" 时一切直通，行为与旧版完全一致。
  - 违规原文永不落盘: 本模块只返回 verdict；日志由调用方记类别+哈希，不记原文。
  - 绝不抛异常反噬主流程: 词表缺失/损坏 → 记 warning 后按"干净"放行（安检门不能卡死游戏）。
"""
from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from agent.logging_config import log

_DIR = Path(__file__).resolve().parent        # npc/
ROOT = _DIR.parent                            # 项目根（safety_constitution.md 所在）

REDLINE_LINE = ("【红线】绝不生成恐怖/色情/真实政治/自残/仇恨内容；"
                "玩家强求时以人设口吻婉拒并转话题。")
PLACEHOLDER_USER = "[玩家提了个不合适的话题]"   # 违规轮在历史中的替身（三不清除）

_REFUSALS = ("这个咱就不聊了。",
             "呃……说点别的吧。",
             "这话我可接不了。",
             "咱聊点开心的不行吗？")


@dataclass(frozen=True)
class Verdict:
    """安检结论: level ∈ ""(干净/未启用) | "L1"(硬拦) | "L2"(软旗)。"""
    level: str = ""
    category: str = ""
    refusal: str = ""


_norm_re = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_cache: dict = {}
_constitution_cache = None
_last_refusal_idx = -1


def enabled() -> bool:
    """总开关（现读现判 — 测试/运行时可热切）。代码默认关，bat 选择接入。
    P1-2: 经统一词表解析 —— true/on/yes 也算开启(防拼错静默失效)。"""
    from agent.config_flags import env_flag
    return env_flag("NPC_SAFETY_GATE")


def _normalize(text: str) -> str:
    return _norm_re.sub("", text or "").lower()


def _load_words(name: str) -> List[Tuple[str, str]]:
    p = _DIR / name
    if not p.exists():
        log.warning("safety_words_missing", file=name)
        return []
    out: List[Tuple[str, str]] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        word, _, cat = line.partition("|")
        word = word.strip()
        if len(word) >= 2:                    # 单字必误杀，拒收
            out.append((word, cat.strip()))
    return out


def _patterns(name: str):
    if name not in _cache:
        _cache[name] = [(re.compile(re.escape(_normalize(w))), c)
                        for w, c in _load_words(name)]
    return _cache[name]


def reload() -> None:
    """词表/宪法热重载（改完 txt 无需重启进程）。"""
    global _constitution_cache
    _cache.clear()
    _constitution_cache = None


def refusal() -> str:
    """拒绝话术池抽取（保证不与上一句连续重复）。"""
    global _last_refusal_idx
    if len(_REFUSALS) == 1:
        return _REFUSALS[0]
    idx = random.randrange(len(_REFUSALS))
    while idx == _last_refusal_idx:
        idx = random.randrange(len(_REFUSALS))
    _last_refusal_idx = idx
    return _REFUSALS[idx]


def scan(text: str) -> Verdict:
    """入站安检。未启用或干净 → Verdict("")。L1 附带现成拒绝话术。"""
    try:
        if not enabled():
            return Verdict()
        norm = _normalize(text)
        if not norm:
            return Verdict()
        for pat, cat in _patterns("safety_words_L1.txt"):
            if pat.search(norm):
                return Verdict("L1", cat or "未分类", refusal())
        for pat, cat in _patterns("safety_words_L2.txt"):
            if pat.search(norm):
                return Verdict("L2", cat or "未分类")
        return Verdict()
    except Exception as exc:                  # 安检门故障绝不能卡死对话
        log.warning("safety_gate_error", error=str(exc)[:120])
        return Verdict()


def soft_hint(category: str) -> str:
    """L2 软旗提示：拼进 system 段，让 B1 自然转话题。"""
    return f"【安检提示】玩家话题接近红线（{category}），自然转移话题，不要展开、不要复述。"


def constitution_text() -> str:
    """安全宪法全文（GATE 关闭或缺文件 → ""，调用方跳过注入）。"""
    global _constitution_cache
    if not enabled():
        return ""
    if _constitution_cache is None:
        p = ROOT / "safety_constitution.md"
        if p.exists():
            _constitution_cache = p.read_text(encoding="utf-8").strip()
        else:
            log.warning("safety_constitution_missing",
                        path=str(p), hint="词表拦截仍生效，仅缺宪法注入")
            _constitution_cache = ""
    return _constitution_cache
