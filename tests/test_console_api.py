"""Web Console 管理型 API 测试（2026-09-07 · Step 4）。

契约:
  · 人设 CRUD: PUT 写盘 + **热加载**（不重启就出现在 /api/npcs 与 /api/state）
  · 删人设: 文件消失 + 实例摘除 + **世界 actor 槽清空**（防 tick 孤儿槽 KeyError）
  · /api/npcs 扩展是 additive: 老字段 id/name 语义不变
  · 关系图: Runtime 无关系 → source="none" + 空 edges（不造假）
  · 记忆单条 CRUD 走 NPCMemory → 记忆卡落盘；写入结果三态
    added / merged（去重闸折叠计数）/ rejected（安全闸拒收）必须如实回报
  · Provider: API Key 只回 masked，明文永不出现在响应里
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from npc.memory import MTYPES
from npc.npc import NPC
from npc.server import create_npc_server


OK_PERSONA = {
    "id": "hun",
    "name": "小铁",
    "identity": "部落的年轻铁匠，手艺还没老练，火候全凭手上感觉",
    "personality": "热情话多，爱显摆，被夸就脸红",
    "speech_style": "快语速，爱用感叹号",
    "taboos": ["湿柴入炉", "不懂装懂"],
    "desires": {"打一把好猎刀": 0.7},
    "goals": {"攒够铁料开炉": {"progress": 0, "target": 1}},
    "rules": {"replies": {"铁": "铁要吃火，不吃话。"}, "fallback": "炉子还热着，你说。"},
    "routine": [{"action": "gather", "resource": "铁料", "count": 1, "weight": 3}],
}

# 明文 key（测试专用假串，不是真密钥）
FAKE_KEY = "sk-test-0000abcd1234"


@pytest.fixture
def paths(tmp_path):
    return {
        "personas": str(tmp_path / "personas"),
        "store": str(tmp_path / "store"),
        "config": str(tmp_path / "config"),
    }


@pytest.fixture(autouse=True)
def isolate_env():
    """隔离 os.environ —— activate provider 会往进程环境里写 key/model。

    不能用 monkeypatch.delenv 收尾: 它把"调用那一刻"的值记为原值，
    teardown 时会把测试里写进去的假 key 又恢复回来，污染后续测试
    （曾导致 test_memory_typed 等拿假 key 真去打网络 → 401 → 断言失败）。
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def client(paths):
    npc = NPC(store_dir=paths["store"])
    npc.use_llm = False
    app = create_npc_server({"cang": npc},
                            personas_dir=paths["personas"],
                            config_dir=paths["config"])
    return TestClient(app)


def persona_file(paths, pid="hun"):
    return os.path.join(paths["personas"], f"{pid}.json")


class TestPersonaCrud:
    def test_get_missing_persona_404(self, client):
        assert client.get("/api/personas/hun").status_code == 404

    def test_put_creates_file(self, client, paths):
        r = client.put("/api/personas/hun", json=OK_PERSONA)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["action"] == "created"
        assert body["hot_reloaded"] is True
        with open(persona_file(paths), encoding="utf-8") as fh:
            assert json.load(fh)["name"] == "小铁"

    def test_put_hot_loads_into_runtime(self, client):
        """热加载: 不重启就出现在列表与世界状态里（需求 §10/§31）。"""
        assert client.put("/api/personas/hun", json=OK_PERSONA).status_code == 200
        ids = [n["id"] for n in client.get("/api/npcs").json()["npcs"]]
        assert "hun" in ids
        actors = client.get("/api/state").json()["actors"]
        assert "hun" in actors          # 世界 actor 槽已注册（否则 tick 会 KeyError）

    def test_post_creates_and_hot_reloads(self, client):
        """2026-09-08 (Step 5): POST 也热加载 —— 与 PUT 口径一致，不再"重启后生效"。"""
        r = client.post("/api/personas", json=OK_PERSONA)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["hot_reloaded"] is True
        assert body["action"] == "created"
        ids = [n["id"] for n in client.get("/api/npcs").json()["npcs"]]
        assert "hun" in ids
        assert "hun" in client.get("/api/state").json()["actors"]

    def test_put_updates_existing_in_place(self, client, paths):
        client.put("/api/personas/hun", json=OK_PERSONA)
        changed = dict(OK_PERSONA, speech_style="慢条斯理，一句话想三遍")
        r = client.put("/api/personas/hun", json=changed)
        assert r.status_code == 200 and r.json()["action"] == "updated"
        # 运行时实例的人设同步更新（不是只改了文件）
        p = client.get("/api/personas/hun").json()["persona"]
        assert p["speech_style"] == "慢条斯理，一句话想三遍"
        # 文件也只有一个（没有被改名改出第二份）
        assert sorted(os.listdir(paths["personas"])) == ["hun.json"]

    def test_put_invalid_persona_422(self, client):
        bad = {k: v for k, v in OK_PERSONA.items() if k != "personality"}
        assert client.put("/api/personas/hun", json=bad).status_code == 422

    def test_put_illegal_id_400(self, client):
        assert client.put("/api/personas/bad%20id", json=OK_PERSONA).status_code == 400

    def test_delete_removes_file_instance_and_slot(self, client, paths):
        client.put("/api/personas/hun", json=OK_PERSONA)
        r = client.delete("/api/personas/hun")
        assert r.status_code == 200 and r.json()["ok"] is True
        # "删除"= 移进 .trash（可反悔），人设目录里已不再有它
        assert not os.path.exists(persona_file(paths))
        trash = os.path.join(paths["personas"], ".trash")
        assert any(f.startswith("hun") for f in os.listdir(trash))
        ids = [n["id"] for n in client.get("/api/npcs").json()["npcs"]]
        assert "hun" not in ids
        assert "hun" not in client.get("/api/state").json()["actors"]   # 孤儿槽已清
        # 回收站是子目录，人设扫描器不会把它捡回来
        assert client.get("/api/personas").json()["personas"] == []

    def test_delete_missing_404(self, client):
        assert client.delete("/api/personas/nope").status_code == 404

    def test_delete_keeps_memory_card_by_default(self, client, paths):
        client.put("/api/personas/hun", json=OK_PERSONA)
        client.post("/api/npcs/hun/memory", json={"content": "第一次开炉"})
        card = os.path.join(paths["store"], "hun_memory.json")
        assert os.path.exists(card)
        client.delete("/api/personas/hun")
        assert os.path.exists(card)          # 默认保留（可再次创建续前缘）

    def test_delete_with_drop_memory(self, client, paths):
        client.put("/api/personas/hun", json=OK_PERSONA)
        client.post("/api/npcs/hun/memory", json={"content": "开炉第一天"})
        card = os.path.join(paths["store"], "hun_memory.json")
        r = client.delete("/api/personas/hun?drop_memory=true")
        assert r.json()["memory_dropped"] is True
        assert not os.path.exists(card)


class TestNpcsListAdditive:
    def test_legacy_fields_intact(self, client):
        npcs = client.get("/api/npcs").json()["npcs"]
        cang = next(n for n in npcs if n["id"] == "cang")
        assert cang["name"] == "苍"                 # 老语义不变
        assert set(("id", "name")).issubset(cang)

    def test_console_fields_added(self, client):
        cang = next(n for n in client.get("/api/npcs").json()["npcs"]
                    if n["id"] == "cang")
        for field in ("state", "activity", "position", "memory_count", "identity"):
            assert field in cang


class TestRelationships:
    def test_no_relationship_data_is_honest(self, client):
        """Runtime 没有关系数据 → 空 edges + source="none"，绝不用 demo 顶替。"""
        data = client.get("/api/relationships").json()
        assert data["source"] == "none"
        assert data["edges"] == []
        assert data["note"]
        ids = {n["id"] for n in data["nodes"]}
        assert "cang" in ids

    def test_persona_relations_are_rendered(self, client):
        rel = dict(OK_PERSONA, relations=[{"who": "cang", "how": "师徒", "score": "+80"}])
        client.put("/api/personas/hun", json=rel)
        data = client.get("/api/relationships").json()
        assert data["source"] == "persona"
        edge = data["edges"][0]
        assert edge["source"] == "hun" and edge["target"] == "cang"
        assert edge["type"] == "师徒"

    def test_world_relationships_take_priority(self):
        """世界声明的关系 = 未来 Runtime 出口，优先于 persona 手填。"""
        from npc.console_api import ConsoleContext, build_relationships

        npc = NPC(store_dir="npc/store_test")
        npc.use_llm = False
        world = {"actors": {"cang": {"position": "村庄", "inventory": {}}},
                 "_relationships": {"edges": [
                     {"source": "cang", "target": "player", "type": "knows", "weight": 3}]}}
        ctx = ConsoleContext(npcs={"cang": npc}, world=world,
                             personas_path=None, store_dir="npc/store_test")
        data = build_relationships(ctx)
        assert data["source"] == "runtime"
        assert data["edges"][0]["type"] == "knows"
        # 边里出现的不知名实体 → 合成节点，类型不假设（不是写死的 npc/player）
        kinds = {n["id"]: n["type"] for n in data["nodes"]}
        assert kinds["cang"] == "npc" and kinds["player"] == "custom"


class TestMemoryCrud:
    def test_add_list_edit_delete(self, client, paths):
        client.put("/api/personas/hun", json=OK_PERSONA)
        r = client.post("/api/npcs/hun/memory",
                        json={"content": "玩家帮我修好了风箱", "importance": 8,
                              "category": "episode"})
        assert r.status_code == 200 and r.json()["ok"] is True
        mid = r.json()["entry"]["id"]

        entries = client.get("/api/npcs/hun/memory").json()["entries"]
        assert any(e["content"] == "玩家帮我修好了风箱" for e in entries)

        r = client.put(f"/api/npcs/hun/memory/{mid}",
                       json={"content": "玩家帮我修好了风箱（还加了新皮带）",
                             "importance": 9})
        assert r.status_code == 200 and r.json()["entry"]["importance"] == 9

        r = client.delete(f"/api/npcs/hun/memory/{mid}")
        assert r.status_code == 200
        assert client.get("/api/npcs/hun/memory").json()["total"] == 0

    def test_add_persists_to_memory_card(self, client, paths):
        client.put("/api/personas/hun", json=OK_PERSONA)
        client.post("/api/npcs/hun/memory", json={"content": "开炉第一天"})
        card = os.path.join(paths["store"], "hun_memory.json")
        with open(card, encoding="utf-8") as fh:
            assert any("开炉第一天" in e["content"] for e in json.load(fh)["memory"])

    def test_add_reports_added_status(self, client, paths):
        client.put("/api/personas/hun", json=OK_PERSONA)
        body = client.post("/api/npcs/hun/memory",
                           json={"content": "开炉第一天", "importance": 7}).json()
        assert body["ok"] is True and body["status"] == "added"
        assert body["entry"]["content"] == "开炉第一天" and body["total"] == 1

    def test_duplicate_daily_entry_is_reported_as_merged(self, client, paths, monkeypatch):
        """同文日常被去重闸折叠成 count+1 —— 不是"没写进去"，UI 必须分得清。"""
        monkeypatch.setenv("NPC_MEMORY_DEDUP", "1")
        client.put("/api/personas/hun", json=OK_PERSONA)
        client.post("/api/npcs/hun/memory", json={"content": "在炉边打盹", "importance": 3})
        body = client.post("/api/npcs/hun/memory",
                           json={"content": "在炉边打盹", "importance": 3}).json()
        assert body["ok"] is True and body["status"] == "merged"
        assert body["entry"]["count"] == 2 and body["total"] == 1
        assert "计数" in body["message"]

    def test_safety_gate_rejection_is_reported_not_silently_ok(
            self, client, paths, monkeypatch):
        """安全闸拒收必须报 rejected —— 否则 UI 会把"没落盘"显示成"已保存"。

        不塞真实 L1 词条进仓库，直接把安检门换成永远返回 L1 的桩。
        """
        class _AlwaysL1:
            @staticmethod
            def enabled():
                return True

            @staticmethod
            def scan(_text):
                from npc.safety import Verdict
                return Verdict(level="L1", category="test")

        monkeypatch.setattr("npc.memory_card._safety", _AlwaysL1)
        client.put("/api/personas/hun", json=OK_PERSONA)
        r = client.post("/api/npcs/hun/memory", json={"content": "不该落盘的东西"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and body["status"] == "rejected"
        assert body["entry"] is None and body["total"] == 0
        assert "拒收" in body["message"]

    def test_facets_come_from_data_not_hardcoded(self, client, paths):
        """分类可选值从实际数据观察 —— Console 不替游戏规定"标准分类"。"""
        client.put("/api/personas/hun", json=OK_PERSONA)
        client.post("/api/npcs/hun/memory",
                    json={"content": "玩家欠我三块铁", "category": "契约"})
        client.post("/api/npcs/hun/memory",
                    json={"content": "火要旺，风要匀", "category": "手艺",
                          "mtype": "instruction"})
        facets = client.get("/api/npcs/hun/memory").json()["facets"]
        assert {"契约", "手艺"}.issubset(set(facets["categories"]))
        # 框架声明的三分类（与任何具体游戏无关）也在建议里
        assert set(MTYPES).issubset(set(facets["mtypes"]))

    def test_query_recalls_top_k_not_full_list(self, client, paths):
        """有 query 时走加权检索: entries 是召回 top-k，total 才是卡上总数。"""
        client.put("/api/personas/hun", json=OK_PERSONA)
        for i in range(6):
            client.post("/api/npcs/hun/memory", json={"content": f"第{i}天开炉"})
        data = client.get("/api/npcs/hun/memory?query=开炉&top_k=3").json()
        assert data["total"] == 6 and len(data["entries"]) == 3

    def test_empty_content_400(self, client, paths):
        client.put("/api/personas/hun", json=OK_PERSONA)
        assert client.post("/api/npcs/hun/memory", json={"content": "  "}).status_code == 400

    def test_unknown_npc_404(self, client):
        assert client.get("/api/npcs/ghost/memory").status_code == 404

    def test_unknown_memory_404(self, client, paths):
        client.put("/api/personas/hun", json=OK_PERSONA)
        assert client.put("/api/npcs/hun/memory/nope", json={"content": "x"}).status_code == 404


class TestProviders:
    def test_key_never_leaks(self, client):
        r = client.post("/api/settings/providers",
                        json={"id": "openai", "name": "OpenAI",
                              "base_url": "https://api.openai.com/v1",
                              "model": "gpt-4o-mini", "api_key": FAKE_KEY})
        assert r.status_code == 200, r.text
        assert FAKE_KEY not in r.text
        prov = r.json()["provider"]
        assert prov["configured"] is True
        assert prov["masked_key"].endswith(FAKE_KEY[-4:])
        assert FAKE_KEY[-4:] in prov["masked_key"] and len(prov["masked_key"]) < len(FAKE_KEY)

    def test_list_masks_and_roundtrip(self, client):
        client.post("/api/settings/providers",
                    json={"id": "ds", "model": "deepseek-v4-flash", "api_key": FAKE_KEY})
        data = client.get("/api/settings/providers").json()
        ds = next(p for p in data["providers"] if p["id"] == "ds")
        assert ds["configured"] is True and FAKE_KEY not in json.dumps(data, ensure_ascii=False)
        assert "key" not in ds                      # 明文字段根本不在视图里

    def test_delete_provider(self, client):
        client.post("/api/settings/providers", json={"id": "ds", "api_key": FAKE_KEY})
        assert client.delete("/api/settings/providers/ds").status_code == 200
        assert client.get("/api/settings/providers").json()["providers"] == []
        assert client.delete("/api/settings/providers/ds").status_code == 404

    def test_activate_applies_to_runtime(self, client):
        client.post("/api/settings/providers",
                    json={"id": "ds", "model": "deepseek-v4-flash",
                          "base_url": "https://api.deepseek.com",
                          "api_key": FAKE_KEY})
        r = client.post("/api/settings/providers/ds/activate")
        assert r.status_code == 200
        env = r.json()["env"]
        assert env["model"] == "deepseek-v4-flash"
        assert env["configured"] is True
        assert FAKE_KEY not in json.dumps(env, ensure_ascii=False)
        assert os.environ.get("LLM_API_KEY") == FAKE_KEY   # 已注入进程（Runtime 解析口径）
        # 环境清理交给 isolate_env fixture

    def test_llm_test_without_key(self, client):
        r = client.post("/api/llm/test", json={"model": "whatever"})
        assert r.status_code == 200
        assert r.json()["ok"] is False and "API Key" in r.json()["error"]


class TestEmptyRuntimeSafety:
    def test_state_and_events_survive_deleting_all_npcs(self, client, paths):
        """Console 允许删光 NPC: /api/state、/api/events 不再 StopIteration。"""
        # 手工补一份 cang 人设（模拟原本就存在的人设文件），才删得掉运行时里的它
        os.makedirs(paths["personas"], exist_ok=True)
        with open(os.path.join(paths["personas"], "cang.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"id": "cang", "name": "苍", "identity": "老猎手",
                       "personality": "寡言", "speech_style": "短句",
                       "taboos": []}, fh)
        assert client.delete("/api/personas/cang").status_code == 200
        assert client.get("/api/npcs").json()["npcs"] == []
        state = client.get("/api/state")
        assert state.status_code == 200
        assert state.json()["actors"] == {}
        events = client.get("/api/events")
        assert events.status_code == 200
        assert events.json()["events"] == []


class TestActions:
    """GET /api/actions —— 动作名建议（**不是白名单**，game-agnostic）。"""

    def test_returns_runtime_actions(self, client):
        r = client.get("/api/actions")
        assert r.status_code == 200
        d = r.json()
        # 当前世界能执行的动作（来自 npc/world.py ACTIONS，至少含 move/gather）
        assert set(d["runtime"]) >= {"move", "gather"}

    def test_observed_collects_custom_actions(self, client):
        """人设 routine 里的自定义动作名会被收进 observed（不硬编码）。"""
        body = dict(OK_PERSONA, routine=[{"action": "打铁", "weight": 1}])
        assert client.put("/api/personas/hun", json=body).status_code == 200
        d = client.get("/api/actions").json()
        assert "打铁" in d["observed"]

    def test_note_clarifies_not_whitelist(self, client):
        d = client.get("/api/actions").json()
        assert "建议" in d.get("note", "")
