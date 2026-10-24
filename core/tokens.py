"""token 估算（纯本地确定性，只用于量级判断，不求精确）。

此前长在 chatlog（持久层）里、context（装配层）又反过来 import chatlog 取用，
两头拧着 —— 下沉到 core 后两模块都只依赖 core。
"""
from __future__ import annotations

import math

# CJK 与 CJK 标点的码点区间（token 估算用）
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x3000, 0x303F))


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """CJK 字符按 1 token、其余按 4 字符 1 token 估算。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk + math.ceil((len(text) - cjk) / 4)
