"""NPCSidekick Web 服务测试 — API 契约 + Origin 安全。"""
import pytest
from fastapi.testclient import TestClient

from npc.npc import NPC
from npc.server import create_npc_server


@pytest.fixture
def client():
    npc = NPC(store_dir="npc/store_test")
    npc.use_llm = False  # 测试确定性: 规则模式
    return TestClient(create_npc_server({"cang": npc}))


class TestNpcApi:
    def test_get_npc_returns_persona(self, client):
        r = client.get("/api/npc")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "苍"
        assert data["role"] == "部落的老猎手，火塘边的话事人，见过冰河那边的大兽热爱合作"
        assert data["taboos"] != ""

    def test_get_npcs_list(self, client):
        r = client.get("/api/npcs")
        assert r.status_code == 200
        assert any(n["id"] == "cang" for n in r.json()["npcs"])

    def test_get_state(self, client):
        r = client.get("/api/state")
        assert r.status_code == 200
        data = r.json()
        assert "actors" in data and "delivered" in data
        assert "cang" in data["actors"]

    def test_get_memory_empty(self, client):
        r = client.get("/api/memory")
        assert r.status_code == 200
        assert r.json()["entries"] == []


class TestTalkApi:
    def test_talk_returns_reply(self, client):
        r = client.post("/api/talk", json={"message": "你好"})
        assert r.status_code == 200
        assert "reply" in r.json()
        assert r.json()["reply"]

    def test_talk_empty_message_400(self, client):
        r = client.post("/api/talk", json={"message": ""})
        assert r.status_code == 400


class TestTaskApi:
    def test_task_runs_gather(self, client):
        r = client.post("/api/task", json={"resource": "木材", "count": 2})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert len(data["steps"]) > 0

    def test_task_missing_resource_fails_gracefully(self, client):
        r = client.post("/api/task", json={"resource": "铁矿石", "count": 1})
        assert r.status_code == 200
        assert r.json()["ok"] is False


class TestModeToggle:
    def test_get_mode_rules_by_default(self, client):
        r = client.get("/api/mode")
        assert r.status_code == 200
        assert r.json()["mode"] == "rules"

    def test_set_mode_llm_then_back(self, client):
        r = client.post("/api/mode", json={"mode": "llm"})
        assert r.status_code == 200
        r2 = client.get("/api/mode")
        assert r2.json()["requested"] == "llm"
        r3 = client.post("/api/mode", json={"mode": "rules"})
        assert r3.json()["requested"] == "rules"

    def test_set_mode_invalid_400(self, client):
        r = client.post("/api/mode", json={"mode": "量子"})
        assert r.status_code == 400


class TestLoadVillage:
    """人设以制作者 JSON 为准（记忆卡只存记忆/世界 — 改 JSON 必须生效）。"""

    def test_fresh_persona_wins_over_memory_card(self, tmp_path):
        npc = NPC(store_dir=str(tmp_path))
        npc.save()   # 记忆卡里存的是默认人格(苍)
        from npc.server import load_village

        fresh = dict(npc.persona)
        fresh["personality"] = "改过的新性格"
        world, loaded = load_village(store_dir=str(tmp_path), personas={"cang": fresh})
        assert loaded["cang"].persona["personality"] == "改过的新性格"
        assert loaded["cang"].world["delivered"] == npc.world["delivered"]   # 记忆/世界保留



class TestTickApi:
    """自主循环 API: 手动推帧 + 状态扩展（不进 lifespan 上下文 → 后台循环不启动）。"""

    def test_state_includes_tick_activity_log(self, client):
        r = client.get("/api/state")
        assert r.status_code == 200
        data = r.json()
        assert data["tick"] == 0
        assert "log_tail" in data
        assert data["actors"]["cang"]["state"] == "idle"
        assert data["actors"]["cang"]["activity"] == ""

    def test_manual_tick_advances_tick(self, client):
        r = client.post("/api/tick", json={})
        assert r.status_code == 200
        assert r.json()["tick"] == 1
        r2 = client.post("/api/tick", json={})
        assert r2.json()["tick"] == 2

    def test_manual_tick_with_seed_deterministic(self):
        """同种子 + 同初始世界 → 完全相同的事件（确定性可复现）。"""

        def fresh():
            npc = NPC(store_dir="npc/store_test")
            npc.use_llm = False
            return TestClient(create_npc_server({"cang": npc}))

        r1 = fresh().post("/api/tick", json={"seed": 7})
        r2 = fresh().post("/api/tick", json={"seed": 7})
        assert r1.json()["events"] == r2.json()["events"]
        assert r1.json()["tick"] == 1

    def test_tick_eventually_delivers(self, client):
        """带种子循环推帧 → 苍自主完成采集+交付（HTTP 全链路冒烟）。"""
        for _ in range(30):
            client.post("/api/tick", json={"seed": 7})
        r = client.get("/api/state")
        assert r.json()["delivered"]["木材"] > 0

    def test_state_activity_updates_mid_activity(self, client):
        client.post("/api/tick", json={"seed": 7})
        r = client.get("/api/state")
        assert r.json()["actors"]["cang"]["activity"] != ""

    def test_bad_origin_rejected_for_tick(self, client):
        r = client.post(
            "/api/tick",
            json={"seed": 1},
            headers={"Origin": "http://evil.com"},
        )
        assert r.status_code == 403


class TestSecurity:
    def test_bad_origin_rejected(self, client):
        r = client.post(
            "/api/talk",
            json={"message": "你好"},
            headers={"Origin": "http://evil.com"},
        )
        assert r.status_code == 403

    def test_loopback_origin_allowed(self, client):
        r = client.post(
            "/api/talk",
            json={"message": "你好"},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        assert r.status_code == 200
