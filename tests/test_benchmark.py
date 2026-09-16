"""P-11 回归锚（2026-09-16）：基准**指标化**——断言 / 退出码 / JSON 可解析 / 自主循环指标。

《NPC大脑架构》§30.1 第 7 步 / 审计 V-04（原话：`npc/benchmark.py` 是 90 行 print 脚本，
**无断言 / 无退出码 / 无 CI 接入**，也测不到自主 scheduler）。本文件钉四件事：
  ① 10 个任务（3 档难度 + 3 个受阻场景）结果全部符合预期 —— 引擎退化就会被卡住；
  ② `--json` 的 stdout **必须是纯 JSON**（CI 能直接 json.loads）；
  ③ **退出码真的会失败**：人为制造不符合预期的任务 → 非零（证明它真能当 CI 闸门）；
  ④ 新增的**自主循环指标**在固定 seed 下确定性、且零 LLM 调用。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from npc import benchmark as B

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestStructuredResult:
    """结构化结果：形状、内容、可序列化。"""

    def test_shape_and_all_correct(self, tmp_path):
        r = B.run_benchmark(store_dir=str(tmp_path), rounds=12)
        assert set(r) == {"summary", "tiers", "tasks", "autonomy"}
        s = r["summary"]
        assert s["total"] == len(B.TASKS) == 10
        assert s["failed"] == 0, f"引擎退化了：{[t['name'] for t in r['tasks'] if not t['correct']]}"
        assert s["pass_rate"] == 1.0
        assert s["llm_calls"] == 0, "基准必须零 LLM 调用（CI 里没有 key）"

    def test_tiers_cover_all_tasks(self, tmp_path):
        r = B.run_benchmark(store_dir=str(tmp_path), rounds=6)
        assert {k: (v["correct"], v["total"]) for k, v in r["tiers"].items()} == \
            {"简单": (3, 3), "中等": (2, 2), "复杂": (2, 2), "受阻": (3, 3)}
        assert sum(v["total"] for v in r["tiers"].values()) == len(B.TASKS)

    def test_blocked_tasks_expect_failure(self, tmp_path):
        """受阻任务"优雅失败"也算对 —— 预期的语义要钉住。"""
        r = B.run_benchmark(store_dir=str(tmp_path), rounds=4)
        blocked = [t for t in r["tasks"] if t["expect"] == "fail"]
        assert len(blocked) == 3
        assert all(t["ok"] is False and t["correct"] for t in blocked)

    def test_json_serialisable(self, tmp_path):
        r = B.run_benchmark(store_dir=str(tmp_path), rounds=4)
        assert json.loads(json.dumps(r, ensure_ascii=False)) == r


class TestAutonomyMetrics:
    """P-11 新增：原来完全看不见的自主循环指标。"""

    def test_metrics_present_and_deterministic(self, tmp_path):
        a = B.run_benchmark(store_dir=str(tmp_path), rounds=16)["autonomy"]
        b = B.run_benchmark(store_dir=str(tmp_path), rounds=16)["autonomy"]
        assert a == b, "固定 seed → 指标必须逐字段确定（可当回归锚）"
        for key in ("rounds", "ticks", "started", "completed", "failed", "log_len",
                    "action_counts", "delivered", "llm_calls"):
            assert key in a
        assert a["ticks"] == 16 and a["log_len"] > 0
        assert a["started"] >= a["completed"], "完成数不可能多于开始数"
        assert a["llm_calls"] == 0
        assert sum(a["action_counts"].values()) > 0, "规则路径应当真的产生了动作"

    def test_zero_rounds_does_not_crash(self, tmp_path):
        a = B.run_benchmark(store_dir=str(tmp_path), rounds=0)["autonomy"]
        assert a["ticks"] == 0 and a["started"] == 0 and a["log_len"] == 0


class TestExitCodeIsRealGate:
    """退出码必须**真的会失败** —— 否则它只是装饰。"""

    def test_all_good_returns_zero(self, tmp_path, capsys):
        assert B.main(["--store", str(tmp_path), "--rounds", "4", "--quiet"]) == 0
        assert "通过: 10/10" in capsys.readouterr().out

    def test_regression_returns_nonzero(self, tmp_path, monkeypatch):
        """塞一个"永远做不到的 pass 期望"→ 退出码必须非零（CI 靠它卡回归）。"""
        bogus = {"name": "不可能任务", "steps": [{"type": "gather", "resource": "不存在的东西",
                                              "count": 1}], "tier": "简单"}
        monkeypatch.setattr(B, "TASKS", B.TASKS + [bogus])
        assert B.main(["--store", str(tmp_path), "--rounds", "2", "--quiet"]) == 1

    def test_json_flag_is_pure_json_via_subprocess(self, tmp_path):
        """真正的 CI 契约：**stdout 从头到尾能被 json.loads**（不能混日志）。"""
        proc = subprocess.run(
            [sys.executable, "-m", "npc.benchmark", "--json", "--rounds", "6",
             "--store", str(tmp_path)],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=180)
        assert proc.returncode == 0, proc.stderr[-800:]
        parsed = json.loads(proc.stdout)          # ← 混进一行日志这里就会炸
        assert parsed["summary"]["failed"] == 0
        assert parsed["autonomy"]["rounds"] == 6


class TestStoreIsolation:
    """基准数据落在指定的隔离目录（默认 npc/store_bench 已被 .gitignore 覆盖）。"""

    def test_writes_into_given_store_only(self, tmp_path):
        B.run_benchmark(store_dir=str(tmp_path), rounds=6)
        assert (tmp_path / "cang_memory.json").exists()
        assert not (REPO_ROOT / "npc" / "store_bench" / "cang_memory.json").exists()


@pytest.mark.parametrize("bad_rounds", [-1])
def test_negative_rounds_tolerated(tmp_path, bad_rounds):
    a = B.run_benchmark(store_dir=str(tmp_path), rounds=bad_rounds)["autonomy"]
    assert a["started"] == 0
