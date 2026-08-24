"""动态资源词典（2026-08-23）— 游戏加新资源改世界 JSON 即生效,零代码。"""
import pytest

from npc.reviewer import (compile_task, get_manifest_resources,
                          load_resource_lexicon_from_world,
                          parse_manifest_doc, set_manifest_resources)


@pytest.fixture(autouse=True)
def _restore_default_lexicon():
    """每例后恢复默认词典, 不污染其他测试文件。"""
    yield
    load_resource_lexicon_from_world(None)
    set_manifest_resources(None)


def test_default_lexicon_unchanged():
    """未加载动态词典 → 行为与旧版完全一致（向后兼容回归）。"""
    assert compile_task("给我两根木材") == {"action": "gather", "resource": "木材", "count": 2}
    assert compile_task("去帮我砍点柴")["resource"] == "木材"


def test_dynamic_resource_from_world():
    """世界新资源自动进词典; 默认资源被整表替换（游戏真相优先）。"""
    world = {"locations": {"火山": {"resources": {"硫磺": 5}, "exits": []}}}
    lex = load_resource_lexicon_from_world(world)
    assert "硫磺" in lex
    assert compile_task("给我2个硫磺") == {
        "action": "gather", "resource": "硫磺", "count": 2}
    # 整表替换: 别游戏的"木材"不再误接（GTA 纯对话世界同理不接采集单）
    assert compile_task("给我两根木材") is None


def test_empty_world_falls_back_to_default():
    """纯对话世界(零资源) → 回退内置默认表（GTA 模式）。"""
    world = {"locations": {"罗克福山": {"resources": {}, "exits": []}}}
    load_resource_lexicon_from_world(world)
    assert compile_task("给我一根木材") is not None   # 回退默认


def test_aliases_from_world_extension_key():
    """world["_resource_aliases"] 给规范名补别名。"""
    world = {
        "locations": {"森林": {"resources": {"水晶": 3}, "exits": []}},
        "_resource_aliases": {"水晶": ["晶石", "亮石头"]},
    }
    load_resource_lexicon_from_world(world)
    assert compile_task("帮我弄点晶石")["resource"] == "水晶"
    assert compile_task("给我一块亮石头")["resource"] == "水晶"
    assert compile_task("弄点水晶")["resource"] == "水晶"


def test_manifest_resources_extra_merge():
    """清单 resources 段可给世界资源补别名, 也可声明世界里没有的规范名。"""
    world = {"locations": {"森林": {"resources": {"木材": 9}, "exits": []}}}
    load_resource_lexicon_from_world(world, extra={"木材": ["log", "原木"], "陨铁": ["天铁"]})
    assert compile_task("给我两根原木")["resource"] == "木材"
    assert compile_task("给我三块log")["resource"] == "木材"
    assert compile_task("给我一个陨铁")["resource"] == "陨铁"


def test_parse_manifest_doc_new_and_legacy():
    """正式模板(actions+resources)与旧版裸动作表都能解析。"""
    parsed = parse_manifest_doc({"actions": {"fly": {"tier": 1, "approval": "allow"}},
                                 "resources": {"羽毛": ["毛"]}})
    assert parsed["actions"]["fly"]["approval"] == "allow"
    assert parsed["resources"] == {"羽毛": ["毛"]}
    legacy = parse_manifest_doc({"dig": {"tier": 1, "approval": "deny"}})
    assert legacy["actions"] == {"dig": {"tier": 1, "approval": "deny"}}
    with pytest.raises(ValueError):
        parse_manifest_doc({"actions": {}})          # 空 actions
    with pytest.raises(ValueError):
        parse_manifest_doc({"foo": 1})               # 无法识别
