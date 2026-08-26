"""模型可插拔·分槽修复测试（§5）— dialogue/review 异款模型各用各的，同款共用。

背景: 此前 _get_llm 单槽缓存 self._llm，第一个调用者的模型被所有角色共用，
NPC_REVIEW_MODEL 配了也不生效。修复后: 配不同模型 → 双槽；同款/未配 → 共用
对话槽（老测试注入 npc._llm 即对全角色生效的语义保持不变）。
"""
import pytest

import npc.llm_wiring as wiring_mod
import npc.npc as npc_mod
from npc.npc import NPC


def _patch_builder(monkeypatch, calls):
    """P2 拆分序①接缝迁移: 装配已迁 llm_wiring —— 补丁点随之改为 build_client。"""
    from types import SimpleNamespace

    def fake_build(api_key, model_name, base_url=""):
        calls.append(model_name)
        return SimpleNamespace(model_name=model_name)   # 哨兵客户端: 验槽位身份，不发请求
    monkeypatch.setattr(wiring_mod, "build_client", fake_build)


def _npc() -> NPC:
    return NPC(store_dir="npc/store_test")


class TestSplitSlots:
    def test_different_models_get_separate_clients(self, monkeypatch):
        """配异款 → B1 用对话档、A/反思用 review 档，互不串台。"""
        calls = []
        _patch_builder(monkeypatch, calls)
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("NPC_DIALOGUE_MODEL", "model-mouth")
        monkeypatch.setenv("NPC_REVIEW_MODEL", "model-gate")
        npc = _npc()
        d = npc._get_llm(role="dialogue")
        r = npc._get_llm(role="review")
        assert set(calls) == {"model-mouth", "model-gate"}
        assert d is not r   # 修复点: 不再一个槽位定终身

    def test_review_channel_serves_reflection(self, monkeypatch):
        """maybe_reflect 走 role='review' — 反思质量跟闸门档模型走。"""
        calls = []
        _patch_builder(monkeypatch, calls)
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("NPC_DIALOGUE_MODEL", "model-mouth")
        monkeypatch.setenv("NPC_REVIEW_MODEL", "model-gate")
        npc = _npc()
        npc._get_llm(role="review")
        assert calls == ["model-gate"]

    def test_same_model_shares_dialogue_slot(self, monkeypatch):
        """配同款/未配 review → 共用一个客户端（兼容语义: 注入 npc._llm 全局生效）。"""
        calls = []
        _patch_builder(monkeypatch, calls)
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.delenv("NPC_DIALOGUE_MODEL", raising=False)
        monkeypatch.delenv("NPC_REVIEW_MODEL", raising=False)
        npc = _npc()
        assert npc._get_llm() is npc._get_llm(role="review")
        assert calls == ["deepseek-v4-flash"]   # 只建了一个

    def test_same_role_returns_cached_client(self, monkeypatch):
        calls = []
        _patch_builder(monkeypatch, calls)
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("NPC_DIALOGUE_MODEL", "model-mouth")
        monkeypatch.setenv("NPC_REVIEW_MODEL", "model-gate")
        npc = _npc()
        assert npc._get_llm() is npc._get_llm()
        assert npc._get_llm(role="review") is npc._get_llm(role="review")

    def test_default_role_is_dialogue(self, monkeypatch):
        calls = []
        _patch_builder(monkeypatch, calls)
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("NPC_DIALOGUE_MODEL", "model-mouth")
        monkeypatch.delenv("NPC_REVIEW_MODEL", raising=False)
        npc = _npc()
        npc._get_llm()
        assert calls == ["model-mouth"]   # 缺省 role=dialogue

    def test_no_key_still_falls_back_to_none(self, monkeypatch):
        """无 key → None → 零惩罚规则回退不受分槽影响。"""
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setenv("NPC_DIALOGUE_MODEL", "deepseek-v4-flash")
        monkeypatch.setattr(wiring_mod, "resolve_api_key", lambda model_name: "")
        assert _npc()._get_llm() is None
