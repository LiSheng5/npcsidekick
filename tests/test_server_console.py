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


class TestFlagsObservability:
    """P1-1 运维可观测：`features.flags` 必须覆盖全部决策类开关的生效态。

    DESIGN CONTRACT（2026-09-17 锚定）：家规是"开关默认关、选择加入"，但开关一旦
    落地却**不进 flags**，用户就无法从 `/api/version` 判断它到底有没有生效 ——
    P-6 的 `NPC_GOAL_RELEVANCE` 就漏过一次（2026-09-16 落地、9-17 补上）。
    以后每加一个影响决策的布尔开关，**必须同时在这里挂账**。
    """

    # 决策类布尔开关 → 环境变量名（加开关时同步扩这张表）
    _DECISION_FLAGS = {
        "goals": "NPC_GOALS",
        "lessons": "NPC_LESSONS",
        "goal_relevance": "NPC_GOAL_RELEVANCE",
        "memory_dedup": "NPC_MEMORY_DEDUP",
        "safety_gate": "NPC_SAFETY_GATE",
    }

    def test_all_decision_flags_present(self, client, monkeypatch):
        for k in self._DECISION_FLAGS.values():
            monkeypatch.delenv(k, raising=False)
        flags = client.get("/api/version").json()["features"]["flags"]
        missing = sorted(set(self._DECISION_FLAGS) - set(flags))
        assert not missing, f"决策类开关未进 features.flags（用户无法观测生效态）: {missing}"

    def test_flags_default_off(self, client, monkeypatch):
        """默认态：决策类开关全 False（家规"默认关"，关 = 与旧版逐字节一致）。"""
        for k in self._DECISION_FLAGS.values():
            monkeypatch.delenv(k, raising=False)
        flags = client.get("/api/version").json()["features"]["flags"]
        for name in self._DECISION_FLAGS:
            assert flags[name] is False, f"{name} 默认应为 False（家规：默认关）"

    @pytest.mark.parametrize("name", sorted(_DECISION_FLAGS))
    def test_flags_read_live(self, name, client, monkeypatch):
        """现读现切（家规）：开关是读环境变量、不缓存，改完立刻反映到 flags。"""
        env = self._DECISION_FLAGS[name]
        monkeypatch.delenv(env, raising=False)
        assert client.get("/api/version").json()["features"]["flags"][name] is False
        monkeypatch.setenv(env, "1")
        assert client.get("/api/version").json()["features"]["flags"][name] is True


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
    """根路径由大脑服务器本尊托管（同源零配置）。

    2026-09-08: 旧关系网页 /npc.html 与通用聊天台(web/server.py + agent_static)
    已整套下架移到桌面归档 —— 根路径不再兜底 npc.html，只认 Console。
    """

    def test_root_points_to_console_or_hint(self, client):
        r = client.get("/")
        assert r.status_code == 200
        # Console 已构建 → 重定向到 /console/；未构建 → 返回提示 JSON（不再回落旧页）
        if r.headers.get("content-type", "").startswith("text/html"):
            assert r.url.path == "/console/"
        else:
            assert r.json()["name"] == "NPCSidekick"

    def test_npc_html_is_gone(self, client):
        """旧关系网页已下架 —— 不应再有人依赖 /npc.html。"""
        assert client.get("/npc.html").status_code == 404
