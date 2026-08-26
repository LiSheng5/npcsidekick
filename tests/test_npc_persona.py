"""TDAM 借鉴② NPC 画像层（2026-08-26）: 四层扫描增量更新 + talk 注入。

开关 NPC_PERSONA（默认关）: 关闭时 consolidate 不产画像文件、对话上下文
零差异（回归锚 P1）。开启时: consolidate 顺路更新 npc/store/{id}_persona.md
（≤2000 字, 无 LLM 走规则兜底只统计与摘录）; _build_context 在【记忆】前
注入【你对玩家的了解】（渐进式披露）。
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
    """返回预设画像并记录收到的 prompt（验证增量修订）。"""
    calls: list = []

    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, **kwargs):
        type(self).calls.append(messages[0]["content"])
        reply = self.replies.pop(0) if self.replies else "# 空画像"
        return type("R", (), {"content": reply})()


# ── 回归锚 ───────────────────────────────────────────────────

def test_off_no_file_and_no_injection(tmp_path):
    """开关关 → consolidate 不建画像文件, 对话上下文无画像段。"""
    npc = _npc(tmp_path)
    npc.remember("完成: 给主角送十根木头", importance=8)
    npc.consolidate()
    assert not npc.persona_path().exists()
    assert _MARKER not in npc._build_context("你好")
    assert npc.read_persona_profile() == ""


def test_off_llm_never_called_for_persona(tmp_path, monkeypatch):
    """开关关 → 即便挂着 LLM 也不该为画像花一次调用。"""
    llm = _PersonaLLM(["# 不该被写进来"])
    npc = _npc(tmp_path, use_llm=True)
    npc._llm = llm
    npc.remember("帮主角采了木材", importance=6)
    npc.consolidate()
    assert _PersonaLLM.calls == []
    assert not npc.persona_path().exists()


# ── 规则兜底路径 ─────────────────────────────────────────────

def test_on_rules_fallback_builds_stub(tmp_path, monkeypatch):
    """开关开 + 无 LLM → 规则版画像: 高频话题统计 + 反思摘录, ≤2000 字。"""
    monkeypatch.setenv("NPC_PERSONA", "1")
    npc = _npc(tmp_path)
    for _ in range(3):
        npc.remember("完成: 给主角送十根木头", importance=8)
    npc.memory.add("主角值得信赖", importance=8, category="reflection", mtype="persona")
    npc.consolidate()
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
    npc.consolidate()
    first = npc.persona_path().read_text(encoding="utf-8")
    assert "常来村里" in first
    npc.consolidate()                            # 第二轮: prompt 应含旧画像全文
    assert len(_PersonaLLM.calls) == 2
    assert "常来村里" in _PersonaLLM.calls[1]    # 旧画像进入增量 prompt
    assert "哨塔" in npc.persona_path().read_text(encoding="utf-8")


def test_on_llm_output_truncated_to_cap(tmp_path, monkeypatch):
    """LLM 输出超长 → 截断到 PERSONA_MAX_CHARS(2000)。"""
    from npc.memory_card import PERSONA_MAX_CHARS
    monkeypatch.setenv("NPC_PERSONA", "1")
    npc = _npc(tmp_path, use_llm=True)
    npc._llm = _PersonaLLM(["#" * (PERSONA_MAX_CHARS + 500)])
    npc.consolidate()
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
