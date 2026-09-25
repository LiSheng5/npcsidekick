"""语音系统测试 —— 音色映射 / SSE audio 帧 / 独立端点 / 降级安全（零网络）。

不依赖真实 edge-tts：`tts.available` 与 `tts.synthesize` 一律 monkeypatch。
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

import server
import tts


CAPS = {"mod": "sims4", "actions": [
    {"name": "cook", "desc": "用厨房做饭", "params": {"dish": "菜名(字符串)"}}]}


def _frames(res) -> list:
    """把 SSE 响应体解析成帧列表。"""
    out = []
    for line in res.text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def _types(res) -> list:
    return [f["type"] for f in _frames(res)]


# ── 音色映射 / 编解码 ────────────────────────────────────

def test_voice_for_persona_overrides_default():
    assert tts.voice_for({"voice": "zh-CN-YunjianNeural"}) == "zh-CN-YunjianNeural"
    assert tts.voice_for({"voice": "  zh-CN-XiaoyiNeural  "}) == "zh-CN-XiaoyiNeural"


def test_voice_for_falls_back_to_global_default():
    assert tts.voice_for({}) == tts.DEFAULT_VOICE
    assert tts.voice_for({"voice": ""}) == tts.DEFAULT_VOICE
    assert tts.voice_for({"voice": 123}) == tts.DEFAULT_VOICE
    assert tts.voice_for(None) == tts.DEFAULT_VOICE


def test_to_base64_roundtrip():
    raw = b"\x00\x01\x02abc"
    assert base64.b64decode(tts.to_base64(raw)) == raw


def test_synthesize_empty_text_returns_none():
    """空文本不合成、也不碰 edge-tts（零网络）。"""
    assert asyncio.run(tts.synthesize("   ", tts.DEFAULT_VOICE)) is None


# ── /api/talk 的 audio 帧 ────────────────────────────────

def test_talk_default_has_no_audio(tmp_store, write_persona, make_app_client,
                                   make_provider, monkeypatch):
    """默认纯文本（零回归）—— 即便 edge-tts 可用也不合成。"""
    monkeypatch.setattr(tts, "available", lambda: True)
    write_persona("cang")
    client = make_app_client(provider=make_provider(turns=[{"text": "行啊。"}]))

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "你好"})

    assert res.status_code == 200
    types = _types(res)
    assert "audio" not in types
    assert types[-1] == "done"


def test_talk_with_voice_appends_audio_frame(tmp_store, write_persona, make_app_client,
                                             make_provider, monkeypatch):
    """voice=true → done 之前多一个 audio 帧，音色取自 persona["voice"]。"""
    seen = {}

    async def fake_synth(text, voice):
        seen["text"], seen["voice"] = text, voice
        return b"fake-mp3"

    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts, "synthesize", fake_synth)
    write_persona("cang", voice="zh-CN-YunjianNeural")
    client = make_app_client(provider=make_provider(turns=[{"text": "行啊。"}]))

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "你好", "voice": True})

    frames = _frames(res)
    assert [f["type"] for f in frames][-2:] == ["audio", "done"]
    audio = frames[-2]
    assert base64.b64decode(audio["audio"]) == b"fake-mp3"
    assert audio["voice"] == "zh-CN-YunjianNeural"
    assert seen == {"text": "行啊。", "voice": "zh-CN-YunjianNeural"}


def test_talk_voice_synthesis_failure_falls_back_to_text(tmp_store, write_persona,
                                                         make_app_client, make_provider,
                                                         monkeypatch):
    """合成抛异常 → 不出 audio 帧，台词照常（绝不卡对话）。"""
    async def boom(text, voice):
        raise RuntimeError("edge-tts 挂了")

    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts, "synthesize", boom)
    write_persona("cang")
    client = make_app_client(provider=make_provider(turns=[{"text": "行啊。"}]))

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "你好", "voice": True})

    types = _types(res)
    assert "audio" not in types and "delta" in types and types[-1] == "done"


def test_talk_voice_timeout_falls_back_to_text(tmp_store, write_persona, make_app_client,
                                               make_provider, monkeypatch):
    """合成超时（TTS_TIMEOUT_SEC）→ 不出 audio 帧。"""
    async def slow(text, voice):
        await asyncio.sleep(5)
        return b"late"

    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts, "synthesize", slow)
    monkeypatch.setattr(server, "TTS_TIMEOUT_SEC", 0.05)
    write_persona("cang")
    client = make_app_client(provider=make_provider(turns=[{"text": "行啊。"}]))

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "你好", "voice": True})

    assert "audio" not in _types(res)


def test_talk_without_tts_package_skips_audio(tmp_store, write_persona, make_app_client,
                                              make_provider, monkeypatch):
    """未装 edge-tts → 请求带 voice=true 也静默降级为纯文本。"""
    monkeypatch.setattr(tts, "available", lambda: False)
    write_persona("cang")
    client = make_app_client(provider=make_provider(turns=[{"text": "行啊。"}]))

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "你好", "voice": True})

    assert "audio" not in _types(res)


# ── 独立端点 /api/tts ────────────────────────────────────

def test_tts_endpoint_503_without_package(tmp_store, persona_dir, make_app_client, monkeypatch):
    monkeypatch.setattr(tts, "available", lambda: False)
    client = make_app_client()
    assert client.post("/api/tts", json={"text": "你好"}).status_code == 503


def test_tts_endpoint_400_on_empty_text(tmp_store, persona_dir, make_app_client, monkeypatch):
    monkeypatch.setattr(tts, "available", lambda: True)
    client = make_app_client()
    assert client.post("/api/tts", json={"text": "   "}).status_code == 400
    assert client.post("/api/tts", json={}).status_code == 400


def test_tts_endpoint_uses_persona_voice(tmp_store, write_persona, make_app_client, monkeypatch):
    seen = {}

    async def fake_synth(text, voice):
        seen["voice"] = voice
        return b"fake-mp3"

    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts, "synthesize", fake_synth)
    write_persona("cang", voice="zh-CN-YunjianNeural")
    client = make_app_client()

    data = client.post("/api/tts", json={"text": "你好", "npc_id": "cang"}).json()

    assert base64.b64decode(data["audio"]) == b"fake-mp3"
    assert data["voice"] == "zh-CN-YunjianNeural"
    assert seen["voice"] == "zh-CN-YunjianNeural"


def test_tts_endpoint_explicit_voice_wins(tmp_store, write_persona, make_app_client, monkeypatch):
    seen = {}

    async def fake_synth(text, voice):
        seen["voice"] = voice
        return b"fake-mp3"

    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts, "synthesize", fake_synth)
    write_persona("cang", voice="zh-CN-YunjianNeural")
    client = make_app_client()

    data = client.post("/api/tts",
                       json={"text": "你好", "npc_id": "cang", "voice": "zh-CN-XiaoyiNeural"}).json()

    assert data["voice"] == "zh-CN-XiaoyiNeural"
    assert seen["voice"] == "zh-CN-XiaoyiNeural"


def test_tts_endpoint_unknown_npc_falls_back_to_default(tmp_store, persona_dir,
                                                        make_app_client, monkeypatch):
    seen = {}

    async def fake_synth(text, voice):
        seen["voice"] = voice
        return b"fake-mp3"

    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts, "synthesize", fake_synth)
    client = make_app_client()

    data = client.post("/api/tts", json={"text": "你好", "npc_id": "nobody"}).json()

    assert data["voice"] == tts.DEFAULT_VOICE


def test_tts_endpoint_empty_audio_when_synthesis_fails(tmp_store, persona_dir,
                                                       make_app_client, monkeypatch):
    """合成失败 → audio 为空串（不是 5xx，游戏端照常回退纯文本）。"""
    async def boom(text, voice):
        raise RuntimeError("挂了")

    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts, "synthesize", boom)
    client = make_app_client()

    res = client.post("/api/tts", json={"text": "你好"})

    assert res.status_code == 200
    assert res.json()["audio"] == ""


# ── 状态暴露 ─────────────────────────────────────────────

def test_state_exposes_tts_availability(tmp_store, persona_dir, make_app_client, monkeypatch):
    monkeypatch.setattr(tts, "available", lambda: True)
    assert make_app_client().get("/api/state").json()["tts"] is True
    monkeypatch.setattr(tts, "available", lambda: False)
    assert make_app_client().get("/api/state").json()["tts"] is False


def test_game_endpoints_unaffected_by_tts(tmp_store, write_persona, make_app_client,
                                          make_provider, monkeypatch):
    """语音不改变协议四端点：不带 voice 的 talk 帧序列与从前一致。"""
    monkeypatch.setattr(tts, "available", lambda: False)
    write_persona("cang")
    client = make_app_client(provider=make_provider(turns=[{"text": "嗯。"}]))
    client.post("/api/capabilities", json=CAPS)

    assert client.post("/api/action_result",
                       json={"npc_id": "cang", "action": "cook", "ok": True}).status_code == 200
    assert _types(client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"}))[-1] == "done"
