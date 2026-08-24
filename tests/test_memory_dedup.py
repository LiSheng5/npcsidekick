"""记忆卡治理（待办#1, 2026-08-25 方案稿获批）: 闸1 写入去重聚合 + 闸2 反思卫生 + 迁移工具。

开关 NPC_MEMORY_DEDUP（默认关）: 关闭时所有行为与旧版逐字节一致（回归锚 T1/T4b）。
"""
import importlib.util
import json
from pathlib import Path

import pytest

from npc.npc import NPC

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_memory_cards.py"
_spec = importlib.util.spec_from_file_location("migrate_memory_cards", _SCRIPT)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("NPC_MEMORY_DEDUP", raising=False)


def _npc(tmp_path, pid: str = "t1") -> NPC:
    return NPC(persona={"id": pid, "name": pid}, store_dir=str(tmp_path),
               use_llm=False, ephemeral=True)


# ── 闸1 写入去重聚合 ─────────────────────────────────────────

def test_gate1_off_keeps_old_behavior(tmp_path):
    """回归锚: 开关关闭 → 同文两条照旧都写入（旧行为逐字节一致）。"""
    npc = _npc(tmp_path)
    npc.remember("完成：休息2刻")
    npc.remember("完成：休息2刻")
    assert len(npc.memory.all()) == 2


def test_gate1_aggregates_same_content(tmp_path, monkeypatch):
    """开关开 + imp≤6 同文 → 只留一条，count 递增、created_at 就地刷新。"""
    monkeypatch.setenv("NPC_MEMORY_DEDUP", "1")
    npc = _npc(tmp_path)
    npc.remember("完成：休息2刻", importance=5)
    entry = npc.memory.all()[0]
    entry["created_at"] = 123.0          # 固定旧时间戳验证刷新
    npc.remember("完成：休息2刻", importance=5)
    npc.remember("完成：休息2刻", importance=5)
    all_ = npc.memory.all()
    assert len(all_) == 1
    assert all_[0]["count"] == 3
    assert all_[0]["created_at"] > 123.0


def test_gate1_spares_important_events(tmp_path, monkeypatch):
    """imp≥7 正事永不合并——每次都独立成条。"""
    monkeypatch.setenv("NPC_MEMORY_DEDUP", "1")
    npc = _npc(tmp_path)
    npc.remember("完成: 给主角送十根木头", importance=8)
    npc.remember("完成: 给主角送十根木头", importance=8)
    assert len(npc.memory.all()) == 2


def test_gate1_ignores_different_content(tmp_path, monkeypatch):
    monkeypatch.setenv("NPC_MEMORY_DEDUP", "1")
    npc = _npc(tmp_path)
    npc.remember("完成：休息2刻")
    npc.remember("完成：采集浆果×1")
    assert len(npc.memory.all()) == 2


# ── 闸2 反思卫生 ─────────────────────────────────────────────

def test_gate2_skips_noise_batch(tmp_path, monkeypatch):
    """唯一内容 <3 的候选批 → 不调 LLM 不写反思，但指针推进（不反复咀嚼）。"""
    monkeypatch.setenv("NPC_MEMORY_DEDUP", "1")
    npc = _npc(tmp_path)
    npc.remember("任务甲", importance=6)
    npc.remember("任务乙", importance=6)   # 各自同文会被闸1聚合 → 批内仅2条, sum=12 达阈值
    assert npc.maybe_reflect() is None
    cats = [e["category"] for e in npc.memory.all()]
    assert "reflection" not in cats                       # 零废话洞察
    assert npc._reflected_upto == 2                       # 指针已翻篇
    assert npc.maybe_reflect() is None                    # 不重复咀嚼


def test_gate2_off_still_reflects_same_input(tmp_path):
    """对照: 同样输入、开关关 → 旧路径正常产出反思（证明差异来自开关而非数据）。"""
    npc = _npc(tmp_path)
    for _ in range(2):
        npc.remember("任务甲", importance=6)
        npc.remember("任务乙", importance=6)
    text = npc.maybe_reflect()
    assert text is not None
    assert sum(1 for e in npc.memory.all() if e["category"] == "reflection") == 1


def test_gate2_dedups_repeated_reflection_output(tmp_path, monkeypatch):
    """反思产出与既有反思条同文 → 不重写，指针仍前进。"""
    monkeypatch.setenv("NPC_MEMORY_DEDUP", "1")
    npc = _npc(tmp_path)
    npc.remember("大事甲", importance=9)
    npc.remember("大事乙", importance=8)
    npc.remember("日常丙", importance=5)                  # 凑足唯一内容≥3 过闸2
    expected = "我最近做了这些事：大事甲；大事乙"          # _reflect_rules(top2) 的确定性输出
    npc.memory.add(expected, importance=8, category="reflection")
    assert npc.maybe_reflect() is None                    # 产出撞车 → 丢弃
    refl = [e for e in npc.memory.all() if e["category"] == "reflection"]
    assert len(refl) == 1                                 # 没有第二条相同反思


# ── 迁移工具 ─────────────────────────────────────────────────

def _fake_card() -> dict:
    return {
        "id": "x", "name": "x", "saved_at": "t", "persona": {}, "world": {},
        "task_log": [], "reflected_upto": 99,
        "memory": [
            {"content": "完成：休息2刻", "importance": 5, "category": "general", "created_at": 1},
            {"content": "完成：休息2刻", "importance": 5, "category": "general", "created_at": 2},
            {"content": "完成：采集浆果×1", "importance": 5, "category": "general", "created_at": 3},
            {"content": "完成: 给主角送十根木头", "importance": 8, "category": "general", "created_at": 4},
            {"content": "我一直在休息，节奏单一", "importance": 8, "category": "reflection", "created_at": 5},
            {"content": "主角值得信赖，多次托付任务", "importance": 8, "category": "reflection", "created_at": 6},
        ],
    }


def test_migrate_card_rules():
    out, st = mig.migrate_card(_fake_card())
    contents = [e["content"] for e in out["memory"]]
    assert st == {"before": 6, "r1": 1, "r2": 2, "r3": 1, "after": 2}
    assert contents == ["完成: 给主角送十根木头", "主角值得信赖，多次托付任务"]
    assert out["reflected_upto"] == 2                     # 正事与好反思分毫不动


def test_migrate_apply_idempotent(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    for cid in ("amanda", "jimmy"):
        card = _fake_card()
        card["id"] = cid
        (store / f"{cid}_memory.json").write_text(
            json.dumps(card, ensure_ascii=False), encoding="utf-8")
    assert mig.main(["--store", str(store), "--apply"]) == 0
    backups = list((tmp_path).glob("store_backup_*/amanda_memory.json"))
    assert backups                                        # 原文件有备份
    cleaned = json.loads((store / "amanda_memory.json").read_text(encoding="utf-8"))
    assert len(cleaned["memory"]) == 2
    # 二次运行幂等: 0 改动
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert mig.main(["--store", str(store), "--apply"]) == 0
    assert "-0 | R2流水账 -0 | R3退化反思 -0" in buf.getvalue()
