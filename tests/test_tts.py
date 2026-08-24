"""语音系统测试 — 音色映射 / 端点形状 / 降级安全（不依赖真实 edge-tts）。"""
import base64

import pytest
from fastapi.testclient import TestClient

from npc.npc import NPC
from npc.server import create_npc_server
from npc.tts import DEFAULT_VOICE, to_base64, voice_for


class TestVoiceFor:
    """音色映射: persona["voice"] > 默认表 > 全局默认。"""

    def test_default_mapping(self):
        assert voice_for({"id": "cang"}, "cang") == "zh-CN-YunjianNeural"
        assert voice_for({"id": "ali"}, "ali") == "zh-CN-XiaoxiaoNeural"
        assert voice_for({"id": "x"}, "x") == DEFAULT_VOICE

    def test_persona_override(self):
        assert voice_for({"id": "cang", "voice": "zh-CN-XiaoyiNeural"}, "cang") == "zh-CN-XiaoyiNeural"


class TestToBase64:
    def test_roundtrip(self):
        raw = b"\x00\x01\x02abc"
        assert base64.b64decode(to_base64(raw)) == raw


@pytest.fixture
def client():
    npc = NPC(store_dir="npc/store_test")
    npc.use_llm = False
    return TestClient(create_npc_server({"cang": npc}))


class TestTalkWithVoice:
    """语音 = 按需输出通道: 默认纯文本（零回归），带 voice=true 才合成。"""

    def test_talk_default_no_audio(self, client, monkeypatch):
        import npc.server as srv
        monkeypatch.setattr(srv, "tts_available", lambda: True)   # 即便已安装，默认也不合成
        r = client.post("/api/talk", json={"message": "你好"})
        assert r.status_code == 200
        assert "audio" not in r.json()

    def test_talk_with_voice_returns_audio(self, client, monkeypatch):
        import npc.server as srv

        async def fake_synth(text, voice):
            return b"fake-mp3"

        monkeypatch.setattr(srv, "tts_available", lambda: True)
        monkeypatch.setattr(srv, "tts_synthesize", fake_synth)
        r = client.post("/api/talk", json={"message": "你好", "voice": True})
        assert r.status_code == 200
        data = r.json()
        assert data["reply"]
        assert "audio" in data
        assert base64.b64decode(data["audio"]) == b"fake-mp3"

    def test_talk_voice_failure_falls_back_text(self, client, monkeypatch):
        import npc.server as srv

        async def failing_synth(text, voice):
            raise RuntimeError("edge-tts 挂了")

        monkeypatch.setattr(srv, "tts_available", lambda: True)
        monkeypatch.setattr(srv, "tts_synthesize", failing_synth)
        r = client.post("/api/talk", json={"message": "你好", "voice": True})
        assert r.status_code == 200
        assert "audio" not in r.json()   # 语音失败 → 纯文本保底，不卡对话


class TestTtsEndpoint:
    """独立 /api/tts: 给任意文本配音。"""

    def test_tts_503_without_package(self, client, monkeypatch):
        import npc.server as srv
        monkeypatch.setattr(srv, "tts_available", lambda: False)
        r = client.post("/api/tts", json={"text": "你好"})
        assert r.status_code == 503

    def test_tts_returns_audio(self, client, monkeypatch):
        import npc.server as srv

        async def fake_synth(text, voice):
            return b"fake-mp3"

        monkeypatch.setattr(srv, "tts_available", lambda: True)
        monkeypatch.setattr(srv, "tts_synthesize", fake_synth)
        r = client.post("/api/tts", json={"text": "你好"})
        assert r.status_code == 200
        assert base64.b64decode(r.json()["audio"]) == b"fake-mp3"

    def test_tts_empty_text_400(self, client, monkeypatch):
        import npc.server as srv
        monkeypatch.setattr(srv, "tts_available", lambda: True)
        r = client.post("/api/tts", json={"text": "   "})
        assert r.status_code == 400
