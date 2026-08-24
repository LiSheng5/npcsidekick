"""NPC 运行时测试 — 任务循环、受阻处理、记忆持久化。"""
import pytest

from npc.npc import NPC
from npc.persona import SAMPLE_NPCS
from npc.world import default_world


@pytest.fixture
def npc():
    return NPC(store_dir="npc/store_test")


class TestTaskLoop:
    def test_single_gather(self, npc):
        assert npc.run_task([{"type": "gather", "resource": "木材", "count": 1}])
        assert npc.world["actors"]["cang"]["inventory"]["木材"] == 1

    def test_multi_gather(self, npc):
        assert npc.run_task([{"type": "gather", "resource": "木材", "count": 3}])
        assert npc.world["actors"]["cang"]["inventory"]["木材"] == 3

    def test_gather_deliver(self, npc):
        assert npc.run_task([
            {"type": "gather", "resource": "木材", "count": 2},
            {"type": "deliver", "resource": "木材", "count": 2},
        ])
        assert npc.world["delivered"]["木材"] == 2
        assert npc.world["actors"]["cang"]["inventory"].get("木材", 0) == 0

    def test_full_chain_with_craft(self, npc):
        assert npc.run_task([
            {"type": "gather", "resource": "木材", "count": 2},
            {"type": "gather", "resource": "石头", "count": 1},
            {"type": "craft", "recipe": "木石工具", "count": 1},
            {"type": "deliver", "resource": "木石工具", "count": 1},
        ])
        assert npc.world["delivered"]["木石工具"] == 1

    def test_deliver_uses_baseline_not_cumulative(self, npc):
        """delivered 是累计计数 — 第二次派发任务必须从基线算增量,不能空手成功(回归: H3)。"""
        npc.world["delivered"]["木材"] = 5   # 村里已累计交付过
        assert npc.run_task([
            {"type": "gather", "resource": "木材", "count": 2},
            {"type": "deliver", "resource": "木材", "count": 2},
        ])
        assert npc.world["delivered"]["木材"] == 7


class TestObstruction:
    """受阻任务 → 优雅失败（有报告、不崩溃、状态一致）。"""

    def test_missing_resource_fails_gracefully(self, npc):
        assert not npc.run_task([{"type": "gather", "resource": "铁矿石", "count": 3}])
        assert any("失败" in s for s in npc.task_log[-1]["steps"])
        assert npc.world["actors"]["cang"]["position"] == "村庄"  # 状态未损坏

    def test_craft_without_ingredients_fails(self, npc):
        assert not npc.run_task([{"type": "craft", "recipe": "木石工具", "count": 1}])

    def test_unknown_step_type_fails(self, npc):
        assert not npc.run_task([{"type": "fly", "resource": "木材", "count": 1}])

    def test_resource_exhaustion_reports(self, npc):
        # 森林木材只够 2 个 → 任务要 3 → 失败报告
        npc.world["locations"]["森林"]["resources"]["木材"] = 2
        assert not npc.run_task([{"type": "gather", "resource": "木材", "count": 3}])
        assert any("采尽" in s for s in npc.task_log[-1]["steps"])


class TestRulesDialogue:
    """规则模式对话 = 制作者可配置（人设 rules 字段，不用写代码）。"""

    def test_default_rules_from_persona(self, npc):
        npc.use_llm = False
        assert "别追跑得快的" in npc.talk("聊聊狩猎的事")   # 苍的规则回复

    def test_custom_rules_in_persona(self):
        custom = NPC(persona={
            "id": "laoli",
            "name": "老李",
            "identity": "铁匠",
            "personality": "沉默寡言",
            "speech_style": "惜字如金",
            "taboos": [],
            "rules": {"replies": {"铁": "好铁出好刀。", "酒": "收摊再喝。"}, "fallback": "嗯。"},
        }, store_dir="npc/store_test")
        custom.use_llm = False
        assert "好铁出好刀" in custom.talk("打把铁剑")
        assert "嗯。" == custom.talk("今天天气不错")

    def test_fallback_when_no_match(self, npc):
        npc.use_llm = False
        assert npc.talk("今天天气不错") == "嗯，火塘边坐着说。"   # 苍的 fallback


class TestSharedWorld:
    """多 NPC 共享一个世界（AI Town 模式）— 各自行动互不干扰。"""

    def test_two_npcs_share_world(self):
        world = default_world()
        cang = NPC(persona=SAMPLE_NPCS["cang"], world=world, store_dir="npc/store_test")
        ali = NPC(persona=SAMPLE_NPCS["ali"], world=world, store_dir="npc/store_test")
        assert cang.world is ali.world  # 同一引用
        assert set(world["actors"].keys()) == {"cang", "ali"}

    def test_actions_isolated_per_actor(self):
        world = default_world()
        cang = NPC(persona=SAMPLE_NPCS["cang"], world=world, store_dir="npc/store_test")
        ali = NPC(persona=SAMPLE_NPCS["ali"], world=world, store_dir="npc/store_test")
        cang.run_gather_task("木材", 2)
        # 苍在森林, 阿黎还在村庄, 背包互不影响
        assert cang.actor_pos == "村庄"      # 交付后回村
        assert ali.actor_pos == "村庄"
        assert world["actors"]["ali"]["inventory"] == {}
        assert world["actors"]["cang"]["inventory"].get("木材", 0) == 0  # 已交付

    def test_observe_shows_other_npc(self):
        world = default_world()
        NPC(persona=SAMPLE_NPCS["cang"], world=world, store_dir="npc/store_test")
        ali = NPC(persona=SAMPLE_NPCS["ali"], world=world, store_dir="npc/store_test")
        text = ali.observe()
        # 世界层显示角色 id（名字装饰是 v1 限制，留给上层）
        assert "cang" in text and "也在附近" in text


class TestMakerExtensions:
    """制作者扩展口: 自定义提示词覆盖 + 自定义状态展示（好感度/情绪等）。"""

    def test_system_prompt_override(self):
        npc = NPC(persona={
            "id": "p1", "name": "测试", "identity": "x", "personality": "y",
            "speech_style": "z", "taboos": [],
            "system_prompt_override": "你是主角的挚友，说话用古风。",
        }, store_dir="npc/store_test")
        assert npc.system_prompt == "你是主角的挚友，说话用古风。"

    def test_context_extra_shows_custom_state(self):
        npc = NPC(persona={
            "id": "p2", "name": "测试", "identity": "x", "personality": "y",
            "speech_style": "z", "taboos": [],
            "context_extra": ["你对主角的好感度: 52（信任）", "当前情绪: 高兴"],
        }, store_dir="npc/store_test")
        ctx = npc._build_context("你好")
        assert "好感度: 52" in ctx
        assert "当前情绪: 高兴" in ctx

    def test_affinity_via_memory_reaches_context(self):
        """好感度走记忆路径: 变化记入记忆 → LLM 对话自动感知。"""
        npc = NPC(store_dir="npc/store_test")
        npc.remember("主角帮我修了屋顶，好感度 +5，现在 52", importance=7)
        ctx = npc._build_context("你对我印象怎么样")
        assert "好感度" in ctx


class TestPersistence:
    def test_save_load_roundtrip(self, npc):
        npc.run_task([{"type": "gather", "resource": "木材", "count": 1}])
        npc.save()
        restored = NPC.load("cang", store_dir="npc/store_test")
        assert restored.world["actors"]["cang"]["inventory"]["木材"] == 1
        assert restored.memory.all()[-1]["content"].startswith("完成:")
        assert restored.task_log[-1]["task"] == "gather(木材×1)"

    def test_memory_importance_recorded(self, npc):
        npc.run_task([{"type": "gather", "resource": "木材", "count": 1}])
        entries = npc.memory.all()
        assert entries[0]["importance"] == 6   # 接到任务
        assert entries[-1]["importance"] == 8  # 完成

    def test_activity_not_persisted(self, npc):
        """自主活动是运行时状态 — save/load 后回到 idle（重启 = 重新规划）。"""
        npc.activity = {"item": {"action": "rest"}, "steps": [{"kind": "rest"}], "desc": "休息2刻"}
        npc.state = "resting"
        npc.save()
        restored = NPC.load("cang", store_dir="npc/store_test")
        assert restored.activity is None
        assert restored.state == "idle"


class TestTickState:
    """自主循环的运行时状态（state/activity/_blocked）。"""

    def test_default_state(self, npc):
        assert npc.state == "idle"
        assert npc.activity is None
        assert npc.activity_desc() == ""

    def test_activity_desc_mid_activity(self, npc):
        npc.activity = {"item": {"action": "gather", "resource": "木材", "count": 2},
                        "steps": [{"kind": "walk"}, {"kind": "gather"}], "desc": "采集木材×2"}
        assert "采集木材×2" in npc.activity_desc()
        assert "剩 2 步" in npc.activity_desc()

    def test_run_task_cancels_activity(self, npc):
        """玩家派活打断自主日常（优先级冲突 = 用户选）。"""
        npc.activity = {"item": {"action": "rest"}, "steps": [], "desc": "休息"}
        npc.state = "resting"
        assert npc.run_task([{"type": "gather", "resource": "木材", "count": 1}])
        assert npc.activity is None
        assert npc.state == "idle"

    def test_repr_no_keyerror(self, npc):
        """__repr__ 不再 KeyError（世界用 actors 键，曾误用 npc 键）。"""
        assert "苍" in repr(npc)
        assert "村庄" in repr(npc)


class TestRecallAndBehaviorLog:
    """防"文字游戏": 扩回忆关键词 + 行为日志直供（LLM 只准复述事实）。"""

    def test_recall_keywords_expanded(self, npc):
        """口语化问法（干嘛/忙什么/做了什么）也走回忆快路径，不进 LLM 不编造。"""
        npc.use_llm = False
        for q in ("你最近在干嘛", "这两天忙什么", "你今天做了什么"):
            reply = npc.talk(q)
            assert "记性不太好" in reply   # 记忆卡直答（无条目 → 明说不记得）

    def test_recall_returns_memory_verbatim(self, npc):
        npc.use_llm = False
        npc.remember("昨天给营地送了两根木材")
        reply = npc.talk("你昨天干嘛了")
        assert "送了两根木材" in reply      # 逐字回答，不是 LLM 润色

    def test_context_has_behavior_log(self, npc):
        """干过的实事进 LLM 上下文（事实源），没干过的不在里面。"""
        npc.run_task([{"type": "gather", "resource": "木材", "count": 1}])
        ctx = npc._build_context("你最近怎么样")
        assert "【行为日志】" in ctx
        assert "采集了 1 个木材" in ctx
        assert "（最近没干什么）" not in ctx


class TestDialogueHistory:
    """短期对话历史: 村民记得上一条聊了什么（最近 N 轮，不落盘不进记忆卡）。"""

    class _FakeLLM:
        def __init__(self):
            self.captured = []

        def chat(self, messages, **kwargs):
            self.captured.append(messages)
            return type("R", (), {"content": "嗯，记下了。"})()

    def _attach(self, npc) -> "_FakeLLM":
        fake = self._FakeLLM()
        npc.use_llm = True
        npc._llm = fake
        return fake

    def test_previous_turns_fed_to_next(self, npc):
        """第二轮对话里带着第一轮的 user/assistant 历史。"""
        fake = self._attach(npc)
        npc.talk("我叫阿花")
        npc.talk("我叫什么")
        hist = [m for m in fake.captured[1] if m["role"] != "system"]
        assert hist[0] == {"role": "user", "content": "我叫阿花"}
        assert hist[1] == {"role": "assistant", "content": "嗯，记下了。"}
        assert hist[2] == {"role": "user", "content": "我叫什么"}

    def test_history_truncated_to_recent_turns(self, npc):
        """超过 5 轮只留最近 5 轮（上下文定期重置，不无限膨胀）。"""
        fake = self._attach(npc)
        for i in range(8):
            npc.talk("第%d句" % i)
        hist = [m for m in fake.captured[-1] if m["role"] != "system"]
        assert len(hist) == 11                      # 5 轮历史(10 条) + 当前这句
        assert hist[0] == {"role": "user", "content": "第2句"}   # 第 0-1 轮已被截断

    def test_rules_mode_no_history(self, npc):
        """规则模式（无 LLM）不记历史 — 确定性对话本来就是无状态的。"""
        npc.use_llm = False
        npc.talk("你好")
        npc.talk("吃了吗")
        assert npc.dialogue_history == []

    def test_history_not_persisted(self, npc):
        """对话历史不落盘（防污染记忆卡；重启 = 对话清零，重要的事在记忆里）。"""
        fake = self._attach(npc)
        npc.talk("你好")
        npc.save()
        restored = NPC.load("cang", store_dir="npc/store_test")
        assert restored.dialogue_history == []


class TestTaskCommand:
    """对话下指令: "给我两根木材" → 接单，tick 循环真去干（零 LLM 规则快路径）。"""

    def test_talk_command_accepts(self, npc):
        npc.use_llm = False
        reply = npc.talk("给我两根木材")
        assert "好" in reply and "木材" in reply
        assert npc.pending_task == {"action": "gather", "resource": "木材", "count": 2}

    def test_aliases_and_counts(self, npc):
        npc.use_llm = False
        npc.talk("去帮我砍点柴")
        assert npc.pending_task["resource"] == "木材"
        npc.talk("给我三个浆果")
        assert npc.pending_task == {"action": "gather", "resource": "浆果", "count": 3}

    def test_no_command_without_resource(self, npc):
        """没提资源 → 不接单（不误接"我要去散步"）。"""
        npc.use_llm = False
        npc.talk("我要去散步")
        assert npc.pending_task is None

    def test_declines_when_resource_exhausted(self, npc):
        """全世界采空了 → 诚实拒绝，不空口答应（接了却不做=看着像坏了）。"""
        npc.use_llm = False
        for loc in npc.world["locations"].values():
            for res in loc.get("resources", {}):
                loc["resources"][res] = 0
        reply = npc.talk("给我两根木材")
        assert "弄不到" in reply
        assert npc.pending_task is None

    def test_command_executed_by_ticks(self, npc):
        """接单后 tick 循环一步步执行 → 木材交付（村民真去干活）。"""
        from npc.scheduler import tick_round

        npc.use_llm = False
        npc.talk("给我两根木材")
        npcs = {"cang": npc}
        for _ in range(20):
            tick_round(npc.world, npcs)
            if npc.state == "idle" and npc.pending_task is None:
                break
        assert npc.world["delivered"]["木材"] == 2
        assert npc.pending_task is None

    def test_run_task_clears_pending(self, npc):
        """手动派活（/api/task）清掉对话指令 — 手动 > 对话。"""
        npc.talk("给我木材")
        assert npc.pending_task is not None
        assert npc.run_task([{"type": "gather", "resource": "木材", "count": 1}])
        assert npc.pending_task is None
