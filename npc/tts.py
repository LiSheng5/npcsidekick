"""
NPCSidekick — 语音系统（edge-tts，免费微软 TTS）。

设计: 语音是"输出通道"，与"说/做"分离 — 落账口/B2/A 完全不受影响。
  - /api/talk 带 voice=true → 返回 {reply, audio(base64 mp3)}（游戏一次拿文本+语音）
  - /api/tts 独立端点 → 游戏可给任意文本(含本地对话表/头顶气泡)配音
  - 无 edge-tts / 无网 / 超时 → 返回无 audio，游戏回退纯文本（语音=锦上添花，文本=保底）

角色音色（persona JSON 加 voice 字段可覆盖，数据驱动）:
  cang → zh-CN-YunjianNeural（沉稳男声）;  ali → zh-CN-XiaoxiaoNeural（清亮女声）
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
from typing import Optional

# 角色默认音色表（persona["voice"] 可覆盖）
DEFAULT_VOICES = {
    "cang": "zh-CN-YunjianNeural",      # 苍: 沉稳男声
    "ali": "zh-CN-XiaoxiaoNeural",      # 阿黎: 清亮女声
}
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# 内存缓存: 相同文本+音色不重复合成（常见台词/兜底话术复用）
_cache: dict[str, str] = {}
_CACHE_LIMIT = 512


def available() -> bool:
    """edge-tts 是否可导入（未安装 → 语音降级，纯文本保底）。"""
    return importlib.util.find_spec("edge_tts") is not None


def voice_for(persona: dict, npc_id: str) -> str:
    """取角色音色: persona["voice"] > 默认表 > 全局默认。"""
    return persona.get("voice") or DEFAULT_VOICES.get(npc_id, DEFAULT_VOICE)


def to_base64(audio: bytes) -> str:
    return base64.b64encode(audio).decode("ascii")


def _cache_key(text: str, voice: str) -> str:
    return hashlib.md5(f"{voice}|{text}".encode("utf-8")).hexdigest()


async def synthesize(text: str, voice: str) -> Optional[bytes]:
    """合成 mp3 音频字节。失败/超时 → None（调用方回退纯文本）。"""
    if not text.strip():
        return None
    key = _cache_key(text, voice)
    if key in _cache:
        return _cache[key]
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
