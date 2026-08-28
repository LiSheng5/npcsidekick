"""任务书#05 任务回路补强: A 派发事件队列 / B manifest 动作模板 / C 地点词典。

三组都钉同一条红线 —— 默认世界(未加载清单/地点词典)行为与旧版逐字一致;
新能力只在游戏声明后才生效。回归锚见各 Test 类首例。
"""
import json
from pathlib import Path

import pytest

from npc import reviewer as rv
from npc import taskloop as tl
from npc.npc import NPC

# 真实 GTA 方言清单（本地文件, 法务隔离不入库 → 缺失时相关用例跳过）
_GTA_ACTIONS_PATH = Path(__file__).parent.parent / "npc" / "adapters" / "gta_actions.json"

# GTA 方言: 含 goto/follow_player/say, 无模板(用于钉"回退"与 C 项地点识别)
GTA_ACTIONS_DIALECT = {
    "follow_player": {"tier": 3, "approval": "ask", "params": []},
    "goto": {"tier": 2, "approval": "ask", "params": ["地点"]},
    "say": {"tier": 1, "approval": "allow", "params": ["text"]},
}


@pytest.fixture(autouse=True)
def _restore_globals(monkeypatch):
    """每例独立账本 + 复位清单/词典, 不污染其他测试文件。"""
    monkeypatch.setattr(tl, "LEDGER", tl.TaskLedger())
    yield
    rv.load_manifest(None)
    rv.set_manifest_resources(None)
    rv.set_manifest_places(None)
    rv.load_place_lexicon_from_world(None)


# ══ A 派发事件队列（book→dispatch 转换即事件）══════════════════

class TestDispatchEventQueue:

    def test_conversion_enqueues_event_exactly_once(self):
        """转换即事件: booked→dispatched 那一刻入队; 已在 dispatched 不再入队。"""
        t = tl.LEDGER.book("cang", "goto", {"地点": "河边"}, desc="去河边")
        assert tl.LEDGER.pending_dispatch_events() == 0      # 落账不入队

        tl.LEDGER.dispatch_view()
        evs = tl.LEDGER.pop_dispatch_events()
        assert len(evs) == 1
        assert evs[0]["task_id"] == t["task_id"]
        assert evs[0]["npc_id"] == "cang"
        assert evs[0]["action"] == "goto"
        assert evs[0]["params"] == {"地点": "河边"}
        assert evs[0]["desc"] == "去河边"

        tl.LEDGER.dispatch_view()                            # 已 dispatched
        assert tl.LEDGER.pop_dispatch_events() == []         # 不重复入队
        assert tl.LEDGER.pending_dispatch_events() == 0

    def test_flush_writes_log_once_and_consumes(self):
        """flush 落日志: pop 即消费 → 多客户端 poll / 断连重连都只写一条。"""
        from npc.server import flush_task_events

        tl.LEDGER.book("cang", "goto", {"地点": "河边"}, desc="去河边")
        tl.LEDGER.dispatch_view()
        world = {"log": []}
        assert flush_task_events(world) == 1
        assert world["log"] == ["cang 接下任务: 去河边"]     # 格式与归档正则对齐
        assert flush_task_events(world) == 0                 # 队列已空
        assert world["log"] == ["cang 接下任务: 去河边"]     # 没多写

    def test_ledger_stats_exposes_queue_depth(self):
        tl.LEDGER.book("cang", "goto", desc="去河边")
        tl.LEDGER.dispatch_view()
        assert tl.LEDGER.stats()["pending_dispatch_events"] == 1
        tl.LEDGER.pop_dispatch_events()
        assert tl.LEDGER.stats()["pending_dispatch_events"] == 0


@pytest.fixture
def task_client(monkeypatch, tmp_path):
    """全 app TestClient: GTA 方言清单 + 消费者报到 + NPC_TASK_LOOP 开门。"""
    from fastapi.testclient import TestClient

    from npc.server import create_npc_server

    monkeypatch.setenv("NPC_TASK_LOOP", "1")
    npc = NPC(store_dir=str(tmp_path))
    npc.use_llm = False          # 规则模式(零 LLM, 确定性)
    client = TestClient(create_npc_server({"cang": npc}))
    assert client.post("/api/manifest", json={"actions": GTA_ACTIONS_DIALECT}).status_code == 200
    assert client.post("/api/consumer/hello",
                       json={"name": "gta_mod", "version": "1.9",
                             "verbs": ["follow_player", "goto", "say"]}).json()["ok"] is True
    yield client


def _pending(client, action):
    st = client.get("/api/state", params={"consumer": "gta_mod"}).json()
    return [t for t in st["pending_tasks"]
            if t["npc_id"] == "cang" and t["action"] == action]


def _task_events(client, status):
    evs = client.get("/api/events", params={"since": 0}).json()["events"]
    return [e for e in evs if e["type"] == "task" and e.get("status") == status]


class TestDispatchEventsServerWiring:

    def test_events_written_without_polling_state(self, task_client):
        """mod 断连/只走 /api/events: 世界推进一帧即派发落日志, 不依赖客户端 poll。"""
        task_client.post("/api/talk", json={"npc_id": "cang", "message": "陪我去河边"})
        # 全程不调 /api/state（模拟 mod 断连, 没人触发派发视图）
        task_client.post("/api/tick", json={})          # 世界推进一帧
        started = _task_events(task_client, "started")
        assert len(started) == 1
        assert "河边" in started[0].get("desc", "")

    def test_state_endpoint_is_read_only(self, task_client):
        """GET /api/state 纯读: 派发照做, 但日志不因读而写（写发生在推进/事件流）。"""
        task_client.post("/api/talk", json={"npc_id": "cang", "message": "跟我走"})
        st = task_client.get("/api/state", params={"consumer": "gta_mod"}).json()
        assert len(st["pending_tasks"]) == 1            # 派发已发生
        assert not any("接下任务" in line for line in st["log_tail"])   # 读路径零写
        # 事件流 flush 后可见, 且只有一条
        assert len(_task_events(task_client, "started")) == 1

    def test_no_duplicate_across_repeated_polls(self, task_client):
        """多客户端/断连重连反复 poll + 反复取事件 → 日志只有一条。"""
        task_client.post("/api/talk", json={"npc_id": "cang", "message": "陪我去河边"})
        for _ in range(4):
            _pending(task_client, "goto")                    # 反复派发视图
        assert len(_task_events(task_client, "started")) == 1
        assert len(_task_events(task_client, "started")) == 1   # 再取一次仍是 1


# ══ B manifest 动作模板（动作呈现单一来源）══════════════════

class TestActionTemplates:

    def test_default_manifest_falls_back_to_builtin(self):
        """回归锚: 未声明模板(默认清单) → 措辞与旧版逐字一致。"""
        assert NPC._task_desc({"action": "goto", "地点": "河边"}) == "去河边"
        assert NPC._task_desc({"action": "goto"}) == "去那里"
        assert NPC._task_desc({"action": "follow_player"}) == "跟着玩家走"
        assert NPC._task_desc({"action": "gather", "task": "采集木材"}) == "采集木材"
        assert NPC._task_ack({"action": "follow_player"}) == "行，我跟着你。"
        assert NPC._task_ack({"action": "goto", "地点": "河边"}) == "好，我去河边。"
        assert (NPC._task_ack({"action": "gather", "resource": "木材", "count": 2})
                == "好，我这就去弄2个木材给你。")

    def test_template_fills_placeholders(self):
        """清单声明模板 → desc/ack 由模板填充（新增动作不再改 npc.py）。"""
        rv.load_manifest({
            "goto": {"tier": 2, "approval": "ask", "params": ["地点"],
                     "desc_tpl": "去{地点}", "ack_tpl": "好，我去{地点}等你。"},
        })
        assert NPC._task_desc({"action": "goto", "地点": "机场"}) == "去机场"
        assert NPC._task_ack({"action": "goto", "地点": "机场"}) == "好，我去机场等你。"

    def test_new_action_needs_manifest_change_only(self):
        """单点验收: drive_to 从未在 npc.py 出现过 → 只写清单即出 desc+ack。"""
        assert "drive_to" not in ("goto", "follow_player")
        rv.load_manifest({
            "drive_to": {"tier": 3, "approval": "ask", "params": ["place"],
                         "desc_tpl": "开车去{place}", "ack_tpl": "上车，我载你去{place}。"},
        })
        assert NPC._task_desc({"action": "drive_to", "place": "机场"}) == "开车去机场"
        assert NPC._task_ack({"action": "drive_to", "place": "机场"}) == "上车，我载你去机场。"
        # 无模板的新动作 → 回退 action 名, 不炸
        rv.load_manifest({"fly": {"tier": 1, "approval": "allow"}})
        assert NPC._task_desc({"action": "fly", "place": "天"}) == "fly"

    def test_braces_and_broken_templates_are_safe(self):
        """风险表: 值含花括号不二次解析; 模板非法 → 静默回退, 不卡落账。"""
        rv.load_manifest({"goto": {"tier": 2, "approval": "ask", "params": ["地点"],
                                   "desc_tpl": "去{地点}"}})
        assert NPC._task_desc({"action": "goto", "地点": "{河边}"}) == "去{河边}"
        # 花括号不配对 → 渲染返回空 → 回退内置措辞
        rv.load_manifest({"goto": {"tier": 2, "approval": "ask", "params": ["地点"],
                                   "desc_tpl": "去{地点", "ack_tpl": "好{count"}})
        assert NPC._task_desc({"action": "goto", "地点": "河边"}) == "去河边"
        assert NPC._task_ack({"action": "goto", "地点": "河边"}) == "好，我去河边。"
        # 占位符缺失 → 空串填充, 非空结果照用
        rv.load_manifest({"goto": {"tier": 2, "approval": "ask", "params": ["地点"],
                                   "desc_tpl": "去{地点}{where}"}})
        assert NPC._task_desc({"action": "goto", "地点": "河边"}) == "去河边"

    @pytest.mark.skipif(not _GTA_ACTIONS_PATH.exists(),
                        reason="GTA 方言清单为本地文件(法务隔离, 不入库)")
    def test_gta_actions_json_templates_end_to_end(self):
        """真实清单文件: 加载后 goto/follow/drive_to 措辞与模板一致。"""
        doc = json.loads(_GTA_ACTIONS_PATH.read_text(encoding="utf-8"))
        parsed = rv.parse_manifest_doc(doc)
        rv.load_manifest(parsed["actions"])
        assert NPC._task_desc({"action": "goto", "地点": "河边"}) == "去河边"
        assert NPC._task_ack({"action": "goto", "地点": "河边"}) == "好，我去河边。"
        assert NPC._task_ack({"action": "follow_player"}) == "行，我跟着你。"
        assert NPC._task_desc({"action": "drive_to", "place": "机场"}) == "开车去机场"

    def test_manifest_response_hides_empty_templates(self):
        """协议零破坏: 未声明模板时 /api/manifest 响应字段与旧版一致。"""
        rv.load_manifest(GTA_ACTIONS_DIALECT)
        spec = rv.get_manifest()["goto"]
        assert spec == {"tier": 2, "approval": "ask", "params": ["地点"], "desc": ""}
        rv.load_manifest({"goto": {"tier": 2, "approval": "ask", "params": ["地点"],
                                   "desc_tpl": "去{地点}"}})
        assert rv.get_manifest()["goto"]["desc_tpl"] == "去{地点}"
        assert "ack_tpl" not in rv.get_manifest()["goto"]      # 没声明就不出现


# ══ C 地点词典（深度版, 由 world/manifest 声明驱动）════════════

class TestPlaceLexicon:

    def test_unloaded_lexicon_behaves_like_old_path(self):
        """回归锚: 未加载词典 → 回退尾词清洗, 逐字与旧版一致。"""
        rv.load_manifest(GTA_ACTIONS_DIALECT)
        rv.load_place_lexicon_from_world(None)
        assert rv.get_place_aliases() is None
        assert rv.compile_task("陪我去河边") == {"action": "goto", "地点": "河边"}
        assert rv.compile_task("陪我去河边吧") == {"action": "goto", "地点": "河边"}
        assert rv.compile_task("到河边去") == {"action": "goto", "地点": "河边"}

    def test_longest_match_prefers_fuller_name(self):
        """最长匹配: "老家的河边" 压过 "河边" → 归一成地标表里的规范名。"""
        rv.load_manifest(GTA_ACTIONS_DIALECT)
        world = {"locations": {"河边": {}, "老家的河边": {}}}
        rv.load_place_lexicon_from_world(world)
        assert rv.compile_task("陪我去老家的河边")["地点"] == "老家的河边"
        assert rv.compile_task("陪我去河边走走")["地点"] == "河边"

    def test_alias_normalizes_to_canonical(self):
        """别名归一: location.aliases / _place_aliases / 清单 places 都收。"""
        rv.load_manifest(GTA_ACTIONS_DIALECT)
        world = {"locations": {"河边": {"aliases": ["河沿儿", "水边"]},
                               "村庄": {}},
                 "_place_aliases": {"河边": ["河滩"]}}
        rv.load_place_lexicon_from_world(world)
        assert rv.compile_task("陪我去河沿儿吧")["地点"] == "河边"
        assert rv.compile_task("到水边去")["地点"] == "河边"
        assert rv.compile_task("陪我去河滩")["地点"] == "河边"

    def test_manifest_places_extra_merges(self):
        """清单 places 段可给世界地名补别名, 也可声明世界里没有的地名。"""
        rv.load_manifest(GTA_ACTIONS_DIALECT)
        world = {"locations": {"洛圣都国际机场": {}}}
        rv.load_place_lexicon_from_world(world,
                                         extra={"洛圣都国际机场": ["机场", "LSIA"],
                                                "好麦坞山": ["山上"]})
        assert rv.compile_task("陪我去LSIA")["地点"] == "洛圣都国际机场"
        assert rv.compile_task("陪我去机场")["地点"] == "洛圣都国际机场"
        assert rv.compile_task("陪我去山上")["地点"] == "好麦坞山"

    def test_long_place_not_truncated(self):
        """痛点验收: 长地名不再被 10 字截断成别的地名（mod 地标表查得到）。"""
        rv.load_manifest(GTA_ACTIONS_DIALECT)
        long_name = "密拉玛高地观景台停车场"       # 11 字 > 旧代码 10 字截断
        rv.load_place_lexicon_from_world({"locations": {long_name: {}}})
        assert rv.compile_task(f"陪我去{long_name}")["地点"] == long_name
        # 对照: 未加载词典 → 旧路径截断成前 10 字(另一个地名, mod 查无 → 失败商议)
        rv.load_place_lexicon_from_world(None)
        assert rv.compile_task(f"陪我去{long_name}")["地点"] == long_name[:10]

    def test_empty_lexicon_stays_unloaded(self):
        """零地点世界(纯对话) → 保持未加载, 走旧路径。"""
        rv.load_place_lexicon_from_world({"locations": {}})
        assert rv.get_place_aliases() is None
        rv.load_manifest(GTA_ACTIONS_DIALECT)
        assert rv.compile_task("陪我去河边吧")["地点"] == "河边"

    def test_parse_manifest_doc_places_section(self):
        """清单 places 段解析（与 resources 同款）: 合法收, 类型错抛 ValueError。"""
        parsed = rv.parse_manifest_doc({"actions": {"goto": {"tier": 2, "approval": "ask"}},
                                        "places": {"机场": ["机场儿"]}})
        assert parsed["places"] == {"机场": ["机场儿"]}
        assert rv.parse_manifest_doc({"fly": {"tier": 1, "approval": "allow"}})["places"] is None
        with pytest.raises(ValueError):
            rv.parse_manifest_doc({"actions": {"fly": {"tier": 1, "approval": "allow"}},
                                   "places": []})
