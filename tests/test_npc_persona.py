"""TDAM 借鉴② NPC 画像层（2026-08-26）: 四层扫描增量更新 + talk 注入。

开关 NPC_PERSONA（默认关）: 关闭时不产画像文件、对话上下文零差异（回归锚 P1）。
开启时: update_persona_profile 更新 npc/store/{id}_persona.md（≤2000 字,
无 LLM 走规则兜底只统计与摘录）; _build_context 在【记忆】前注入
【你对玩家的了解】（渐进式披露）。
2026-08-28: 触发点从 consolidate 顺路改为 server 黎明跨点(一日一次 LLM),
测试改直接调用 update_persona_profile（函数语义不变）。
"""
import pytest

from npc.npc import NPC

_MARKER = "你对玩家的了解"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("NPC_PERSONA", raising=False)


def _npc(tmp_path, pid: str = "p1", use_llm: bool = False) -> NPC:
    return NPC(persona={"id": pid, "name": "阿曼"}, store_dir=str(tmp_path),
               use_llm=use_llm, ephemeral=True)


class _PersonaLLM:
    """返回预设画像并记录收到的 prompt（验证增量修订）。

    任务书#03-C: calls 为实例级(self.calls) —— 旧的类级共享列表会在
    用例乱序/-k 单跑时跨用例污染, 断言须对各自实例。
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages[0]["content"])
        reply = self.replies.pop(0) if self.replies else "# 空画像"
        return type("R", (), {"content": reply})()


# ── 回归锚 ───────────────────────────────────────────────────

def test_off_no_file_and_no_injection(tmp_path):
    """开关关 → consolidate 不建画像文件, 对话上下文无画像段。"""
    npc = _npc(tmp_path)
    npc.remember("完成: 给主角送十根木头", importance=8)
    npc.update_persona_profile()
    assert not npc.persona_path().exists()
    assert _MARKER not in npc._build_context("你好")
    assert npc.read_persona_profile() == ""


def test_off_llm_never_called_for_persona(tmp_path, monkeypatch):
    """开关关 → 即便挂着 LLM 也不该为画像花一次调用。"""
    llm = _PersonaLLM(["# 不该被写进来"])
    npc = _npc(tmp_path, use_llm=True)
    npc._llm = llm
    npc.remember("帮主角采了木材", importance=6)
    npc.update_persona_profile()
    assert llm.calls == []                  # 实例级(任务书#03-C)
    assert not npc.persona_path().exists()


# ── 规则兜底路径 ─────────────────────────────────────────────

def test_on_rules_fallback_builds_stub(tmp_path, monkeypatch):
    """开关开 + 无 LLM → 规则版画像: 高频话题统计 + 反思摘录, ≤2000 字。"""
    monkeypatch.setenv("NPC_PERSONA", "1")
    npc = _npc(tmp_path)
    for _ in range(3):
        npc.remember("完成: 给主角送十根木头", importance=8)
    npc.memory.add("主角值得信赖", importance=8, category="reflection", mtype="persona")
    npc.update_persona_profile()
    path = npc.persona_path()
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert len(text) <= 2000
    assert "规则版" in text
    assert "木材" in text                       # 同义词族归一后的高频规范词
    assert "主角值得信赖" in text               # 已沉淀认知逐字摘录


# ── LLM 四层扫描路径 ─────────────────────────────────────────

def test_on_llm_writes_profile_incremental(tmp_path, monkeypatch):
    """开关开 + LLM → 写入画像; 二轮 consolidate 把旧画像喂回 prompt（增量）。"""
    monkeypatch.setenv("NPC_PERSONA", "1")
    llm = _PersonaLLM([
        "# 对玩家的了解\n## 基础锚点\n- 玩家常来村里",
        "# 对玩家的了解\n## 基础锚点\n- 玩家常来村里\n## 认知内核\n- 目标是修好哨塔",
    ])
    npc = _npc(tmp_path, use_llm=True)
    npc._llm = llm
    npc.remember("帮主角采了木材", importance=6)
    npc.remember("陪主角去河边", importance=6)
    npc.update_persona_profile()
    first = npc.persona_path().read_text(encoding="utf-8")
    assert "常来村里" in first
    npc.update_persona_profile()                            # 第二轮: prompt 应含旧画像全文
    assert len(llm.calls) == 2
    assert "常来村里" in llm.calls[1]    # 旧画像进入增量 prompt
    assert "哨塔" in npc.persona_path().read_text(encoding="utf-8")


def test_on_llm_output_truncated_to_cap(tmp_path, monkeypatch):
    """LLM 输出超长 → 截断到 PERSONA_MAX_CHARS(2000)。"""
    from npc.memory_card import PERSONA_MAX_CHARS
    monkeypatch.setenv("NPC_PERSONA", "1")
    npc = _npc(tmp_path, use_llm=True)
    npc._llm = _PersonaLLM(["#" * (PERSONA_MAX_CHARS + 500)])
    npc.update_persona_profile()
    assert len(npc.persona_path().read_text(encoding="utf-8")) == PERSONA_MAX_CHARS


# ── talk 注入（渐进式披露）───────────────────────────────────

def test_on_profile_injected_before_memory(tmp_path, monkeypatch):
    """文件存在 + 开关开 → 上下文出现【你对玩家的了解】且排在【记忆】之前。"""
    monkeypatch.setenv("NPC_PERSONA", "1")
    npc = _npc(tmp_path)
    npc.persona_path().write_text("# 对玩家的了解\n- 玩家爱夜猎", encoding="utf-8")
    ctx = npc._build_context("你好啊")
    assert _MARKER in ctx
    assert ctx.index(_MARKER) < ctx.index("【记忆】")
    assert "玩家爱夜猎" in ctx


def test_on_missing_file_still_silent(tmp_path, monkeypatch):
    """开关开但画像还没生成过 → 注入口静默空串, 不炸上下文。"""
    monkeypatch.setenv("NPC_PERSONA", "1")
    npc = _npc(tmp_path)
    assert npc.read_persona_profile() == ""
    assert _MARKER not in npc._build_context("你好")


# ── 黎明触发（2026-08-28: 画像 LLM 一日一次, server 跨点驱动）──────────────

class TestDawnTrigger:
    def test_game_hour_tick_fallback(self):
        """未同步真实时钟 → 本地自推: (8 + tick//60) % 24, 60 tick = 1 游戏小时。"""
        from npc.server import _game_hour_now
        assert _game_hour_now({"_tick": 0}) == 8
        assert _game_hour_now({"_tick": 59}) == 8
        assert _game_hour_now({"_tick": 60}) == 9
        assert _game_hour_now({"_tick": 60 * 22}) == 6      # 22 小时后 = 黎明 6 点

    def test_game_hour_sync_priority(self):
        """真实时钟同步优先于 tick 自推。"""
        from npc.server import _game_hour_now
        assert _game_hour_now({"_tick": 60, "_game_hour": 23.0}) == 23
        assert _game_hour_now({"_tick": 60, "_game_hour": 6}) == 6

    def test_dawn_boundary_only_5_to_6(self):
        """黎明触发 = 小时跨向 6:00 的边界帧；其它跳变/同值不触发。"""
        from npc.server import _is_dawn_boundary
        assert _is_dawn_boundary(5, 6) is True
        assert _is_dawn_boundary(6, 6) is False    # 同值（每帧常态）
        assert _is_dawn_boundary(4, 5) is False    # 其它小时边界
        assert _is_dawn_boundary(None, 6) is False  # 首帧（无 prev）不触发
        assert _is_dawn_boundary(5, 22) is False   # 跳变方向不对

    def test_dawn_batch_skips_ephemeral(self):
        """黎明批次: 流民跳过, 非流民各跑一次 update_persona_profile。"""
        from npc.housekeeper import persona_batch   # 2026-08-28 简洁性 review: 搬家至此
        hit = []

        class _N:
            def __init__(self, ephemeral):
                self.ephemeral = ephemeral
            def update_persona_profile(self):
                hit.append(self)

        a, b, c, d = _N(False), _N(False), _N(True), _N(False)
        persona_batch({"a": a, "b": b, "c": c, "d": d})
        assert hit == [a, b, d]                    # 流民 c 被跳过

