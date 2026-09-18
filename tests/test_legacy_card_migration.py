# -*- coding: utf-8 -*-
"""老存档声明键迁移（2026-09-18）。

声明机制引入前的记忆卡里没有 `_resource_regen` / `_entity_synonyms` 等键；
而新机制按"世界已加载但未声明 → 空"处理（三态约定）。若不迁移，老档会静默
失去资源回补与同义召回 = 回归。`_migrate_world_declarations` 负责补齐。
"""
from npc.server import _MIGRATED_DECL_KEYS, _migrate_world_declarations
from npc.world import default_world


def test_legacy_world_gets_all_declaration_keys():
    """缺键的老世界 → 迁移补上全部声明键（值 = 参考默认 = 旧行为）。"""
    legacy = {"_tick": 3, "actors": {}, "locations": {}, "log": []}
    filled = _migrate_world_declarations(legacy)
    assert set(filled) == set(_MIGRATED_DECL_KEYS)
    assert legacy["_resource_regen"] == 1
    assert len(legacy["_entity_synonyms"]) == 12
    assert legacy["_entity_single_chars"]
    assert legacy["_weather_text"]
    assert legacy["_review_tpl"]


def test_legacy_world_keeps_old_regen_behavior():
    """迁移后老档仍按旧行为回补（这是迁移存在的全部理由）。"""
    from npc.scheduler import tick_round
    import random

    legacy = {"_tick": 0, "_resource_caps": {"木材": 9}, "actors": {},
              "protagonist": {"position": "村庄", "name": "主角"},
              "locations": {"森林": {"desc": "x", "resources": {"木材": 0},
                                     "exits": ["村庄"]}},
              "exits": {}, "log": []}
    _migrate_world_declarations(legacy)
    tick_round(legacy, {}, rng=random.Random(0))
    assert legacy["locations"]["森林"]["resources"]["木材"] == 1


def test_declared_keys_are_never_overwritten():
    """世界已声明（哪怕声明成 0 / 空表）→ 一律不覆盖，尊重制作者意图。"""
    world = {
        "_resource_regen": 0,
        "_entity_synonyms": {},
        "_entity_single_chars": [],
        "_weather_text": {},
        "_review_tpl": {"exhausted": "x"},
    }
    filled = _migrate_world_declarations(world)
    assert filled == []
    assert world["_resource_regen"] == 0
    assert world["_entity_synonyms"] == {}
    assert world["_weather_text"] == {}
    assert world["_review_tpl"] == {"exhausted": "x"}


def test_migration_is_idempotent():
    """幂等: 连跑两次，第二次无新补（重启重算也不重复注入）。"""
    legacy = {"_tick": 0, "locations": {}}
    _migrate_world_declarations(legacy)
    assert _migrate_world_declarations(legacy) == []


def test_fresh_world_needs_no_migration():
    """`default_world()` 自带全部声明 → 迁移是 no-op。"""
    assert _migrate_world_declarations(default_world()) == []
