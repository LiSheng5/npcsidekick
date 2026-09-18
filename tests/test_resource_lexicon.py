"""动态资源词典（2026-08-23）— 游戏加新资源改世界 JSON 即生效,零代码。"""
import pytest

from npc.reviewer import (compile_task, get_manifest_resources,
                          load_resource_lexicon_from_world,
                          parse_manifest_doc, set_manifest_resources)
from npc.world import default_world


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


def test_empty_world_has_empty_lexicon():
    """纯对话世界(零资源) → **空词典**(态②) —— 不继承本游戏的资源词表。

    (2026-09-18) 原用例断言"回退内置默认表", 与项目"兜底三态"相悖: 别的游戏只要
    漏声明资源, 就会继承"木材/浆果/石头"。而本文件 test_dynamic_resource_from_world
    的注释原本就写着**相反意图**（"GTA 纯对话世界同理不接采集单"）—— 两条互相打架,
    现按三态约定收紧到这一条。
    """
    world = {"locations": {"某地标": {"resources": {}, "exits": []}}}
    assert load_resource_lexicon_from_world(world) == {}
    assert compile_task("给我一根木材") is None


def test_default_world_lexicon_keeps_aliases():
    """加载参考世界后, 同义说法仍接得住(态①) —— 堵"测试态看不见"的盲区。

    回归锚: 参考世界若不声明 `_resource_aliases`, 词典每条会只剩规范名自身 →
    "砍柴/木头" 这类同义说法全部接不住。而测试默认跑在"未加载世界"态(走含同义词的
    硬编码默认表), **恒绿看不见** —— 这个坑曾长期存在, 本条专门堵它。
    """
    lex = load_resource_lexicon_from_world(default_world())
    assert lex["木材"] == ("木材", "木头", "柴", "木", "树")
    assert compile_task("去帮我砍点柴")["resource"] == "木材"
    assert compile_task("给我两根木头")["resource"] == "木材"


def test_reference_lexicon_matches_undeclared():
    """态①(加载参考世界) 与态③(未加载) 行为等价 —— 钉住"参考世界声明完整"。

    参考世界的声明一旦缺键, 这条会立刻红, 不会像以前那样被"测试都在态③跑"掩盖。
    """
    def probe():
        return (compile_task("去帮我砍点柴"),
                compile_task("给我3个浆果"),
                compile_task("给我一块石头"))

    undeclared = probe()
    load_resource_lexicon_from_world(default_world())
    assert probe() == undeclared
    assert undeclared[0] is not None       # 别因为两边都失败而"等价"


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
