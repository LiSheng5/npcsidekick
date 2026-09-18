# -*- coding: utf-8 -*-
"""兜底三态（2026-09-18）—— 本项目"声明驱动"机制的核心约定。

  ① 世界已声明        → 用声明值
  ② 世界已加载但未声明 → **空 / 中性**（制作者的沉默 = 不要我的内容）
  ③ 未加载任何世界     → 参考默认（保裸调用与既有测试）

态②是"别人拿这个引擎接自己的游戏"能成立的唯一前提：没有它，"可覆盖"是假的 ——
别的游戏漏声明一个键，就会从兜底继承本游戏的资源名/地名，污染它的检索与行为。

本文件用同义词表做代表来钉这三态（资源回补、审查台词、天气文案同款，各自的
专项锚在 test_regen_declaration / test_review_lines_declaration 里）。
"""
import pytest

from npc.memory import (_ENTITY_SYNONYMS, _canonical_terms,
                        load_entity_synonyms_from_world)


@pytest.fixture(autouse=True)
def _reset_synonyms():
    """每例前后复位到"未加载"态 —— 模块全局必须还原，防污染其他测试文件。"""
    load_entity_synonyms_from_world(None)
    yield
    load_entity_synonyms_from_world(None)


def test_no_world_loaded_uses_reference():
    """态③: 未加载世界 → 回落参考默认（既有行为不变）。"""
    assert _canonical_terms("去砍柴") == {"木材"}
    assert _canonical_terms("摘了一把野果") == {"浆果"}


def test_world_declaration_wins():
    """态①: 世界声明自己的词表 → 用声明值，参考词表整表让位。"""
    load_entity_synonyms_from_world({
        "_entity_synonyms": {"硫磺": ["硫磺", "磺石"]},
        "_entity_single_chars": [],
    })
    assert _canonical_terms("去挖点磺石") == {"硫磺"}
    assert _canonical_terms("去砍柴") == set()          # 参考词表不再命中


def test_world_loaded_but_omits_key_is_empty():
    """态②: 世界已加载但没声明该键 → 空表，**不**回落参考默认。"""
    load_entity_synonyms_from_world({"locations": {}})
    assert _canonical_terms("去砍柴") == set()
    assert _canonical_terms("森林里的木材很粗") == set()


def test_reset_back_to_undeclared_state():
    """显式 `world=None` → 复位回态③（测试复位用，机制可逆）。"""
    load_entity_synonyms_from_world({"locations": {}})
    assert _canonical_terms("去砍柴") == set()
    load_entity_synonyms_from_world(None)
    assert _canonical_terms("去砍柴") == {"木材"}


def test_reference_table_admits_it_is_only_a_fallback():
    """反向锚: 参考默认表存在，但只是态③兜底 —— 真相在世界声明里。"""
    assert "木材" in _ENTITY_SYNONYMS
    load_entity_synonyms_from_world({"locations": {}})
    assert _canonical_terms("木材") == set()
