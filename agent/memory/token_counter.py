"""
Token 计数器 — 使用 tiktoken 估算消息的 token 数量。

DeepSeek 兼容 OpenAI 的 tokenizer，使用 cl100k_base 编码。
"""
from __future__ import annotations

from typing import Dict, List

try:
    import tiktoken
    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False

# DeepSeek 兼容 OpenAI tokenizer
ENCODING_NAME = "cl100k_base"


def _get_encoding():
    """获取 tiktoken 编码器（首次调用时懒加载）。"""
    if not HAS_TIKTOKEN:
        return None
    try:
        return tiktoken.get_encoding(ENCODING_NAME)
    except Exception:
        return None


def count_tokens(text: str) -> int:
    """计算单个字符串的 token 数。"""
    enc = _get_encoding()
    if enc:
        return len(enc.encode(text))
    # tiktoken 不可用时的粗略估计: ~3 字符/token (中英文混合)
    return len(text) // 3


def count_message_tokens(messages: List[Dict], model: str = "deepseek-chat") -> int:
    """
    计算消息列表的总 token 数（按 OpenAI 消息格式估算）。

    每条消息: 4 token 开销 (role + content 边界标记) + 内容 token。
    """
    enc = _get_encoding()
    total = 0
    for msg in messages:
        total += 4  # 消息边界标记
        for value in msg.values():
            total += len(enc.encode(str(value))) if enc else len(str(value)) // 3
    total += 2  # 回复引导
    return total
