"""上下文装配 —— 把「system + 历史 + 玩家这句话」拼成发给 LLM 的 messages。

**纯函数**：不做 IO、不调 LLM、不读环境变量（需要什么全部由调用方传进来）。
这样每一层怎么拼、占多少 token 都可以单测，不用真跑回合。

任务 2：行为与原来逐字相同（system = 人设+观测+规矩，随后历史，最后 user 这句话）；
任务 3 起在这里加分层与预算，届时才是改变行为的地方。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import chatlog
from core.personas import build_system_prompt


@dataclass
class AssembledContext:
    """装配结果：完整 messages + 各层估算 token（键名与 turn_trace 的 tokens 一致）。"""

    messages: List[Dict[str, Any]] = field(default_factory=list)
    section_tokens: Dict[str, int] = field(default_factory=dict)


def assemble(persona: Dict[str, Any], observation: Any,
             history: List[Dict[str, Any]], message: str) -> AssembledContext:
    """拼出 messages：system（人设+观测+规矩）→ 历史 → 玩家这句话。

    与 task 书 2 的验收口径：产出的 messages 必须与 stream_turn 原先那几行逐字相同。
    """
    system_content = build_system_prompt(persona, observation)
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})
    section_tokens = {
        "system": chatlog.estimate_tokens(system_content),
        "history": sum(chatlog.estimate_tokens(str(m.get("content", ""))) for m in history),
        "user": chatlog.estimate_tokens(message),
    }
    return AssembledContext(messages=messages, section_tokens=section_tokens)
