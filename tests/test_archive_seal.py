"""任务书#06 归档收口 — archived 退出反思候选与合并/修剪（只排除, 永不删除）。

痛点: 管家把流水账降级成 archived 后, 它仍会被反思重新归纳成 reflection
（借尸还魂回 LLM 上下文）、被同主题合并卷走或被弱旧修剪删掉, "证据链在卡上"
的契约其实是漏的。本文件钉住两条漏网路径: 反思候选 + consolidate。

红线: 本任务只做"不参与", 任何用例都不得出现删除 archived 的路径。
"""
import json
import time

import pytest

from npc.memory import CATEGORY_ARCHIVED
from npc.npc import NPC

_HOUR = 3600


class _EchoLLM:
    """记录喂进来的 prompt（反思候选内容的观察窗）, 返回固定反思文本。"""

    def __init__(self, reply="我更谨慎了"):
        self.reply = reply
        self.prompts = []

    def chat(self, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        return type("R", (), {"content": self.reply})()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """关掉会改变反思分支的开关, 保证走可观察的单条反思路径。"""
    monkeypatch.delenv("NPC_MEMORY_TYPED", raising=False)
    monkeypatch.delenv("NPC_MEMORY_DEDUP", raising=False)


def _npc(tmp_path, llm=None):
    npc = NPC(persona={"id": "a1", "name": "阿一"}, store_dir=str(tmp_path),
              use_llm=llm is not None)
    npc._llm = llm            # 注入对话槽
    npc._llm_review = llm     # 注入 review 槽(异款模型时走这条)
    return npc


def _entry(eid, content, category="general", importance=5, hours_ago=0.0):
    return {"id": eid, "content": content, "importance": importance,
            "category": category, "created_at": time.time() - hours_ago * _HOUR}


# ── 1. 反思候选 ─────────────────────────────────────────────

def test_reflect_candidates_skip_archived(tmp_path):
    """archived 不进反思候选批（不得借反思还魂）。"""
    llm = _EchoLLM()
    npc = _npc(tmp_path, llm=llm)
    npc.memory.load([
        _entry("m1", "修好了哨塔", importance=7),
        _entry("m2", "去了森林", category=CATEGORY_ARCHIVED, importance=9),
        _entry("m3", "和玩家聊了天", importance=7),
        _entry("m4", "在河边取水", category=CATEGORY_ARCHIVED, importance=9),
        _entry("m5", "打磨了石斧", importance=7),
    ])
    out = npc.maybe_reflect()
    assert out is not None                      # 非 archived 重要性之和 21 ≥ 18 → 触发
    facts = llm.prompts[0]
    assert "修好了哨塔" in facts and "打磨了石斧" in facts
    assert "去了森林" not in facts              # 降级层不进候选
    assert "在河边取水" not in facts


def test_reflect_pointer_advances_by_filtered_batch(tmp_path, monkeypatch):
    """指针推进量 = 过滤后批长（只滤不推会让指针错位, 同一批反复检）。"""
    monkeypatch.setenv("NPC_MEMORY_DEDUP", "1")   # 开噪音闸, 走 += len(entries) 分支
    npc = _npc(tmp_path)
    npc.memory.load([
        _entry("m1", "重复的日常", importance=9),
        _entry("m2", "去了森林", category=CATEGORY_ARCHIVED, importance=9),
        _entry("m3", "去了河边", category=CATEGORY_ARCHIVED, importance=9),
        _entry("m4", "重复的日常", importance=9),
    ])
    assert npc.maybe_reflect() is None           # 唯一内容 1 种 < 3 → 噪音跳过
    # 过滤后批长 2（两条"重复的日常"）, 不是切片长 4
    assert npc._reflected_upto == 2


def test_archived_never_reincarnates_as_reflection(tmp_path):
    """核心痛点: 一轮反思下来, 产出的 reflection 里没有 archived 的影子。"""
    llm = _EchoLLM(reply="我在意玩家的安全")
    npc = _npc(tmp_path, llm=llm)
    npc.memory.load([
        _entry("m1", "修好了哨塔", importance=7),
        _entry("m2", "去了森林", category=CATEGORY_ARCHIVED, importance=9),
        _entry("m3", "打磨了石斧", importance=7),
        _entry("m4", "在河边取水", importance=7),
    ])
    npc.maybe_reflect()
    fresh = [e for e in npc.memory.all() if e.get("category") == "reflection"]
    assert fresh and "去了森林" not in fresh[-1]["content"]
    assert "去了森林" not in llm.prompts[0]      # 素材侧也没喂进去
    # 降级条目本身也还在卡上（永存红线）
    assert [e["content"] for e in npc.memory.all()
            if e.get("category") == CATEGORY_ARCHIVED] == ["去了森林"]


# ── 2. consolidate: 不合并 / 不修剪 ─────────────────────────

def test_consolidate_skips_archived_merge_and_prune(tmp_path):
    """archived 不被同主题合并卷走, 也不被弱旧修剪删掉（对照组证明修剪仍工作）。"""
    npc = _npc(tmp_path)
    npc.memory.load([
        _entry("m1", "去了森林", importance=5),
        _entry("m2", "去了森林", importance=5),
        _entry("m3", "去了森林", importance=5),
        _entry("m4", "去了森林", category=CATEGORY_ARCHIVED, importance=5,
               hours_ago=300),                 # 强度早已低于阈值
        _entry("m5", "去了河边", importance=5, hours_ago=300),   # 对照组: 会被修剪
    ])
    removed = npc.memory.consolidate(min_group=3)
    assert removed == 4                          # 合并吞 3 条 general + 修剪 m5
    left = [e for e in npc.memory.all() if e.get("category") == CATEGORY_ARCHIVED]
    assert [e["content"] for e in left] == ["去了森林"]     # archived 纹丝不动
    assert "去了河边" not in [e["content"] for e in npc.memory.all()]   # 修剪确实生效
    assert any(e.get("category") == "consolidated" for e in npc.memory.all())


def test_merge_summary_does_not_quote_archived(tmp_path):
    """合并摘要的"最近一次"不得引用 archived 条目（否则降级内容又回上下文）。"""
    npc = _npc(tmp_path)
    npc.memory.load([
        _entry("m1", "去了森林", importance=5),
        _entry("m2", "去了森林", importance=5),
        _entry("m3", "去了森林", importance=4),
        _entry("m4", "森林里发现兽径", category=CATEGORY_ARCHIVED, importance=9,
               hours_ago=1),                     # 重要度最高, 若参与会被选为"最近一次"
    ])
    npc.memory.consolidate(min_group=3)
    merged = [e for e in npc.memory.all() if e.get("category") == "consolidated"]
    assert merged and "兽径" not in merged[0]["content"]
    assert [e["content"] for e in npc.memory.all()
            if e.get("category") == CATEGORY_ARCHIVED] == ["森林里发现兽径"]


def test_archived_untouched_after_reflect_and_consolidate_round(tmp_path):
    """降级后跑一轮反思 + consolidate: archived 条目逐字节不变。"""
    llm = _EchoLLM()
    npc = _npc(tmp_path, llm=llm)
    archived = _entry("m9", "去了森林", category=CATEGORY_ARCHIVED,
                      importance=4, hours_ago=300)
    npc.memory.load([
        _entry("m1", "修好了哨塔", importance=7),
        _entry("m2", "和玩家聊了天", importance=7),
        _entry("m3", "打磨了石斧", importance=7),
        dict(archived),
    ])
    before = json.dumps(archived, sort_keys=True, ensure_ascii=False)
    npc.maybe_reflect()
    npc.memory.consolidate()
    after = [e for e in npc.memory.all() if e.get("category") == CATEGORY_ARCHIVED]
    assert len(after) == 1
    assert json.dumps(after[0], sort_keys=True, ensure_ascii=False) == before
