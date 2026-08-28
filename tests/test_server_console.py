"""§15 调试台三件套测试 — /api/version 握手 + /api/stats 观测 + POST /api/memory 回写。"""
import pytest
from fastapi.testclient import TestClient

from npc.npc import NPC
from npc.server import BRAIN_VERSION, create_npc_server


@pytest.fixture
def client(tmp_path):
    """规则模式单 NPC（tmp 隔离记忆卡，测试确定性）。"""
    npc = NPC(store_dir=str(tmp_path))
    npc.use_llm = False
    return TestClient(create_npc_server({"cang": npc}))


@pytest.fixture
def gta_client(tmp_path):
    """声明 world_id=gta 的实例（多世界握手/守卫用）。"""
    npc = NPC(store_dir=str(tmp_path))
    npc.use_llm = False
    return TestClient(create_npc_server({"cang": npc}, world_id="gta"))


class TestVersionApi:
    def test_version_shape(self, client):
        r = client.get("/api/version")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "NPCSidekick Brain"
        assert data["version"] == BRAIN_VERSION
        f = data["features"]
        # 新功能必须在这里挂账 — 老客户端靠它降级
        assert f["events_polling"] is True
        assert f["events_sse"] is True
        assert f["memory_edit"] is True
        assert f["stats"] is True
        assert isinstance(f["jieba_tokenize"], bool)
        assert isinstance(f["tts"], bool)

    def test_version_echoes_world(self, gta_client):
        data = gta_client.get("/api/version").json()
        assert data["world_id"] == "gta"
        assert data["features"]["multi_world"] is True


class TestStatsApi:
    def test_initial_counters_zeroed(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["talk"]["total"] == 0
        assert data["talk"]["errors"] == 0
        assert data["sse_clients"] == 0
        assert data["uptime_sec"] >= 0
        assert "tick" in data and "mode" in data

    def test_talk_increments_rules_counter(self, client):
        assert client.post("/api/talk", json={"message": "你好"}).status_code == 200
        t = client.get("/api/stats").json()["talk"]
        assert t["total"] == 1
        assert t["rules"] == 1
        assert t["llm"] == 0
        assert t["last_latency_ms"] >= 0

    def test_validation_failure_not_counted_as_call(self, client):
        """400 校验失败发生在计数收口之前 — 不算一次调用（契约固化）。"""
        assert client.post("/api/talk", json={"message": ""}).status_code == 400
        t = client.get("/api/stats").json()["talk"]
        assert t["total"] == 0 and t["errors"] == 0

    def test_task_and_tts_buckets(self, client):
        client.post("/api/task", json={"resource": "木材", "count": 1})
        assert client.get("/api/stats").json()["task"]["total"] == 1


class TestMemoryWriteApi:
    def test_replace_roundtrip(self, client, tmp_path):
        r = client.post("/api/memory", json={"npc_id": "cang", "entries": [
            {"content": "测试记忆A", "importance": 7},
            {"content": "第二条", "category": "reflection"},
        ]})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "count": 2}
        entries = client.get("/api/memory?npc_id=cang").json()["entries"]
        assert len(entries) == 2
        assert entries[0]["content"] == "测试记忆A"
        assert entries[0]["importance"] == 7
        assert entries[1]["category"] == "reflection"

    def test_importance_clamped_and_persisted(self, client, tmp_path):
        client.post("/api/memory", json={"npc_id": "cang",
                                         "entries": [{"content": "超纲重要度", "importance": 99}]})
        restored = NPC.load("cang", store_dir=str(tmp_path))   # 走盘（save 生效证明）
        assert restored.memory.all()[0]["importance"] == 9

    def test_empty_content_400_no_partial_write(self, client):
        r = client.post("/api/memory", json={"npc_id": "cang", "entries": [
            {"content": "好的"}, {"content": "   "},
        ]})
        assert r.status_code == 400
        assert client.get("/api/memory?npc_id=cang").json()["entries"] == []

    def test_entries_must_be_list(self, client):
        assert client.post("/api/memory", json={"npc_id": "cang",
                                                "entries": "不是数组"}).status_code == 400

    def test_world_guard_applies(self, gta_client):
        r = gta_client.post("/api/memory", json={"npc_id": "cang", "world_id": "godot",
                                                 "entries": [{"content": "串世界的写入"}]})
        assert r.status_code == 409


class TestStaticConsole:
    """调试台页面由大脑服务器本尊托管（同源零配置）。"""

    def test_root_serves_console(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "NPCSidekick" in r.text

    def test_npc_html_is_new_console(self, client):
        r = client.get("/npc.html")
        assert r.status_code == 200
        assert "NPC关系网" in r.text       # 关系网标题（2026-08-27 替换调试台）
        assert "新建 NPC" in r.text        # 新建按钮 + /api/personas 表单
        assert "USE_MOCK" not in r.text    # mock 开关已移除
