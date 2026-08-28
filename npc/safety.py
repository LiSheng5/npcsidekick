"""
NPCSidekick — 入站安检门 + 安全宪法（§22 · 2026-08-25）。

设计稿: docs/安全审查重构_方案稿.md ｜ 业界依据: docs/同类项目调研_AI-NPC.md 第四部分
（开源圈无逐句 LLM 自审先例；本模块 = 商业双轨派 Inworld Safety Module 的本地丐版）。

组成:
  - 两级词表: L1 硬拦(占位替换+提示模型婉拒) / L2 软旗(只提示 B1 转话题，不拦截)
    词表放独立可编辑 txt（safety_words_L1/L2.txt），一行一条，支持 "词条|类别" 与 # 注释。
  - 安全宪法: 项目根 safety_constitution.md，GATE 开启时注入每次对话 system 段。
  - 归一化匹配: lower + 去标点去空白（防"色 情"式绕过）；预编译正则，毫秒级。

2026-08-27 重构（用户拍板）:
  - L1 罐头拒绝话术退役（_REFUSALS/refusal() 移除）——命中后走 LLM 婉拒:
    模型自对齐 + 宪法 + hard_hint 已够用；人设口吻婉拒更自然, 玩家不再是"被消失"。
  - 词表清瘴: L1 删组织/宗教词（圣战/ISIS 等有正常语境）；L2 删游戏语境词
    （战争/喝酒/手枪等场景内正常台词不打扰），只留擦边类真雷点。

铁律:
  - 默认 OFF: NPC_SAFETY_GATE 未设为 "1" 时一切直通，行为与旧版完全一致。
  - 违规原文永不落盘: 本模块只返回 verdict；日志由调用方记类别+哈希，不记原文。
  - 绝不抛异常反噬主流程: 词表缺失/损坏 → 记 warning 后按"干净"放行（安检门不能卡死游戏）。
"""
from __future__ import annotations

import os
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

@dataclass(frozen=True)
class Verdict:
    """安检结论: level ∈ ""(干净/未启用) | "L1"(硬拦) | "L2"(软旗)。"""
    level: str = ""
    category: str = ""
    refusal: str = ""


_norm_re = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_cache: dict = {}
_constitution_cache = None


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


def scan(text: str) -> Verdict:
    """入站安检。未启用或干净 → Verdict("")。L1 命中只报类别（婉拒交给模型）。"""
    try:
        if not enabled():
            return Verdict()
        norm = _normalize(text)
        if not norm:
            return Verdict()
        for pat, cat in _patterns("safety_words_L1.txt"):
            if pat.search(norm):
                return Verdict("L1", cat or "未分类")
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


def hard_hint(category: str) -> str:
    """L1 硬拦提示（2026-08-27）：拼进 system 段，让 B1 以人设口吻婉拒。

    替代旧罐头拒绝：模型自对齐 + 宪法红线 + 本提示三层保证婉拒而不展开；
    玩家消息已由调用方替换为占位符（三不清除），这里不要再解释安检细节。
    """
    return (f"【安检提示】玩家这条消息不合适（{category}）。以人设口吻婉拒并换个话题："
            f"不要展开、不要复述、不要追问、不要提及安全检查。")


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
