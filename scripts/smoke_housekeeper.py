"""任务书#04 冒烟: 模拟黎明触发 → 管家全量大整理 → 输出整理报告示例。

零网络零 LLM API: 注入离线 fake LLM 走 SCHED 同款通道, 演示
roll_and_compress + tidy_memory + 画像 三件套与 {id}_report.jsonl 产物。
用法: python scripts/smoke_housekeeper.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 项目根入 path(任意 cwd 可跑)

# 归档/报告落临时目录, 不污染仓库
_TMP = tempfile.mkdtemp(prefix="hk_smoke_")
os.environ["NPC_LOG_ARCHIVE_DIR"] = str(Path(_TMP) / "arch")
os.environ["NPC_LOG_TAIL"] = "6"

from npc import housekeeper as hk
from npc.npc import NPC
from npc.world import default_world

class FakeLLM:
    """离线三分类回复(与 npc/memory_card._parse_typed_reflection 契约对齐)。"""
    def chat(self, messages, **kw):
        return type("R", (), {"content": json.dumps([
            {"mtype": "persona", "content": "玩家是个经常夜猎的人", "importance": 8},
            {"mtype": "instruction", "content": "玩家要我修好哨塔", "importance": 7},
        ], ensure_ascii=False)})()


world = default_world()
world["log"] = [
    "cang 前往 森林", "cang 采集了 1 个木材", "cang 前往 河边",
    "cang 采集了 1 个浆果", "cang 前往 森林", "cang 采集了 1 个木材",
    "cang 前往 矿洞", "cang 采集了 1 个石头", "cang 前往 河边",
    "cang 休息", "cang 休息", "cang 休息", "cang 说: 今天收获不错",
]
world["_log_offset"] = 0

npc = NPC(persona={"id": "cang", "name": "苍"}, store_dir=_TMP, use_llm=True)
npc._llm = FakeLLM()
npc.world = world
for _ in range(3):
    npc.memory.add("夜猎回来了", importance=5)
npc.memory.add("玩家要修好哨塔并盯着进度", importance=5)
npc.memory.add("玩家在大雪夜救过我", importance=9)      # 红线: 只读不动
npc.memory.add("夜猎的路线我熟", importance=8, category="reflection")

print("=== 黎明前 memory 条数:", len(npc.memory.all()),
      "| log 条数:", len(world["log"]), "===")
reduced = hk.dawn(world, {"cang": npc})
print("=== 黎明后: 归档压缩减少行数:", reduced,
      "| memory 条数:", len(npc.memory.all()), "===")
print("world._log_offset =", world.get("_log_offset"), "(逻辑条数契约)")
print("尾部日志:", world["log"])
print("归档文件:", Path(os.environ["NPC_LOG_ARCHIVE_DIR"]) / "log_archive.jsonl")
print("归档前 3 行:")
_arch = Path(os.environ["NPC_LOG_ARCHIVE_DIR"]) / "log_archive.jsonl"
if _arch.exists():
    for line in _arch.read_text(encoding="utf-8").splitlines()[:3]:
        print("  ", line)
report = hk.report_path(npc)
print("=== 整理报告:", report, "===")
if report.exists():
    for line in report.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        print(f"[{rec['trigger']}] {rec['at']}")
        for a in rec["actions"]:
            print(f"  {a['op']:8s} {a.get('content') or a.get('id','')} — {a['why']}")
else:
    print("  (无动作 → 无报告)")
print("=== 红线核对: 高重要度条目状态:", )
for e in npc.memory.all():
    if e.get("importance", 5) >= 8:
        print("  ", e["category"], "|", e["content"])
print("冒烟完成。临时目录:", _TMP)
