# -*- coding: utf-8 -*-
"""同义词表声明化（2026-09-18）—— 引擎层不持有游戏词表。

世界用 `_entity_synonyms` / `_entity_single_chars` 声明检索召回用的词表。
参考世界声明一份与旧 `_ENTITY_SYNONYMS` **逐字节等价**的表 —— 这是零回归锚：
加载参考世界后，`_canonical_terms` 的行为与"未加载世界"时完全一致。
"""
import pytest

from npc.memory import (_ENTITY_SYNONYMS, _SINGLE_CHAR_TERMS, _canonical_terms,
                        load_entity_synonyms_from_world)
from npc.world import default_world


@pytest.fixture(autouse=True)
def _reset():
    """每例前后复位"未加载"态，防模块全局污染其他测试。"""
    load_entity_synonyms_from_world(None)
    yield
    load_entity_synonyms_from_world(None)


def test_default_world_declares_12_groups():
    """参考世界声明 12 组同义词族。"""
    assert len(default_world()["_entity_synonyms"]) == 12


def test_declaration_equals_engine_reference_table():
    """逐字节等价锚: 世界声明 == 引擎参考默认表（零回归的根本保证）。"""
    declared = {k: frozenset(v) for k, v in default_world()["_entity_synonyms"].items()}
    assert declared == dict(_ENTITY_SYNONYMS)


def test_declaration_equals_reference_single_chars():
    """单字白名单同样等价。"""
    assert frozenset(default_world()["_entity_single_chars"]) == _SINGLE_CHAR_TERMS


def test_loading_default_world_matches_undeclared_behavior():
    """加载参考世界声明后，行为与态③（未加载）逐字节一致。"""
    before = _canonical_terms("去砍柴")
    load_entity_synonyms_from_world(default_world())
    assert before == {"木材"}
    assert _canonical_terms("去砍柴") == before
    assert _canonical_terms("摘了一把野果") == {"浆果"}
    assert _canonical_terms("矿洞里的岩石") == {"石头", "矿洞"}
    assert _canonical_terms("今天天气不错") == set()


def test_world_without_declaration_has_no_synonyms():
    """世界已加载但未声明 → 空表（不继承参考词表）。"""
    load_entity_synonyms_from_world({"locations": {}})
    assert _canonical_terms("去砍柴") == set()


def test_bad_declaration_types_are_ignored():
    """声明写坏（值不是序列）→ 跳过该项，不抛异常。"""
    load_entity_synonyms_from_world({"_entity_synonyms": {"木材": "不是列表"}})
    assert _canonical_terms("去砍柴") == set()
    assert _canonical_terms("木材") == set()


def test_declared_aliases_apply():
    """世界声明的别名真实生效。"""
    load_entity_synonyms_from_world({
        "_entity_synonyms": {"水晶": ["水晶", "晶石"]},
        "_entity_single_chars": [],
    })
    assert _canonical_terms("帮我弄点晶石") == {"水晶"}
