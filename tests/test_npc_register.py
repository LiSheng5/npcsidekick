# -*- coding: utf-8 -*-
"""动态注册(2026-08-22,GTA 前置 #1): ped 随刷随出热注册。
- register: 幂等/人设兜底/id 白名单(防路径穿越)/流民不落盘/常驻续前缘/上限
- unregister: 流民即删(RAM 忘)/常驻先落盘/cast 静态角色不可反注册
- ephemeral: save() 跳过;tick_round 对孤儿 actor 槽防御
"""
import pytest
from fastapi.testclient import TestClient

import npc.llm_wiring as wiring_mod
import npc.server as server_mod
from npc.npc import NPC
from npc.scheduler import tick_round
from npc.server import create_npc_server
from npc.world import actor_of, default_world


@pytest.fixture
def env(tmp_path, monkeypatch):
    """干净环境: 隔离 store 目录,砍掉真 LLM key(register 默认 use_llm=True 会打真 API)。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.setattr(wiring_mod, "resolve_api_key", lambda model_name: "")
    npc = NPC(store_dir=str(tmp_path))
    npc.use_llm = False
    return {"client": TestClient(create_npc_server({"cang": npc})),
            "store": tmp_path}


class TestRegister:
    def test_register_minimal_persona(self, env):
        """最少只需 id: 其余字段按路人兜底。"""
        r = env["client"].post("/api/npc/register",
                               json={"persona": {"id": "ped_001"}})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] and data["npc_id"] == "ped_001"
        assert data["existed"] is False
        assert data["persistent"] is False   # 默认流民

    def test_register_idempotent(self, env):
        """重复注册(反复进出同步半径) → existed=True,不重建。"""
        c = env["client"]
        c.post("/api/npc/register", json={"persona": {"id": "ped_001"}})
        r = c.post("/api/npc/register", json={"persona": {"id": "ped_001"}})
        assert r.json()["existed"] is True

    def test_registered_npc_listed_and_talkable(self, env):
        """注册后即出现在 NPC 列表,/api/talk 可用(规则模式)。"""
        c = env["client"]
        c.post("/api/npc/register", json={
            "persona": {"id": "ped_002", "name": "路人甲",
                        "rules": {"replies": {}, "fallback": "走开。"}},
            "use_llm": False,
        })
        ids = [n["id"] for n in c.get("/api/npcs").json()["npcs"]]
        assert "ped_002" in ids
        r = c.post("/api/talk", json={"message": "你好", "npc_id": "ped_002"})
        assert r.status_code == 200
        assert r.json()["reply"] == "走开。"

    def test_register_bad_ids_rejected(self, env):
        """id 白名单: 路径穿越/空/超长/带空格 → 400(store 文件名由 id 拼出)。"""
        c = env["client"]
        for bad in ("../evil", "a/b", "", "x" * 33, "a b", None):
            r = c.post("/api/npc/register", json={"persona": {"id": bad}})
            assert r.status_code == 400, f"id={bad!r} 应被拒"

    def test_register_ephemeral_never_saves(self, env):
        """流民: 注册→触发 save 路径(consolidate)→不落盘。"""
        c = env["client"]
        c.post("/api/npc/register", json={"persona": {"id": "ped_003"}})
        # 反注册(流民路径不落盘)后 store 无该文件
        c.post("/api/npc/unregister", json={"npc_id": "ped_003"})
        assert not (env["store"] / "ped_003_memory.json").exists()

    def test_register_persistent_saves_on_unregister(self, env):
        """常驻层: 反注册时落盘记忆卡(下次注册续前缘)。"""
        c = env["client"]
        c.post("/api/npc/register", json={
            "persona": {"id": "local_barber", "name": "理发师"},
            "persistent": True, "use_llm": False,
        })
        c.post("/api/talk", json={"message": "剪个头", "npc_id": "local_barber"})
        c.post("/api/npc/unregister", json={"npc_id": "local_barber"})
        card = env["store"] / "local_barber_memory.json"
        assert card.exists(), "常驻层反注册应落盘"
        assert "理发师" in card.read_text(encoding="utf-8")

    def test_register_cap(self, env, monkeypatch):
        """动态 NPC 上限 → 429(防失控)。"""
        monkeypatch.setattr(server_mod, "MAX_DYNAMIC_NPCS", 2)
        c = env["client"]
        for i in range(2):
            assert c.post("/api/npc/register",
                          json={"persona": {"id": f"ped_{i}"}}).status_code == 200
        r = c.post("/api/npc/register", json={"persona": {"id": "ped_overflow"}})
        assert r.status_code == 429


class TestUnregister:
    def test_unregister_removes_everywhere(self, env):
        """反注册: 列表消失 + 世界 actor 槽删 + talk 404。"""
        c = env["client"]
        c.post("/api/npc/register", json={"persona": {"id": "ped_010"}})
        r = c.post("/api/npc/unregister", json={"npc_id": "ped_010"})
        assert r.status_code == 200 and r.json()["ok"]
        ids = [n["id"] for n in c.get("/api/npcs").json()["npcs"]]
        assert "ped_010" not in ids
        assert c.post("/api/talk",
                      json={"message": "在吗", "npc_id": "ped_010"}).status_code == 404

    def test_unregister_static_cast_refused(self, env):
        """cast 静态角色(cang)不可反注册 — 404。"""
        r = env["client"].post("/api/npc/unregister", json={"npc_id": "cang"})
        assert r.status_code == 404

    def test_unregister_unknown_404(self, env):
        assert env["client"].post("/api/npc/unregister",
                                  json={"npc_id": "nobody"}).status_code == 404

    def test_re_register_after_unregister_fresh(self, env):
        """流民反注册后再注册 → 全新身份(existed=False,不续前缘)。"""
        c = env["client"]
        c.post("/api/npc/register", json={"persona": {"id": "ped_020"}})
        c.post("/api/npc/unregister", json={"npc_id": "ped_020"})
        r = c.post("/api/npc/register", json={"persona": {"id": "ped_020"}})
        assert r.json()["existed"] is False


class TestEphemeralInternals:
    def test_ephemeral_save_noop(self, tmp_path):
        npc = NPC(persona={"id": "ped_x"}, store_dir=str(tmp_path), ephemeral=True)
        npc.save()
        assert not (tmp_path / "ped_x_memory.json").exists()

    def test_persistent_save_writes(self, tmp_path):
        npc = NPC(persona={"id": "ped_y"}, store_dir=str(tmp_path), ephemeral=False)
        npc.save()
        assert (tmp_path / "ped_y_memory.json").exists()

    def test_tick_round_skips_orphan_actor(self):
        """actor 槽在而 NPC 已反注册 → tick_round 跳过不炸(动态注册竞态防线)。"""
        w = default_world()
        actor_of(w, "ghost")   # 世界里有槽,npcs 里没有
        events = tick_round(w, {})   # 以前 KeyError,现在安全跳过
        assert events == {}
