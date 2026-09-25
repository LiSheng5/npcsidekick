"""console_api.py 测试 —— 控制台（调试面）端点 + 静态挂载。"""
from __future__ import annotations

import json

import pytest

import chatlog
import memory
import server


CAPS = {"mod": "sims4", "actions": [
    {"name": "cook", "desc": "用厨房做饭", "params": {"dish": "菜名(字符串)"}}]}


# ── 静态挂载 ─────────────────────────────────────────────

def test_root_redirects_to_console(tmp_store, persona_dir, make_app_client):
    client = make_app_client()
    res = client.get("/", follow_redirects=False)
    assert res.status_code in (307, 308)
    assert res.headers["location"] == "/console/"


def test_console_assets_served(tmp_store, persona_dir, make_app_client):
    client = make_app_client()
    for path in ("/console/", "/console/index.html", "/console/app.js", "/console/styles.css"):
        res = client.get(path)
        assert res.status_code == 200, path
    assert "NPCSidekick" in client.get("/console/index.html").text


# ── 角色卡读写 ─────────────────────────────────────────────

def test_list_personas(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    write_persona("ali", name="阿黎")
    client = make_app_client()

    items = client.get("/api/personas").json()["personas"]

    assert [i["id"] for i in items] == ["ali", "cang"]
    assert items[1]["name"] == "苍"


def test_list_personas_reports_broken_file(tmp_store, persona_dir, make_app_client):
    (persona_dir / "bad.json").write_text("{坏", encoding="utf-8")
    client = make_app_client()

    item = client.get("/api/personas").json()["personas"][0]
    assert item["id"] == "bad" and "error" in item


def test_underscore_template_is_not_an_npc(tmp_store, persona_dir, write_persona, make_app_client):
    """personas/_模板.json 是空白模板：不该出现在角色列表 / NPC 列表 / 关系网里。"""
    write_persona("cang")
    (persona_dir / "_模板.json").write_text(
        json.dumps({"name": "模板", "identity": "说明文字"}, ensure_ascii=False), encoding="utf-8")
    client = make_app_client()

    assert [i["id"] for i in client.get("/api/personas").json()["personas"]] == ["cang"]
    assert [n["npc_id"] for n in client.get("/api/npcs").json()["npcs"]] == ["cang"]
    assert [n["id"] for n in client.get("/api/relationships").json()["nodes"]] == ["cang"]


def test_get_persona_and_404(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    assert client.get("/api/personas/cang").json()["persona"]["id"] == "cang"
    assert client.get("/api/personas/nobody").status_code == 404


def test_put_persona_creates_and_updates(tmp_store, persona_dir, make_app_client):
    client = make_app_client()

    res = client.put("/api/personas/cang", json={"name": "苍", "identity": "老猎手"})
    assert res.status_code == 200
    saved = json.loads((persona_dir / "cang.json").read_text("utf-8"))
    assert saved["id"] == "cang" and saved["name"] == "苍"

    client.put("/api/personas/cang", json={"name": "苍二"})
    assert json.loads((persona_dir / "cang.json").read_text("utf-8"))["name"] == "苍二"


def test_put_persona_id_mismatch_400(tmp_store, persona_dir, make_app_client):
    client = make_app_client()
    res = client.put("/api/personas/cang", json={"id": "ali", "name": "x"})
    assert res.status_code == 400
    assert not (persona_dir / "cang.json").exists()


def test_put_persona_bad_body_400(tmp_store, persona_dir, make_app_client):
    client = make_app_client()
    assert client.put("/api/personas/cang", content=b"{bad",
                      headers={"Content-Type": "application/json"}).status_code == 400
    assert client.put("/api/personas/cang", json=["不是对象"]).status_code == 400


# ── 记忆卡 CRUD ──────────────────────────────────────────

def test_memory_list_decorates_strength(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    memory.add_entry("cang", "钉住的话", importance=1, pinned=True)
    client = make_app_client()

    data = client.get("/api/npcs/cang/memory").json()

    assert data["count"] == 1
    entry = data["entries"][0]
    assert entry["exempt"] is True            # pinned → 豁免修剪
    assert entry["strength"] == pytest.approx(1.0, abs=0.2)
    assert entry["age_hours"] == pytest.approx(0.0, abs=0.1)
    assert data["prune_threshold"] == 1.0


def test_memory_add_update_delete(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    added = client.post("/api/npcs/cang/memory",
                        json={"content": "玩家爱吃面", "importance": 7,
                              "category": "preference", "pinned": True})
    assert added.status_code == 200
    entry = added.json()["entry"]
    assert entry["importance"] == 7 and entry["pinned"] is True

    updated = client.put(f"/api/npcs/cang/memory/{entry['id']}",
                         json={"content": "玩家最爱吃面", "importance": 9, "pinned": False})
    assert updated.status_code == 200
    assert updated.json()["entry"]["content"] == "玩家最爱吃面"
    assert "pinned" not in memory.load_card("cang")[0]        # pinned=False 会删键

    assert client.delete(f"/api/npcs/cang/memory/{entry['id']}").status_code == 200
    assert memory.load_card("cang") == []


def test_memory_endpoint_validation(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    assert client.post("/api/npcs/cang/memory", json={"content": "  "}).status_code == 400
    assert client.put("/api/npcs/cang/memory/不存在", json={"importance": 5}).status_code == 404
    assert client.delete("/api/npcs/cang/memory/不存在").status_code == 404


def test_memory_update_needs_a_field(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    entry = memory.add_entry("cang", "一条")
    client = make_app_client()

    res = client.put(f"/api/npcs/cang/memory/{entry['id']}", json={"unknown": 1})
    assert res.status_code == 400


def test_memory_prune_endpoint(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    import time
    import uuid
    old = {"id": uuid.uuid4().hex, "content": "很久以前的弱记忆", "importance": 1,
           "category": "general", "created_at": time.time() - 100000 * 3600}
    memory.save_card("cang", [old])
    client = make_app_client()

    res = client.post("/api/npcs/cang/prune").json()

    assert res == {"ok": True, "npc_id": "cang", "removed": 1, "remaining": 0}


# ── 聊天记录 ─────────────────────────────────────────────

def test_chat_endpoint_paging_and_summary(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    for i in range(5):
        chatlog.append_turn("cang", f"问{i}", f"答{i}")
    chatlog._save_summary("cang", "此前聊过吃饭的事", 4)
    client = make_app_client()

    data = client.get("/api/npcs/cang/chat?limit=2").json()

    assert data["total"] == 10
    assert [m["content"] for m in data["turns"]] == ["问4", "答4"]
    assert data["summary"]["summary"] == "此前聊过吃饭的事"
    assert data["summary"]["covered"] == 4
    assert data["estimated_tokens"] > 0
    assert data["summary_threshold"] == chatlog.token_threshold()

    older = client.get("/api/npcs/cang/chat?limit=2&offset=2").json()
    assert [m["content"] for m in older["turns"]] == ["问3", "答3"]


def test_chat_endpoint_empty(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    data = client.get("/api/npcs/cang/chat").json()
    assert data["total"] == 0 and data["turns"] == [] and data["summary"]["summary"] == ""


# ── 关系网（persona 的 relations 字段驱动）────────────────

def test_relationships_empty_is_honest(tmp_store, write_persona, make_app_client):
    """没人填 relations → source="none" + 空 edges，并给出怎么加，绝不编造。"""
    write_persona("cang")
    write_persona("ali", name="阿黎")
    client = make_app_client()

    data = client.get("/api/relationships").json()

    assert data["source"] == "none"
    assert data["edges"] == []
    assert [n["id"] for n in data["nodes"]] == ["ali", "cang"]
    assert data["note"]


def test_relationships_from_persona_field(tmp_store, write_persona, make_app_client):
    write_persona("cang", relations=[
        {"who": "ali", "how": "师徒", "weight": 0.8, "since": "入冬前"},
        {"who": "玩家", "how": "信任"},
    ])
    write_persona("ali", name="阿黎")
    client = make_app_client()

    data = client.get("/api/relationships").json()

    assert data["source"] == "persona"
    assert len(data["edges"]) == 2
    first = data["edges"][0]
    assert (first["source"], first["target"], first["type"]) == ("cang", "ali", "师徒")
    assert first["weight"] == 0.8
    assert first["metadata"] == {"since": "入冬前"}
    assert data["note"] == ""


def test_relationships_synthesizes_unknown_endpoints(tmp_store, write_persona, make_app_client):
    """边里出现但不在角色卡里的对象 → 补合成节点（类型不假设）。"""
    write_persona("cang", relations=[{"who": "玩家", "how": "信任"}])
    client = make_app_client()

    nodes = {n["id"]: n for n in client.get("/api/relationships").json()["nodes"]}

    assert nodes["玩家"]["type"] == "custom"
    assert nodes["玩家"]["synthetic"] is True
    assert nodes["cang"]["type"] == "npc" and "synthetic" not in nodes["cang"]


def test_relationships_accepts_aliases_and_skips_junk(tmp_store, write_persona,
                                                      make_app_client):
    """target / type / score 是 who / how / weight 的别名；坏条目静默跳过。"""
    write_persona("cang", relations=[
        {"target": "ali", "type": "同族", "score": 3},
        {"who": "   "},          # 空目标 → 跳过
        "不是对象",                # 非对象 → 跳过
        {"how": "没有目标"},        # 缺 who/target → 跳过
    ])
    client = make_app_client()

    data = client.get("/api/relationships").json()

    assert len(data["edges"]) == 1
    assert (data["edges"][0]["target"], data["edges"][0]["type"]) == ("ali", "同族")
    assert data["edges"][0]["weight"] == 3


def test_relationships_reports_broken_persona(tmp_store, persona_dir, write_persona,
                                              make_app_client):
    write_persona("cang")
    (persona_dir / "bad.json").write_text("{坏", encoding="utf-8")
    client = make_app_client()

    nodes = {n["id"]: n for n in client.get("/api/relationships").json()["nodes"]}

    assert "error" in nodes["bad"]
    assert nodes["cang"]["name"] == "苍"


def test_relationships_ignores_non_list_relations(tmp_store, write_persona, make_app_client):
    write_persona("cang", relations={"who": "ali"})
    client = make_app_client()

    data = client.get("/api/relationships").json()
    assert data["source"] == "none" and data["edges"] == []


# ── 头像（关系网节点）────────────────────────────────────

def _png_data_uri(extra: bytes = b"\x89PNG\r\n\x1a\n") -> str:
    import base64
    return "data:image/png;base64," + base64.b64encode(extra).decode("ascii")


def test_avatar_upload_then_served_and_listed(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    res = client.post("/api/npcs/cang/avatar", json={"image": _png_data_uri(b"\x89PNG\r\n\x1a\n" + b"a" * 40)})

    assert res.status_code == 200
    body = res.json()
    assert body["bytes"] == 48
    assert body["avatar"].startswith("/avatars/cang.png?v=")

    # 静态挂载能取到（浏览器就是靠它显示）
    served = client.get(body["avatar"])
    assert served.status_code == 200
    assert served.content[:4] == b"\x89PNG"

    # relationships 里带上头像
    node = {n["id"]: n for n in client.get("/api/relationships").json()["nodes"]}["cang"]
    assert node["avatar"] == body["avatar"]


def test_avatar_replaces_previous_extension(tmp_store, write_persona, avatar_dir, make_app_client):
    write_persona("cang")
    client = make_app_client()

    client.post("/api/npcs/cang/avatar", json={"image": _png_data_uri()})
    assert (avatar_dir / "cang.png").exists()

    import base64
    jpg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"b" * 20).decode("ascii")
    res = client.post("/api/npcs/cang/avatar", json={"image": jpg})

    assert res.json()["avatar"].startswith("/avatars/cang.jpg?v=")
    assert sorted(p.name for p in avatar_dir.iterdir()) == ["cang.jpg"]   # 旧 png 已清掉


def test_avatar_rejects_bad_payload(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    assert client.post("/api/npcs/cang/avatar", json={}).status_code == 400
    assert client.post("/api/npcs/cang/avatar", json={"image": 123}).status_code == 400
    assert client.post("/api/npcs/cang/avatar",
                       json={"image": "https://example.com/a.png"}).status_code == 400
    assert client.post("/api/npcs/cang/avatar",
                       json={"image": "data:image/png;base64,!!!不是base64"}).status_code == 400
    assert client.post("/api/npcs/cang/avatar",
                       json={"image": "data:image/png;base64,"}).status_code == 400
    assert client.post("/api/npcs/cang/avatar",
                       json={"image": "data:text/plain;base64," + "YQ=="}).status_code == 400


def test_avatar_rejects_oversize(tmp_store, write_persona, make_app_client):
    import base64
    write_persona("cang")
    client = make_app_client()

    big = "data:image/png;base64," + base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode("ascii")
    res = client.post("/api/npcs/cang/avatar", json={"image": big})

    assert res.status_code == 400
    assert "太大" in res.json()["detail"]


def test_avatar_rejects_traversal_id(tmp_store, write_persona, avatar_dir, make_app_client):
    write_persona("cang")
    client = make_app_client()

    assert client.post("/api/npcs/%2E%2E/avatar", json={"image": _png_data_uri()}).status_code == 400
    assert not avatar_dir.exists() or not list(avatar_dir.iterdir())


def test_avatar_delete(tmp_store, write_persona, avatar_dir, make_app_client):
    write_persona("cang")
    client = make_app_client()

    assert client.delete("/api/npcs/cang/avatar").status_code == 404      # 还没有

    client.post("/api/npcs/cang/avatar", json={"image": _png_data_uri()})
    res = client.delete("/api/npcs/cang/avatar")

    assert res.status_code == 200 and res.json()["removed"] == 1
    assert list(avatar_dir.iterdir()) == []
    node = {n["id"]: n for n in client.get("/api/relationships").json()["nodes"]}["cang"]
    assert node["avatar"] is None


def test_avatar_on_synthetic_node(tmp_store, write_persona, make_app_client):
    """合成节点（不在 personas 里，如"玩家"）也能挂头像。"""
    write_persona("cang", relations=[{"who": "玩家", "how": "信任"}])
    client = make_app_client()

    res = client.post("/api/npcs/玩家/avatar", json={"image": _png_data_uri()})

    assert res.status_code == 200
    nodes = {n["id"]: n for n in client.get("/api/relationships").json()["nodes"]}
    assert nodes["玩家"]["synthetic"] is True and nodes["玩家"]["avatar"] == res.json()["avatar"]


def test_relationships_avatar_null_without_file(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    nodes = {n["id"]: n for n in client.get("/api/relationships").json()["nodes"]}
    assert nodes["cang"]["avatar"] is None


# ── 能力清单只读视图 ─────────────────────────────────────

def test_get_capabilities_view(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()
    client.post("/api/capabilities", json=CAPS)

    data = client.get("/api/capabilities").json()

    assert data["mods"]["sims4"]["online"] is True
    assert data["mods"]["sims4"]["actions"][0]["name"] == "cook"
    assert data["heartbeat_timeout_s"] == 60.0


def test_get_capabilities_empty(tmp_store, write_persona, make_app_client):
    client = make_app_client()
    assert client.get("/api/capabilities").json()["mods"] == {}


# ── 与游戏面互不干扰 ─────────────────────────────────────

def test_console_router_does_not_shadow_game_endpoints(tmp_store, write_persona,
                                                       make_app_client, make_provider):
    """调试面新增端点不影响协议四端点（mod 契约不变）。"""
    write_persona("cang")
    client = make_app_client(provider=make_provider(turns=[{"text": "嗯。"}]))

    assert client.post("/api/capabilities", json=CAPS).status_code == 200
    assert client.post("/api/action_result",
                       json={"npc_id": "cang", "action": "cook", "ok": True}).status_code == 200
    assert client.post("/api/talk",
                       json={"npc_id": "cang", "message": "在吗"}).status_code == 200
    assert client.get("/api/state").status_code == 200
    assert client.get("/api/npcs").status_code == 200
    assert server.CONSOLE_DIR.is_dir()
