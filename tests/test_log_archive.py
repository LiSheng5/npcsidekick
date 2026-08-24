"""§18 记忆分层·冷层测试 — world.log 分段归档 + 游标跨轮转恒有效。

铁律对照: 本体(日志流)只追加不删除 — 归档是搬家不是销毁;
任何写失败 → 放弃轮转（宁可多占内存, 绝不丢事件）。
"""
import json

import pytest
from fastapi.testclient import TestClient

from npc.npc import NPC
from npc.server import create_npc_server
from npc.world import rotate_world_log


def _say_lines(n, who="cang"):
    return [f"{who} 说: 事件{n}-{k}" for k in range(n)]


def _read_archive(tmp_path):
    f = tmp_path / "arch" / "log_archive.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]


class TestRotateWorldLog:
    def test_below_threshold_noop(self, tmp_path):
        w = {"log": ["a", "b"], "_log_offset": 0}
        assert rotate_world_log(w, tail=10, archive_dir=str(tmp_path)) == 0
        assert len(w["log"]) == 2 and w["_log_offset"] == 0

    def test_tail_zero_disables(self, tmp_path):
        w = {"log": list("abcdefghijk")}
        assert rotate_world_log(w, tail=0, archive_dir=str(tmp_path)) == 0
        assert len(w["log"]) == 11

    def test_no_archive_dir_keeps_everything(self):
        """没配归档目录 → 不截断（宁涨内存不丢事件）。"""
        w = {"log": list("abcdefghijk")}
        assert rotate_world_log(w, tail=5) == 0
        assert len(w["log"]) == 11

    def test_head_archived_tail_kept(self, tmp_path):
        lines = _say_lines(12)
        w = {"log": list(lines), "_log_offset": 0}
        moved = rotate_world_log(w, tail=5, archive_dir=str(tmp_path / "arch"))
        assert moved == 7
        assert w["log"] == lines[7:]
        assert w["_log_offset"] == 7
        recs = _read_archive(tmp_path)
        assert [r["i"] for r in recs] == [0, 1, 2, 3, 4, 5, 6]   # 绝对索引
        assert recs[0]["text"] == lines[0]

    def test_second_rotation_continues_absolute_indices(self, tmp_path):
        w = {"log": _say_lines(8), "_log_offset": 0}
        assert rotate_world_log(w, tail=5, archive_dir=str(tmp_path / "arch")) == 3
        w["log"].extend(_say_lines(5, "ali"))
        assert rotate_world_log(w, tail=5, archive_dir=str(tmp_path / "arch")) == 5
        assert w["_log_offset"] == 8
        assert [r["i"] for r in _read_archive(tmp_path)] == list(range(8))

    def test_write_failure_raises_not_truncates(self, tmp_path):
        blocker = tmp_path / "block"          # 预建同名"文件" → mkdir 必炸
        blocker.write_text("", encoding="utf-8")
        lines = _say_lines(9)
        w = {"log": list(lines)}
        with pytest.raises(Exception):
            rotate_world_log(w, tail=5, archive_dir=str(blocker))
        assert len(w["log"]) == 9             # 日志原封不动


class TestEventsApiColdLayer:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NPC_LOG_TAIL", "5")
        monkeypatch.setenv("NPC_LOG_ARCHIVE_DIR", str(tmp_path / "arch"))
        npc = NPC(store_dir=str(tmp_path))
        npc.use_llm = False
        client = TestClient(create_npc_server({"cang": npc}))
        return client, npc

    def test_cursor_survives_rotation_with_cold_replay(self, env):
        """核心契约: 轮转后旧游标照样取到全量增量（冷段服务端回放）。"""
        client, npc = env
        npc.world["log"].extend(_say_lines(9))
        r1 = client.get("/api/events", params={"since": 0}).json()
        assert r1["log_count"] == 9                       # 绝对流位置 = offset+尾部
        assert len(r1["events"]) == 9                     # 冷段+热段拼回完整流
        assert len(npc.world["log"]) == 5                 # 内存只留尾部
        assert npc.world["_log_offset"] == 4
        # 旧式游标（轮转前发的绝对位置）继续有效:
        r2 = client.get("/api/events", params={"since": 6}).json()
        assert [e["text"] for e in r2["events"]] == ["事件9-6", "事件9-7", "事件9-8"]
        assert r2["log_count"] == 9
        # 追平后再要增量 → 空
        assert client.get("/api/events", params={"since": 9}).json()["events"] == []

    def test_incremental_after_rotation(self, env):
        client, npc = env
        npc.world["log"].extend(_say_lines(9))
        client.get("/api/events", params={"since": 0})     # 触发轮转
        npc.world["log"].extend(_say_lines(2, "ali"))
        r = client.get("/api/events", params={"since": 9}).json()
        assert [e["npc"] for e in r["events"]] == ["ali", "ali"]
        assert r["log_count"] == 11

    def test_write_failure_degrades_to_no_rotation(self, tmp_path, monkeypatch):
        """归档写失败 → 服务端放弃轮转但端点照常工作（绝不因归档丢/堵事件）。"""
        monkeypatch.setenv("NPC_LOG_TAIL", "5")
        blocker = tmp_path / "block"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setenv("NPC_LOG_ARCHIVE_DIR", str(blocker))
        npc = NPC(store_dir=str(tmp_path))
        npc.use_llm = False
        client = TestClient(create_npc_server({"cang": npc}))
        npc.world["log"].extend(_say_lines(9))
        r = client.get("/api/events", params={"since": 0}).json()
        assert len(r["events"]) == 9 and r["log_count"] == 9
        assert len(npc.world["log"]) == 9 and npc.world.get("_log_offset", 0) == 0


class TestObservability:
    def test_version_flags_and_stats_offset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NPC_LOG_TAIL", raising=False)
        npc = NPC(store_dir=str(tmp_path))
        npc.use_llm = False
        client = TestClient(create_npc_server({"cang": npc}))
        f = client.get("/api/version").json()["features"]
        assert f["log_archive"] is True
        stats = client.get("/api/stats").json()
        assert stats["log_offset"] == 0
