"""卫生债 T-03~T-08 回归锚（2026-09-16）。

《NPC大脑架构》§30.1 顺手项 / 审查报告 S1/S4/S2/S11。每条都钉"修好之后的行为"，不钉实现细节：
  T-03 引擎不再硬编码游戏地名 —— 制作台地点由**世界声明** `craft_at` 决定（未声明 = 就地制作）
  T-04 scheduler 的 routine **真的能 craft**（旧版一律 return None，人设写了也永远不做）
  T-05 `_reflect_rules` 只有**一处实现**（npc.py 的重定义已删，导入即唯一实现）
  T-06 `_a_semantic_block` 保持封存：**无任何调用点**（谁接回去这里会红）
  T-07 画像读取 / 商议队列的失败**不再静默**（留痕但仍 fail-soft，不卡对话）
  T-08 `reviewer.py` 头注与 `subagent.py` 对齐（A 审查已退役 · §22）
"""
import random
from pathlib import Path

from npc.npc import NPC
from npc.scheduler import _plan_steps, tick_round
from npc.world import actor_of, apply_action, craft_station, default_world

REPO = Path(__file__).resolve().parents[1]
NPC_DIR = REPO / "npc"


def _world(locations, craft_at=None, protagonist_pos=None):
    """造一个和示例世界无关的小世界（地点名全是虚构的，用来证明引擎不含游戏词）。"""
    first = next(iter(locations))
    world = {
        "_tick": 0,
        "actors": {},
        "log": [],
        "protagonist": {"position": protagonist_pos or first, "name": "主角"},
        "locations": locations,
        "recipes": {"工具": {"木头": 1, "produces": "工具"}},
    }
    if craft_at is not None:
        world["craft_at"] = craft_at
    return world


FARM = "田野"
SHOP = "工坊"


def _npc(world, routine=None, inv=None, pos=None, pid="t"):
    persona = {"id": pid, "name": pid, "identity": "t", "personality": "t",
               "speech_style": "t", "taboos": [],
               "rules": {"replies": {}, "fallback": "嗯。"},
               "routine": routine or [], "goals": {}}
    npc = NPC(persona=persona, world=world, store_dir="npc/store_test")
    if pos:
        world["actors"][pid]["position"] = pos
    if inv:
        world["actors"][pid]["inventory"].update(inv)
    return npc


class TestT03NoHardcodedPlace:
    """T-03：制作台地点 = 世界声明；引擎不认识任何具体地名。"""

    def test_default_world_declares_station(self):
        w = default_world()
        assert craft_station(w) == w["craft_at"], "示例世界用声明表达'在哪制作'"
        # 行为保持：站在别处做不了、回到声明地点才行
        w["actors"]["cang"] = {"position": "森林", "inventory": {"木材": 2}}
        _, ok, msg = apply_action(w, "craft", {"recipe": "木石工具"}, who="cang")
        assert ok is False and w["craft_at"] in msg

    def test_custom_world_uses_its_own_station(self):
        """换个世界（地点名虚构）→ 规则跟着声明走，而不是跟着'村庄'。"""
        locs = {SHOP: {"resources": {}, "exits": [FARM]}, FARM: {"resources": {"木头": 9}, "exits": [SHOP]}}
        w = _world(locs, craft_at=SHOP)
        assert craft_station(w) == SHOP
        _npc(w, inv={"木头": 1}, pos=FARM)
        _, ok, msg = apply_action(w, "craft", {"recipe": "工具"}, who="t")
        assert ok is False and SHOP in msg, "不该出现'村庄'—— 规则读的是声明"
        w["actors"]["t"]["position"] = SHOP
        _, ok2, _ = apply_action(w, "craft", {"recipe": "工具"}, who="t")
        assert ok2 is True

    def test_no_declaration_means_craft_anywhere(self):
        locs = {FARM: {"resources": {}, "exits": []}}
        w = _world(locs)                     # 没声明 craft_at
        assert craft_station(w) is None
        _npc(w, inv={"木头": 1}, pos=FARM)
        _, ok, _ = apply_action(w, "craft", {"recipe": "工具"}, who="t")
        assert ok is True, "未声明 = 就地制作（引擎不替世界做假设）"

    def test_invalid_declaration_is_ignored(self):
        locs = {FARM: {"resources": {}, "exits": []}}
        w = _world(locs, craft_at="不存在的地方")
        assert craft_station(w) is None

    def test_spawn_falls_back_without_hardcoded_name(self):
        locs = {FARM: {"resources": {}, "exits": []}, SHOP: {"resources": {}, "exits": []}}
        w = _world(locs)                     # 没声明 _default_spawn
        assert actor_of(w, "new")["position"] == FARM, "兜底 = 第一个地点，不写死地名"

    def test_engine_source_has_no_village_walk(self):
        """源码级锚：引擎不再出现 walk_to("村庄") 这种硬编码。"""
        src = (NPC_DIR / "npc.py").read_text(encoding="utf-8")
        assert 'walk_to("村庄")' not in src


class TestT04RoutineCanCraft:
    """T-04：routine 里的 craft 项现在真的会被规划并执行。"""

    def _craft_world(self):
        locs = {SHOP: {"resources": {}, "exits": []}}
        w = _world(locs, craft_at=SHOP)
        return w

    def test_plan_steps_supports_craft(self):
        w = self._craft_world()
        npc = _npc(w, inv={"木头": 1}, pos=SHOP)
        steps = _plan_steps(npc, w, {"action": "craft", "recipe": "工具", "count": 1})
        assert steps is not None, "T-04 之前这里恒为 None（craft 永远做不了）"
        assert {"kind": "craft", "recipe": "工具"} in steps

    def test_plan_steps_rejects_unknown_recipe(self):
        w = self._craft_world()
        npc = _npc(w, inv={}, pos=SHOP)
        assert _plan_steps(npc, w, {"action": "craft", "recipe": "不存在的配方"}) is None

    def test_plan_walks_to_declared_station(self):
        locs = {SHOP: {"resources": {}, "exits": [FARM]}, FARM: {"resources": {}, "exits": [SHOP]}}
        w = _world(locs, craft_at=SHOP)
        npc = _npc(w, inv={"木头": 1}, pos=FARM)
        steps = _plan_steps(npc, w, {"action": "craft", "recipe": "工具"})
        assert steps and steps[0]["kind"] == "walk" and steps[0]["target"] == SHOP

    def test_end_to_end_crafts_for_real(self):
        """终极证据：人设写了 craft → 跑几帧 → 背包里**真的出现了产物**。"""
        w = self._craft_world()
        npc = _npc(w, routine=[{"action": "craft", "recipe": "工具", "count": 1, "weight": 1}],
                   inv={"木头": 1}, pos=SHOP)
        for _ in range(4):
            tick_round(w, {"t": npc}, rng=random.Random(1))
        assert w["actors"]["t"]["inventory"].get("工具", 0) == 1
        # 证据看**世界日志**（自主循环不走 run_task，所以不写 npc.task_log）
        assert any("制作" in line for line in w["log"]), f"日志里该有制作记录：{w['log']}"


class TestT05SingleReflectionRules:
    """T-05：`_reflect_rules` 只有一处实现。"""

    def test_same_object_as_memory_card(self):
        from npc.memory_card import _reflect_rules as canonical
        from npc.npc import _reflect_rules
        assert _reflect_rules is canonical, "npc.py 的重定义应已删除（保留导入）"

    def test_only_one_definition_in_source(self):
        src = (NPC_DIR / "npc.py").read_text(encoding="utf-8")
        assert src.count("def _reflect_rules(") == 0


class TestT06SealedMethodStillSealed:
    """T-06：封存方法保持"无调用点"（乙案：留着做 A/B，但生产不经过）。"""

    def test_no_call_sites_in_production(self):
        """只看**生产代码**（）里的调用点；剥掉 \`#\` 注释再判。

        tests/ 里直接调它是**正当的**（ 就是封存机器的单测），
        所以不扫测试目录。
        """
        hits = []
        for f in NPC_DIR.rglob("*.py"):
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if "_a_semantic_block(" in code and not code.strip().startswith("def "):
                    hits.append(f"{f.name}:{i}")
        assert hits == [], f"有人把它接回生产路径了：{hits}"

    def test_docstring_says_sealed(self):
        src = (NPC_DIR / "npc.py").read_text(encoding="utf-8")
        assert "乙案封存" in src and "无调用点" in src


class TestT07SwallowedExceptionsLeaveATrace:
    """T-07：fail-soft 保留，但**不再静默**。"""

    def test_profile_read_failure_is_logged(self, monkeypatch, caplog):
        import logging as _logging
        w = default_world()
        npc = _npc(w)

        def _boom(*a, **kw):
            raise RuntimeError("画像层炸了")

        monkeypatch.setattr(type(npc), "read_persona_profile", _boom, raising=False)
        with caplog.at_level(_logging.WARNING):
            ctx = npc._build_context("你好")      # 契约：不抛异常（fail-soft）
        assert isinstance(ctx, str) and ctx
        assert "npc_profile_read_failed" in caplog.text,             "画像读取失败必须留痕（T-07 之前是静默 pass）"


class TestT08HeaderSynced:
    """T-08：reviewer.py 头注与 subagent.py 的退役说明对齐。"""

    def test_header_marks_a_retired(self):
        src = (NPC_DIR / "reviewer.py").read_text(encoding="utf-8")
        head = src[:1200]
        assert "已退役" in head and "§22" in head
        assert "三角色: Agent A 审查管线" not in head, "旧措辞应已改掉"
