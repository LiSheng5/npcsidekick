"""chatlog.py 测试 —— 持久聊天记录 + 滚动摘要。"""
from __future__ import annotations

import json

import chatlog


# ── 追加 / 读取 ──────────────────────────────────────────

def test_append_and_load(tmp_store):
    chatlog.append_turn("cang", "你好", "嗯，坐吧。", store_dir=tmp_store)
    chatlog.append_turn("cang", "今天吃什么", "锅里煮着面。", store_dir=tmp_store)
    chatlog.append_turn("cang", "好", "吃吧。", store_dir=tmp_store)

    lines = chatlog.chat_path("cang", store_dir=tmp_store).read_text("utf-8").splitlines()
    assert len(lines) == 6                                   # 每轮两行：用户一行 + 助手一行
    msgs = chatlog.load_turns("cang", store_dir=tmp_store)
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 3
    assert msgs[0]["content"] == "你好" and msgs[1]["content"] == "嗯，坐吧。"


def test_load_limit_and_missing_file(tmp_store):
    for i in range(3):
        chatlog.append_turn("cang", f"问{i}", f"答{i}", store_dir=tmp_store)

    assert [m["content"] for m in chatlog.load_turns("cang", limit=2,
                                                     store_dir=tmp_store)] == ["问2", "答2"]
    assert chatlog.load_turns("cang", limit=0, store_dir=tmp_store) == []
    assert chatlog.load_turns("nobody", store_dir=tmp_store) == []


def test_bad_line_is_skipped(tmp_store):
    path = chatlog.chat_path("cang", store_dir=tmp_store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"role": "user", "content": "好的"}) + "\n"
        + "{半行坏掉" + "\n"
        + json.dumps({"role": "assistant", "content": "嗯"}) + "\n",
        encoding="utf-8")

    msgs = chatlog.load_turns("cang", store_dir=tmp_store)
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


# ── 滚动摘要（提示用到窗口 40% 就压缩，保留最近 3 轮）────────

def _fill(store, npc_id: str, rounds: int, size: int = 10):
    for i in range(rounds):
        chatlog.append_turn(npc_id, "问" * size + str(i), "答" * size + str(i),
                            store_dir=store)


def _ratio_env(monkeypatch, window: str = "1000", ratio: str = "0.4"):
    """窗口 × 比例 = 压缩阈值（1000 × 0.4 → 400）；硬上限放开，避免干扰断言。"""
    monkeypatch.setenv("NPC_CONTEXT_WINDOW", window)
    monkeypatch.setenv("NPC_SUMMARY_RATIO", ratio)
    monkeypatch.setenv("NPC_BUDGET_HISTORY", "1000000")


def test_no_summary_below_ratio(tmp_store, make_provider, monkeypatch):
    """prompt_tokens 低于窗口 × 比例（400）→ 不摘要、不惊动 LLM。"""
    _ratio_env(monkeypatch)                               # 阈值 = 1000 × 0.4 = 400
    _fill(tmp_store, "cang", 5)
    provider = make_provider()

    assert chatlog.summary_limit() == 400
    assert chatlog.needs_summary("cang", 399, store_dir=tmp_store) is False
    assert chatlog.maybe_summarize("cang", provider, prompt_tokens=399,
                                   store_dir=tmp_store) is None
    assert not chatlog.summary_path("cang", store_dir=tmp_store).exists()
    assert provider.chat_calls == []                      # 没到阈值不惊动 LLM


def test_summary_folds_to_keep_recent_rounds(tmp_store, make_provider, monkeypatch):
    """prompt_tokens ≥ 阈值且旧轮次多于 3 轮 → 压缩；build_history = 摘要 + 恰好最近 3 轮。"""
    _ratio_env(monkeypatch)                               # 阈值 = 400，默认保留最近 3 轮
    _fill(tmp_store, "cang", 6, size=20)                              # 12 条消息
    provider = make_provider(turns=[{"text": "前情：玩家问过吃饭的事。"}])

    assert chatlog.needs_summary("cang", 400, store_dir=tmp_store) is True
    summary = chatlog.maybe_summarize("cang", provider, prompt_tokens=500,
                                      store_dir=tmp_store)

    assert summary == "前情：玩家问过吃饭的事。"
    state = chatlog.load_summary("cang", store_dir=tmp_store)
    assert state["summary"] == summary
    assert state["covered"] == 6                           # 12 条 - 保留 3 轮(6 条)
    assert state["updated_at"] > 0
    # 摘要 + 恰好最近 3 轮（6 条），一条不多一条不少
    history = chatlog.build_history("cang", provider, store_dir=tmp_store,
                                    summarize=False)
    assert len(history) == 7
    assert "前情：玩家问过吃饭的事。" in history[0]["content"]
    assert [m["role"] for m in history[1:]] == ["user", "assistant"] * 3


def test_summary_rolls_forward(tmp_store, make_provider, monkeypatch):
    """摘要之后又涨到阈值 → 再次触发；新摘要把前情摘要合并进来。"""
    _ratio_env(monkeypatch)
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "1")
    _fill(tmp_store, "cang", 5, size=20)
    provider = make_provider(turns=[{"text": "第一版摘要"}, {"text": "第二版摘要"}])

    chatlog.maybe_summarize("cang", provider, prompt_tokens=500, store_dir=tmp_store)
    _fill(tmp_store, "cang", 3, size=20)                              # 再来 3 轮，又越过阈值
    second = chatlog.maybe_summarize("cang", provider, prompt_tokens=500,
                                     store_dir=tmp_store)

    assert second == "第二版摘要"
    assert chatlog.load_summary("cang", store_dir=tmp_store)["covered"] == 14
    prompt = provider.chat_calls[1]["messages"][1]["content"]
    assert "前情摘要：第一版摘要" in prompt                   # 旧摘要参与滚动合并
    assert "问" not in prompt.split("前情摘要：")[0]          # 摘要消息排在最前


def test_summary_keeps_chatlog_when_llm_fails(tmp_store, make_provider, monkeypatch):
    from conftest import BoomProvider

    _ratio_env(monkeypatch)
    _fill(tmp_store, "cang", 5, size=20)
    before = len(chatlog.load_turns("cang", store_dir=tmp_store))

    assert chatlog.maybe_summarize("cang", BoomProvider(), prompt_tokens=500,
                                   store_dir=tmp_store) is None
    assert len(chatlog.load_turns("cang", store_dir=tmp_store)) == before
    assert not chatlog.summary_path("cang", store_dir=tmp_store).exists()


def test_summary_skipped_when_nothing_to_fold(tmp_store, make_provider, monkeypatch):
    """只有 3 轮（= 保留窗口）→ 没有可压的旧轮次，即使提示远超阈值也不触发。"""
    _ratio_env(monkeypatch)                               # 默认保留最近 3 轮
    _fill(tmp_store, "cang", 3, size=20)
    provider = make_provider()

    assert chatlog.needs_summary("cang", 10**6, store_dir=tmp_store) is False
    assert chatlog.maybe_summarize("cang", provider, prompt_tokens=10**6,
                                   store_dir=tmp_store) is None
    assert provider.chat_calls == []


def test_build_history_includes_summary_and_recent(tmp_store, make_provider, monkeypatch):
    """summarize=True = 强制压缩（不看阈值）：摘要 + 最近保留的轮次。"""
    _ratio_env(monkeypatch)
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "1")
    _fill(tmp_store, "cang", 5, size=20)
    provider = make_provider(turns=[{"text": "摘要内容"}])

    history = chatlog.build_history("cang", provider, token_budget=1000,
                                    store_dir=tmp_store)

    assert history[0]["role"] == "assistant"
    assert "摘要内容" in history[0]["content"]
    assert len(history) == 3                               # 摘要 + 保留的 1 轮(2 条)
    assert [m["role"] for m in history[1:]] == ["user", "assistant"]


def test_build_history_without_summary(tmp_store, make_provider, monkeypatch):
    _ratio_env(monkeypatch)
    _fill(tmp_store, "cang", 2)

    history = chatlog.build_history("cang", make_provider(), token_budget=1000,
                                    store_dir=tmp_store)

    assert len(history) == 4
    assert all(m["role"] in ("user", "assistant") for m in history)


def test_build_history_returns_all_turns_without_budget_blind_spot(tmp_store,
                                                                   make_provider,
                                                                   monkeypatch):
    """盲区回归：聊天记录约 5000 token、未摘要时，build_history 返回**全部**轮次 ——
    不会因为旧的 4000 固定预算把更早的轮次截掉（既不进上下文也没被摘要 = 盲区）。"""
    for name in ("NPC_BUDGET_HISTORY", "NPC_CONTEXT_WINDOW", "NPC_SUMMARY_RATIO"):
        monkeypatch.delenv(name, raising=False)             # 窗口用默认 1M
    _fill(tmp_store, "cang", 100, size=25)                             # ≈ 5300 token

    history = chatlog.build_history("cang", make_provider(), store_dir=tmp_store,
                                    summarize=False)

    assert len(history) == 200                             # 100 轮全在
    total = sum(chatlog.estimate_tokens(m["content"]) for m in history)
    assert total > 4000                                    # 确实超过旧的 4000 预算
    assert not chatlog.summary_path("cang", store_dir=tmp_store).exists()


def test_build_history_hard_cap_when_summary_unavailable(tmp_store, make_provider,
                                                         monkeypatch):
    """硬上限：NPC_BUDGET_HISTORY 很小、摘要 LLM 抛异常时，历史仍被硬上限截断、不报错。"""
    from conftest import BoomProvider

    monkeypatch.setenv("NPC_BUDGET_HISTORY", "50")
    _fill(tmp_store, "cang", 10, size=20)                             # 20 条 × 21 token
    before = len(chatlog.load_turns("cang", store_dir=tmp_store))

    history = chatlog.build_history("cang", BoomProvider(), store_dir=tmp_store,
                                    summarize=True)

    assert 0 < len(history) < before                        # 被硬上限截断，没有全量回传
    assert sum(chatlog.estimate_tokens(m["content"]) for m in history) <= 50 + 21
    assert len(chatlog.load_turns("cang", store_dir=tmp_store)) == before
    assert not chatlog.summary_path("cang", store_dir=tmp_store).exists()


def test_load_summary_empty_and_broken(tmp_store):
    assert chatlog.load_summary("nobody", store_dir=tmp_store) == {
        "summary": "", "covered": 0, "updated_at": 0.0}

    chatlog.summary_path("cang", store_dir=tmp_store).write_text("{坏的", encoding="utf-8")
    assert chatlog.load_summary("cang", store_dir=tmp_store)["summary"] == ""

    chatlog.summary_path("cang", store_dir=tmp_store).write_text(
        json.dumps({"summary": 123, "covered": -1}), encoding="utf-8")
    assert chatlog.load_summary("cang", store_dir=tmp_store) == {
        "summary": "", "covered": 0, "updated_at": 0.0}
