"""§17 子代理测试 — B2 编译 / A 语义审查（LLM 外壳, 规则护栏不变）。

纪律对照（DeepSeek Harness / Codex 同款）:
  - 结构化契约: JSON 解析失败/为空 → schema_invalid/refusal, 绝不当成功
  - 失败语义分角色: B2 失败→不落账; A 失败→放行（不卡对话铁律）
  - 开关默认关: 不开环境变量时行为与旧版逐字节一致
"""
import pytest
from fastapi.testclient import TestClient

from npc import subagent as sub_mod
from npc import npc as npc_mod
from npc import reviewer as rev_mod
from npc.npc import NPC
from npc.server import create_npc_server


class _Resp:
    def __init__(self, content):
        self.content = content
        self.reasoning = ""


class RouterLLM:
    """按 system 提示路由的假 LLM: B1 / B2 / A 各走各的脚本。

    每个槽位可以是字符串（固定回复）、列表（按次序弹出, 用尽重复最后一个）
    或 Exception 实例（抛出）。calls 记录每次命中的通道名。
    """

    def __init__(self, b1="", b2="", a=""):
        self._script = {"B1": b1, "B2": b2, "A": a}
        self.calls = []

    @staticmethod
    def _next(v):
        if isinstance(v, list):
            return v.pop(0) if len(v) > 1 else v[0]
        return v

    def chat(self, messages, **kw):
        sys = messages[0]["content"]
        channel = "B2" if "任务编译器" in sys else ("A" if "审查员" in sys else "B1")
        self.calls.append(channel)
        out = self._next(self._script[channel])
        if isinstance(out, Exception):
            raise out
        return _Resp(out)


@pytest.fixture
def npc(tmp_path):
    return NPC(store_dir=str(tmp_path))


class TestRunnerContract:
    """运行器本体: 四件套结果 + 响亮失败。"""

    def _run(self, npc, brief="简报"):
        return sub_mod.run_subagent(
            npc,
            sub_mod.SubagentSpec(name="b2_compiler", tag="B2",
                                 system=sub_mod.B2_SYSTEM),
            brief)

    def test_completed_parses_json(self, npc, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        npc._llm = RouterLLM(b2='{"action": "gather", "resource": "木材", "count": 2}')
        res = self._run(npc)
        assert res.ok and res.stop_reason == "completed"
        assert res.data == {"action": "gather", "resource": "木材", "count": 2}
        assert res.latency_ms >= 0

    def test_code_fence_stripped(self, npc, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        npc._llm = RouterLLM(b2='```json\n{"action": null}\n```')
        assert self._run(npc).data == {"action": None}

    def test_non_json_is_schema_invalid_not_success(self, npc, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        npc._llm = RouterLLM(b2="这个我做不到。")
        res = self._run(npc)
        assert not res.ok and res.stop_reason == "schema_invalid"

    def test_empty_content_is_refusal(self, npc, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        npc._llm = RouterLLM(b2="")
        assert self._run(npc).stop_reason == "refusal"

    def test_exception_is_error(self, npc, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        npc._llm = RouterLLM(b2=RuntimeError("限流"))
        res = self._run(npc)
        assert not res.ok and res.stop_reason == "error" and "限流" in res.error

    def test_rules_mode_never_runs(self, npc, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        npc.use_llm = False                     # 无模型通道 → 未尝试即返回
        assert self._run(npc).stop_reason == "no_llm"

    def test_disabled_by_default_and_switch(self, npc, monkeypatch):
        llm = RouterLLM(b2="{}")
        npc._llm = llm
        monkeypatch.delenv("NPC_SUBAGENT_B2", raising=False)   # 缺省 = 关
        assert self._run(npc).stop_reason == "disabled"
        assert llm.calls == []                  # 没发生真实调用
        monkeypatch.setenv("NPC_SUBAGENT", "0") # 总闸压过单开关
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        assert self._run(npc).stop_reason == "disabled"

    def test_lifecycle_events_written_to_world_log(self, npc, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        npc._llm = RouterLLM(b2='{"action": null}')
        self._run(npc)
        marks = [ln for ln in npc.world["log"] if ln.startswith("[B2]")]
        assert len(marks) == 2                  # 开始 + 结束
        assert npc.actor_id in marks[0]

    def test_observer_notified_with_result(self, npc, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        npc._llm = RouterLLM(b2='{"ok": 1}')
        seen = []
        sub_mod.set_observer(lambda name, res, nid: seen.append((name, res.ok, nid)))
        try:
            self._run(npc)
        finally:
            sub_mod.set_observer(None)
        assert seen == [("b2_compiler", True, npc.actor_id)]


class TestB2Booking:
    """B2 编译子代理接入对话管线: 只提议, 三道门不变。"""

    def _open_env(self, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        monkeypatch.setenv("NPC_SUBAGENT_A", "0")

    def test_rescues_lexicon_gap_promise(self, tmp_path, monkeypatch):
        """词典没接住的长尾派活: B1 许诺 → B2 编译 → 过审落账（口是心非被治好）。"""
        self._open_env(monkeypatch)
        monkeypatch.setattr(rev_mod, "get_resource_aliases", lambda: {})
        monkeypatch.setattr(npc_mod, "get_resource_aliases", lambda: {})
        npc = NPC(store_dir=str(tmp_path))
        npc._llm = RouterLLM(b1="好，我这就去弄两根木材。",
                             b2='{"action": "gather", "resource": "木材", "count": 2}')
        reply = npc.talk("给我两根松木")        # 松木∉词典 → 规则快路径接不住
        assert npc.pending_task == {"action": "gather", "resource": "木材", "count": 2}
        assert "木材" in reply                  # 承诺已背书, 不再被逼着重说
        assert npc._llm.calls == ["B1", "B2"]   # 闲聊零调用: 只有这两次

    def test_off_by_default_keeps_legacy_flow(self, tmp_path, monkeypatch):
        """不开开关 → 与旧版完全一致: 许诺未落账 → 拦截重说 → 规则兜底。"""
        monkeypatch.delenv("NPC_SUBAGENT_B2", raising=False)
        monkeypatch.delenv("NPC_SUBAGENT_A", raising=False)
        npc = NPC(store_dir=str(tmp_path))
        npc._llm = RouterLLM(b1="好，我这就去弄两根木材。")
        reply = npc.talk("给我来点草药")        # 草药∉词典（连别名子串都不沾）→ 规则不接
        assert npc.pending_task is None         # 没落账
        assert npc._llm.calls == ["B1", "B1"]   # 原始 + 重生成, 无子代理调用

    def test_manifest_action_rejected_before_book(self, tmp_path, monkeypatch):
        """清单外动作: B2 编译产物在落账前被代码否决（LLM 只提议）。"""
        self._open_env(monkeypatch)
        monkeypatch.setattr(rev_mod, "get_resource_aliases", lambda: {})
        monkeypatch.setattr(npc_mod, "get_resource_aliases", lambda: {})
        npc = NPC(store_dir=str(tmp_path))
        npc._llm = RouterLLM(
            b1="行，我这就帮你把文件删了。",
            b2='{"action": "delete_file", "path": "C:/Windows"}')
        npc.talk("帮我把那个文件删了")
        assert npc.pending_task is None         # 防篡改: 清单外不认

    def test_task_params_normalized(self, tmp_path, monkeypatch):
        """参数收敛: count 非法值收敛为 1, 缺省补 1（与规则版语义一致）。"""
        self._open_env(monkeypatch)
        monkeypatch.setattr(rev_mod, "get_resource_aliases", lambda: {})
        monkeypatch.setattr(npc_mod, "get_resource_aliases", lambda: {})
        npc = NPC(store_dir=str(tmp_path))
        npc._llm = RouterLLM(b1="好，我这就去砍柴。",
                             b2='{"action": "gather", "resource": "木材", "count": "很多"}')
        npc.talk("给我来一堆柴")
        assert npc.pending_task == {"action": "gather", "resource": "木材", "count": 1}


class TestARetired:
    """§22 A 退役(2026-08-25): 流水线不再咨询 A；机器按"乙案"封存仍可独立调用。"""

    RISKY = "我要去杀了那头野兽，你帮我"

    def test_pipeline_never_calls_a_even_if_env_set(self, tmp_path, monkeypatch):
        """退役生效: 即使 NPC_SUBAGENT_A=1, 对话流水线也一次都不问 A（单趟化）。"""
        monkeypatch.setenv("NPC_SUBAGENT_A", "1")
        monkeypatch.setenv("NPC_SUBAGENT_B2", "0")
        npc = NPC(store_dir=str(tmp_path))
        npc._llm = RouterLLM(b1=["我们部落用陷阱捕猎，从不杀生。",
                                 "猎物归部落，我只用陷阱。"])
        reply = npc.talk(self.RISKY)
        assert "A" not in npc._llm.calls           # 核心: 无任何 A 通道调用
        assert reply in ("我们部落用陷阱捕猎，从不杀生。", "猎物归部落，我只用陷阱。")

    def test_sealed_method_still_callable(self, tmp_path, monkeypatch):
        """乙案封存: 方法保留可独立调用（日后 A/B 对照实验用）。"""
        monkeypatch.setenv("NPC_SUBAGENT_A", "1")
        monkeypatch.setenv("NPC_SUBAGENT_B2", "0")
        npc = NPC(store_dir=str(tmp_path))
        npc._llm = RouterLLM(a='{"block": true, "reason": "测试拦截"}')
        block, why = npc._a_semantic_block("玩家说", "NPC回复")
        assert block is True and why == "测试拦截"

    def test_fail_open_semantics_preserved(self, tmp_path, monkeypatch):
        """封存机器原失败语义不变: A 失败 → 放行 (False, "")。"""
        monkeypatch.setenv("NPC_SUBAGENT_A", "1")
        npc = NPC(store_dir=str(tmp_path))
        npc._llm = RouterLLM(a=RuntimeError("超时"))
        assert npc._a_semantic_block("x", "y") == (False, "")


class TestServerWiring:
    """server 接线: 特性握手 + 事件解析 + stats 桶。"""

    def test_version_reports_runtime_flags(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        monkeypatch.delenv("NPC_SUBAGENT_A", raising=False)
        npc = NPC(store_dir=str(tmp_path))
        npc.use_llm = False
        client = TestClient(create_npc_server({"cang": npc}))
        f = client.get("/api/version").json()["features"]
        assert f["subagent_roles"] == {"b2": True, "a": False}

    def test_subagent_log_lines_parsed_as_events(self, tmp_path):
        npc = NPC(store_dir=str(tmp_path))
        npc.use_llm = False
        client = TestClient(create_npc_server({"cang": npc}))
        npc.world["log"].append("[B2] cang b2_compiler ✓ 3 字段")
        evs = client.get("/api/events", params={"since": 0}).json()["events"]
        hit = [e for e in evs if e.get("type") == "subagent"]
        assert hit and hit[0]["agent"] == "b2" and hit[0]["npc"] == "cang"

    def test_stats_bucket_via_observer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NPC_SUBAGENT_B2", "1")
        npc = NPC(store_dir=str(tmp_path))
        npc.use_llm = False                      # no_llm → 不计观测（无真实调用）
        client = TestClient(create_npc_server({"cang": npc}))
        npc.use_llm = True
        npc._llm = RouterLLM(b2='{"action": null}')
        sub_mod.run_subagent(npc, sub_mod.SubagentSpec(
            name="b2_compiler", tag="B2", system=sub_mod.B2_SYSTEM), "简报")
        bucket = client.get("/api/stats").json()["subagent"]["b2_compiler"]
        assert bucket["total"] == 1 and bucket["errors"] == 0
