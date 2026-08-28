"""制作者人设 API 测试 — GET/POST /api/personas（关系网"新建 NPC"）。

契约: GET 列目录(与 load_personas_from_dir 同加载逻辑); POST 校验后落盘
npc/personas/<id>.json, 非法 id → 400, 缺必需字段 → 422, 已存在 → 409。
"""
import json

import pytest
from fastapi.testclient import TestClient

from npc.npc import NPC
from npc.server import create_npc_server


@pytest.fixture
def personas_dir(tmp_path):
    return str(tmp_path / "personas")


@pytest.fixture
def client(personas_dir):
    npc = NPC(store_dir="npc/store_test")
    npc.use_llm = False
    return TestClient(create_npc_server({"cang": npc}, personas_dir=personas_dir))


OK_BODY = {
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


class TestPersonasApi:
    def test_list_empty_dir(self, client):
        r = client.get("/api/personas")
        assert r.status_code == 200
        assert r.json()["personas"] == []

    def test_create_persona_writes_json(self, client, personas_dir):
        import os
        r = client.post("/api/personas", json=OK_BODY)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["npc_id"] == "hun"
        # 落盘 + 可被加载器读回（制作者闭环: 写完就活）
        p = os.path.join(personas_dir, "hun.json")
        assert os.path.exists(p)
        data = json.load(open(p, encoding="utf-8"))
        assert data["name"] == "小铁"
        assert data["taboos"] == ["湿柴入炉", "不懂装懂"]
        r2 = client.get("/api/personas")
        assert any(x["id"] == "hun" for x in r2.json()["personas"])

    def test_name_defaults_to_id(self, client, personas_dir):
        body = dict(OK_BODY, name="", id="joe")
        client.post("/api/personas", json=body)
        data = json.load(open(f"{personas_dir}/joe.json", encoding="utf-8"))
        assert data["name"] == "joe"

    def test_missing_required_fields_422(self, client):
        r = client.post("/api/personas", json={"id": "x"})
        assert r.status_code == 422
        assert "缺少必需字段" in r.json()["detail"]

    def test_bad_id_rejected(self, client):
        for bad in ("../evil", "中文", "a b", "", "x" * 33):
            r = client.post("/api/personas", json=dict(OK_BODY, id=bad))
            assert r.status_code == 400, bad

    def test_duplicate_id_409(self, client):
        assert client.post("/api/personas", json=OK_BODY).status_code == 200
        r = client.post("/api/personas", json=OK_BODY)
        assert r.status_code == 409
        assert "已存在" in r.json()["detail"]

    def test_bad_taboos_type_422(self, client):
        body = dict(OK_BODY, taboos="不是列表")
        r = client.post("/api/personas", json=body)
        assert r.status_code == 422

    def test_bad_routine_item_is_cleaned(self, client, personas_dir):
        body = dict(OK_BODY, id="clean", routine=[{"action": "fly", "weight": 1}])
        r = client.post("/api/personas", json=body)
        assert r.status_code == 200
        data = json.load(open(f"{personas_dir}/clean.json", encoding="utf-8"))
        assert data["routine"] == []   # 坏项丢弃不炸，顶多"不动"
