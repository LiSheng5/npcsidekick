"""TDAM 借鉴① 三分类记忆卡（2026-08-26）: mtype 字段 + 三分类反思。

开关 NPC_MEMORY_TYPED（默认关）: 关闭时写入路径零差异（回归锚 T1）、
反思走旧单条路径。开启时: 任务事件卡确定性归 episodic；LLM 反思产出
≤3 条 persona/episodic/instruction 结构化记忆，解析失败无痕落回旧路径。
"""
import pytest

from npc.memory_card import (
    MTYPE_DEFAULT,
    MTYPES,
    TYPED_REFLECT_MAX,
    _parse_typed_reflection,
)
from npc.npc import NPC


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("NPC_MEMORY_TYPED", raising=False)
    monkeypatch.delenv("NPC_MEMORY_DEDUP", raising=False)


def _npc(tmp_path, pid: str = "t1", use_llm: bool = False) -> NPC:
    n = NPC(persona={"id": pid, "name": pid}, store_dir=str(tmp_path),
            use_llm=use_llm, ephemeral=True)
    return n


# ── 写入口: mtype 字段 ───────────────────────────────────────

def test_off_writes_no_mtype_key(tmp_path):
    """回归锚: 开关关 → 记忆条目与旧版逐字节同构（无 mtype 键）。"""
    npc = _npc(tmp_path)
    npc.remember("完成: 给主角送十根木头", importance=8)
    entry = npc.memory.all()[0]
    assert "mtype" not in entry


def test_on_ev_events_auto_taged_episodic(tmp_path, monkeypatch):
    """开关开 → EV_DONE/EV_FAIL 前缀确定性归 episodic（构造性证据不劳 LLM）。"""
    monkeypatch.setenv("NPC_MEMORY_TYPED", "1")
    npc = _npc(tmp_path)
    npc.remember("完成: 给主角送十根木头", importance=8)
    npc.remember("没做成: 修栅栏缺工具", importance=6)
    npc.remember("玩家说今天天气不错")            # 非事件卡 → 不打标
    entries = npc.memory.all()
    assert entries[0]["mtype"] == "episodic"
    assert entries[1]["mtype"] == "episodic"
    assert "mtype" not in entries[2]


def test_on_explicit_mtype_passthrough(tmp_path, monkeypatch):
    """显式传参优先于自动打标（persona 卡不会被 EV 前缀覆盖）。"""
    monkeypatch.setenv("NPC_MEMORY_TYPED", "1")
    npc = _npc(tmp_path)
    npc.remember("完成: 给主角送十根木头", importance=8, mtype="persona")
    assert npc.memory.all()[0]["mtype"] == "persona"


# ── 解析器单元测试 ───────────────────────────────────────────

def test_parser_strips_fences_and_validates():
    raw = ('```json\n'
           '[{"mtype": "persona", "content": "甲", "importance": 8},'
           ' {"mtype": "bogus", "content": "乙", "importance": 99},'
           ' {"content": "丙"}]'
           '\n```')
    out = _parse_typed_reflection(raw)
    assert out is not None and len(out) == 3
    assert out[0] == {"mtype": "persona", "content": "甲", "importance": 8}
    assert out[1]["mtype"] == MTYPE_DEFAULT        # 白名单外归 episodic
    assert out[1]["importance"] == 9               # 夹取上界
    assert out[2]["mtype"] == MTYPE_DEFAULT        # 缺省归 episodic
    assert out[2]["importance"] == 5               # 缺省 5


def test_parser_caps_at_max():
    items = [{"mtype": "episodic", "content": f"第{i}条", "importance": 5}
             for i in range(10)]
    out = _parse_typed_reflection(str(items).replace("'", '"'))
    assert len(out) == TYPED_REFLECT_MAX


@pytest.mark.parametrize("bad", ["", "不是json", '{"a": 1}', "[broken",
                                 '[{"content": ""}]', '[]'])
def test_parser_garbage_returns_none(bad):
    assert _parse_typed_reflection(bad) is None


# ── 反思集成: 三分类路径 ─────────────────────────────────────

class _TypedLLM:
    """按预设文本回复并记录收到的 prompt。"""
    calls: list = []

    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, messages, **kwargs):
        type(self).calls.append(messages[0]["content"])
        return type("R", (), {"content": self.reply})()


_TYPED_OK = (
    '[{"mtype": "persona", "content": "主角总把采集任务托付给我", "importance": 8},'
    ' {"mtype": "episodic", "content": "帮主角修了屋顶", "importance": 6},'
    ' {"mtype": "instruction", "content": "主角要求以后优先找木材", "importance": 7},'
    ' {"mtype": "episodic", "content": "第四条应被上限截断", "importance": 5}]'
)


def test_typed_reflect_stores_multiple(tmp_path, monkeypatch):
    """开关开 + 合法 JSON → ≤3 条带 mtype 的 reflection 条目, 指针推进。"""
    monkeypatch.setenv("NPC_MEMORY_TYPED", "1")
    npc = _npc(tmp_path, use_llm=True)
    npc._llm = _TypedLLM(_TYPED_OK)
    npc.remember("帮主角采了 2 根木材", importance=6)
    npc.remember("帮主角修了屋顶", importance=7)
    text = npc.maybe_reflect()
    refl = [e for e in npc.memory.all() if e["category"] == "reflection"]
    assert len(refl) == 3                          # 第 4 条被 TYPED_REFLECT_MAX 截断
    assert [e["mtype"] for e in refl] == ["persona", "episodic", "instruction"]
    assert refl[0]["importance"] == 8
    assert "主角总把采集任务托付给我" in text
    assert npc._reflected_upto == len(npc.memory.all())
    assert "JSON" in _TypedLLM.calls[-1]           # prompt 确实要求结构化输出


def test_typed_unparsable_falls_back_to_legacy(tmp_path, monkeypatch):
    """开关开但 LLM 输出非 JSON → 无痕落回旧单条反思路径（同一 LLM 的原话）。"""
    monkeypatch.setenv("NPC_MEMORY_TYPED", "1")
    npc = _npc(tmp_path, use_llm=True)
    npc._llm = _TypedLLM("苍靠得住，总帮主角办事。")
    npc.remember("帮主角采了 2 根木材", importance=6)
    npc.remember("帮主角修了屋顶", importance=7)
    text = npc.maybe_reflect()
    assert text == "苍靠得住，总帮主角办事。"
    refl = [e for e in npc.memory.all() if e["category"] == "reflection"]
    assert len(refl) == 1 and "mtype" not in refl[0]


def test_typed_all_dupes_silently_advances(tmp_path, monkeypatch):
    """闸2b 同款: 产出与既有反思全撞车 → 返回 None 但指针翻篇。"""
    monkeypatch.setenv("NPC_MEMORY_TYPED", "1")
    npc = _npc(tmp_path, use_llm=True)
    npc._llm = _TypedLLM('[{"mtype": "persona", "content": "旧结论", "importance": 8}]')
    npc.remember("帮主角采了木材", importance=6)
    npc.remember("帮主角修了屋顶", importance=7)   # 13 >= 12 达反思阈值
    npc.memory.add("旧结论", importance=8, category="reflection")
    before = len(npc.memory.all())
    assert npc.maybe_reflect() is None             # 全撞车 → 静默翻篇
    refl = [e for e in npc.memory.all() if e["category"] == "reflection"]
    assert len(refl) == 1                          # 没有第二条
    assert npc._reflected_upto == len(npc.memory.all()) == before


def test_off_uses_legacy_single_reflection(tmp_path):
    """回归锚: 开关关 + 同样的 LLM 输出 → 走旧单条路径, 不产多条。"""
    npc = _npc(tmp_path, use_llm=True)
    npc._llm = _TypedLLM(_TYPED_OK)
    npc.remember("帮主角采了 2 根木材", importance=6)
    npc.remember("帮主角修了屋顶", importance=7)
    text = npc.maybe_reflect()
    assert text == _TYPED_OK                       # 旧路径: LLM 原话整段入库
    refl = [e for e in npc.memory.all() if e["category"] == "reflection"]
    assert len(refl) == 1 and "mtype" not in refl[0]


def test_mtypes_constant_shape():
    """白名单契约: 恰好三类, 默认档在列。"""
    assert MTYPES == ("persona", "episodic", "instruction")
    assert MTYPE_DEFAULT in MTYPES
