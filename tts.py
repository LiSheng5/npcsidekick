"""语音系统（edge-tts，免费微软 TTS）—— **输出通道**，与"说/做"分离。

设计（设计.md §5）：
  · `/api/talk` 带 `voice=true` → 台词流结束后追加一个 `audio` 帧（base64 mp3）
  · `/api/tts` 独立端点 → 给任意文本配音（本地对话表 / 头顶气泡也能用）
  · 无 edge-tts / 无网 / 超时 → 不出 `audio` 帧，纯文本保底（语音=锦上添花）

音色数据驱动（设计.md §7）：`persona["voice"]` 覆盖，缺省用 `DEFAULT_VOICE`。
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
from typing import Optional

# 全局默认音色（persona["voice"] 可覆盖）
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# 内存缓存：相同文本 + 音色不重复合成（常见台词/兜底话术复用）
_cache: dict[str, bytes] = {}
_CACHE_LIMIT = 512


def available() -> bool:
    """edge-tts 是否可导入（未安装 → 语音降级，纯文本保底）。"""
    return importlib.util.find_spec("edge_tts") is not None


def voice_for(persona: Optional[dict]) -> str:
    """取角色音色：persona["voice"] > 全局默认。"""
    if isinstance(persona, dict):
        voice = persona.get("voice")
        if isinstance(voice, str) and voice.strip():
            return voice.strip()
    return DEFAULT_VOICE


def to_base64(audio: bytes) -> str:
    return base64.b64encode(audio).decode("ascii")


def _cache_key(text: str, voice: str) -> str:
    return hashlib.md5(f"{voice}|{text}".encode("utf-8")).hexdigest()


async def synthesize(text: str, voice: str) -> Optional[bytes]:
    """合成 mp3 音频字节。失败/超时/空文本 → None（调用方回退纯文本）。"""
    if not text.strip():
        return None
    key = _cache_key(text, voice)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice)
        data = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                data.extend(chunk.get("data", b""))
        result = bytes(data)
        if not result:
            return None
        if len(_cache) >= _CACHE_LIMIT:
            _cache.clear()
        _cache[key] = result
        return result
    except Exception:
        return None
