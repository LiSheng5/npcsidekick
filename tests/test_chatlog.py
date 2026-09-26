"""chatlog.py 测试 —— 持久聊天记录 + 滚动摘要。"""
from __future__ import annotations

import json

import chatlog


# ── 追加 / 读取 ──────────────────────────────────────────

def test_append_and_load(tmp_store):
    chatlog.append_turn("cang", "你好", "嗯，坐吧。")
    chatlog.append_turn("cang", "今天吃什么", "锅里煮着面。")
    chatlog.append_turn("cang", "好", "吃吧。")

    lines = chatlog.chat_path("cang").read_text("utf-8").splitlines()
    assert len(lines) == 6                                   # 每轮两行：用户一行 + 助手一行
    msgs = chatlog.load_turns("cang")
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 3
    assert msgs[0]["content"] == "你好" and msgs[1]["content"] == "嗯，坐吧。"


def test_load_limit_and_missing_file(tmp_store):
    for i in range(3):
        chatlog.append_turn("cang", f"问{i}", f"答{i}")

    assert [m["content"] for m in chatlog.load_turns("cang", limit=2)] == ["问2", "答2"]
    assert chatlog.load_turns("cang", limit=0) == []
    assert chatlog.load_turns("nobody") == []


def test_bad_line_is_skipped(tmp_store):
    path = chatlog.chat_path("cang")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"role": "user", "content": "好的"}) + "\n"
        + "{半行坏掉" + "\n"
        + json.dumps({"role": "assistant", "content": "嗯"}) + "\n",
        encoding="utf-8")

    msgs = chatlog.load_turns("cang")
    assert [m["content"] for m in msgs] == ["好的", "嗯"]


# ── token 估算与裁剪 ─────────────────────────────────────

def test_estimate_tokens():
    assert chatlog.estimate_tokens("") == 0
    assert chatlog.estimate_tokens("你好世界") == 4            # CJK 1 字 1 token
    assert chatlog.estimate_tokens("hello world") == 3        # 11 字符 → ceil(11/4)
    assert chatlog.estimate_tokens("你好abc") == 3            # 2 + ceil(3/4)


def test_select_recent_respects_budget():
    turns = [{"role": "user", "content": "字" * 10},
             {"role": "assistant", "content": "字" * 10},
             {"role": "user", "content": "字" * 10}]

    assert len(chatlog.select_recent(turns, 1000)) == 3
    assert [m["content"] for m in chatlog.select_recent(turns, 10)] == ["字" * 10]   # 至少留最新一条
    assert chatlog.select_recent([], 10) == []


# ── 滚动摘要（超阈值压旧轮次）────────────────────────────

def _fill(npc_id: str, rounds: int, size: int = 10):
    for i in range(rounds):
        chatlog.append_turn(npc_id, "问" * size + str(i), "答" * size + str(i))


def test_no_summary_below_threshold(tmp_store, make_provider, monkeypatch):
    monkeypatch.setenv("NPC_SUMMARY_TOKEN_THRESHOLD", "10000")
    _fill("cang", 3)
    provider = make_provider()

    assert chatlog.maybe_summarize("cang", provider) is None
    assert not chatlog.summary_path("cang").exists()
    assert provider.chat_calls == []                      # 没超阈值不惊动 LLM


def test_summary_triggered_above_threshold(tmp_store, make_provider, monkeypatch):
    monkeypatch.setenv("NPC_SUMMARY_TOKEN_THRESHOLD", "50")
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "1")
    _fill("cang", 5, size=20)                              # 10 条消息，远超 50 token
    provider = make_provider(turns=[{"text": "前情：玩家问过吃饭的事。"}])

    summary = chatlog.maybe_summarize("cang", provider)

    assert summary == "前情：玩家问过吃饭的事。"
    state = chatlog.load_summary("cang")
    assert state["summary"] == summary
    assert state["covered"] == 8                           # 10 条 - 保留 1 轮(2 条)
    assert state["updated_at"] > 0
    # 只压最旧的 8 条，最新一轮仍在聊天记录里
    assert chatlog.load_turns("cang")[-2]["content"].startswith("问")


def test_summary_rolls_forward(tmp_store, make_provider, monkeypatch):
    monkeypatch.setenv("NPC_SUMMARY_TOKEN_THRESHOLD", "50")
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "1")
    _fill("cang", 5, size=20)
    provider = make_provider(turns=[{"text": "第一版摘要"}, {"text": "第二版摘要"}])

    chatlog.maybe_summarize("cang", provider)
    _fill("cang", 3, size=20)                              # 再来 3 轮
    second = chatlog.maybe_summarize("cang", provider)

    assert second == "第二版摘要"
    assert chatlog.load_summary("cang")["covered"] == 14
    prompt = provider.chat_calls[1]["messages"][1]["content"]
    assert "前情摘要：第一版摘要" in prompt                   # 旧摘要参与滚动合并
    assert "问" not in prompt.split("前情摘要：")[0]          # 摘要消息排在最前


def test_summary_keeps_chatlog_when_llm_fails(tmp_store, make_provider, monkeypatch):
    from conftest import BoomProvider

    monkeypatch.setenv("NPC_SUMMARY_TOKEN_THRESHOLD", "50")
    _fill("cang", 5, size=20)
    before = len(chatlog.load_turns("cang"))

    assert chatlog.maybe_summarize("cang", BoomProvider()) is None
    assert len(chatlog.load_turns("cang")) == before
    assert not chatlog.summary_path("cang").exists()


def test_summary_skipped_when_nothing_to_fold(tmp_store, make_provider, monkeypatch):
    monkeypatch.setenv("NPC_SUMMARY_TOKEN_THRESHOLD", "50")
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "10")     # 保留窗口比历史还长
    _fill("cang", 3, size=20)
    provider = make_provider()

    assert chatlog.maybe_summarize("cang", provider) is None
    assert provider.chat_calls == []


def test_build_history_includes_summary_and_recent(tmp_store, make_provider, monkeypatch):
    monkeypatch.setenv("NPC_SUMMARY_TOKEN_THRESHOLD", "50")
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "1")
    _fill("cang", 5, size=20)
    provider = make_provider(turns=[{"text": "摘要内容"}])

    history = chatlog.build_history("cang", provider, token_budget=1000)

    assert history[0]["role"] == "assistant"
    assert "摘要内容" in history[0]["content"]
    assert len(history) == 3                               # 摘要 + 保留的 1 轮(2 条)
    assert [m["role"] for m in history[1:]] == ["user", "assistant"]


def test_build_history_without_summary(tmp_store, make_provider, monkeypatch):
    monkeypatch.setenv("NPC_SUMMARY_TOKEN_THRESHOLD", "10000")
    _fill("cang", 2)

    history = chatlog.build_history("cang", make_provider(), token_budget=1000)

    assert len(history) == 4
    assert all(m["role"] in ("user", "assistant") for m in history)


def test_load_summary_empty_and_broken(tmp_store):
    assert chatlog.load_summary("nobody") == {"summary": "", "covered": 0, "updated_at": 0.0}

    chatlog.summary_path("cang").write_text("{坏的", encoding="utf-8")
    assert chatlog.load_summary("cang")["summary"] == ""

    chatlog.summary_path("cang").write_text(json.dumps({"summary": 123, "covered": -1}),
                                            encoding="utf-8")
    assert chatlog.load_summary("cang") == {"summary": "", "covered": 0, "updated_at": 0.0}
