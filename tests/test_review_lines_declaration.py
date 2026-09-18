# -*- coding: utf-8 -*-
"""审查拒绝语声明化（2026-09-18）—— 引擎只留中性默认，台词归世界。

"资源会长回来"这类假设属于**世界**（参考世界声明了 `_resource_regen`），不属于引擎：
引擎的中性默认只说"弄不到了"，不替任何游戏断言资源会再生。
"""
from npc.reviewer import _DEFAULT_REVIEW_TPL, _review_line
from npc.world import default_world

# 参考世界的具体游戏内容 —— 中性默认里一个都不许出现
_GAME_WORDS = ("木材", "浆果", "石头", "村庄", "森林", "矿洞", "河边", "苍", "阿黎")


def test_neutral_default_has_no_game_terms():
    """未声明世界 → 中性默认**模板**里不含任何具体游戏名词。

    注意: 校验模板而非渲染结果 —— 渲染结果里的资源名来自调用方的参数，
    那是运行时数据（玩家/世界给的），不是引擎硬编码的游戏内容。
    """
    for key in ("disallowed", "forbidden", "exhausted"):
        tpl = _DEFAULT_REVIEW_TPL[key]
        assert tpl
        for word in _GAME_WORDS:
            assert word not in tpl, f"{key} 的中性默认模板混入了 {word}"
        # 用中性占位参数渲染，仍应成立
        assert _review_line({}, key, action="act", resource="res")


def test_neutral_default_keeps_generic_substring():
    """中性默认保留"弄不到"这个**通用中文**子串 —— 既有测试断言它。"""
    assert "弄不到" in _DEFAULT_REVIEW_TPL["exhausted"]


def test_neutral_default_has_no_regen_assumption():
    """引擎的中性默认不再替世界断言"资源会再生"。"""
    assert "长回来" not in _DEFAULT_REVIEW_TPL["exhausted"]


def test_review_tpl_override():
    """世界声明 `_review_tpl` → 生效（可换文案，甚至换语言）。"""
    world = {"_review_tpl": {"exhausted": "{resource} depleted, try later."}}
    assert _review_line(world, "exhausted", resource="iron") == "iron depleted, try later."


def test_forbidden_and_disallowed_override():
    """三个键都可各自覆盖。"""
    world = {"_review_tpl": {"forbidden": "no", "disallowed": "nope:{action}"}}
    assert _review_line(world, "forbidden") == "no"
    assert _review_line(world, "disallowed", action="fly") == "nope:fly"


def test_broken_tpl_falls_back_to_neutral():
    """模板占位符写错 → 回落中性默认，绝不让审查本身失败。"""
    world = {"_review_tpl": {"exhausted": "只有 {nope} 能填"}}
    text = _review_line(world, "exhausted", resource="木材")
    assert text == _DEFAULT_REVIEW_TPL["exhausted"].format(resource="木材")


def test_default_world_declares_all_three_keys():
    """参考世界声明全部三个键（否则参考世界会退化成中性文案）。"""
    tpl = default_world()["_review_tpl"]
    assert set(tpl) == {"disallowed", "forbidden", "exhausted"}
    # 参考世界确实有资源再生（_resource_regen=1）→ 它的措辞可以提再生
    assert "长回来" in tpl["exhausted"]


def test_missing_key_on_loaded_world_falls_back_to_neutral():
    """世界声明了 `_review_tpl` 但缺某个键 → 该键用中性默认（不炸）。"""
    world = {"_review_tpl": {"forbidden": "x"}}
    text = _review_line(world, "exhausted", resource="木材")
    assert text == _DEFAULT_REVIEW_TPL["exhausted"].format(resource="木材")
