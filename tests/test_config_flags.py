"""core.config_flags / core.config 的坏 env 告警行为 —— 回落不静默（全内存，零网络）。"""
from __future__ import annotations

import pytest

from core import config, config_flags


@pytest.fixture(autouse=True)
def _fresh_warned():
    config_flags._WARNED.clear()          # 告警去重登记表按测试隔离
    yield
    config_flags._WARNED.clear()


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(config_flags.log, "warning",
                        lambda event, **fields: calls.append((event, fields)))
    return calls


def test_env_num_invalid_falls_back_with_warning(monkeypatch):
    """非法数值回落默认值并告警一次；同一取值重复出现不再刷屏。"""
    calls = _capture(monkeypatch)
    monkeypatch.setenv("NPC_TEST_NUM", "abc")

    assert config_flags.env_num("NPC_TEST_NUM", 3, cast=int) == 3
    assert [event for event, _ in calls] == ["env_num_invalid_fallback"]
    assert config_flags.env_num("NPC_TEST_NUM", 3, cast=int) == 3
    assert len(calls) == 1                              # 同 env 同取值只告警一次


def test_env_num_changed_bad_value_warns_again(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("NPC_TEST_NUM", "abc")
    config_flags.env_num("NPC_TEST_NUM", 3, cast=int)
    monkeypatch.setenv("NPC_TEST_NUM", "xyz")
    config_flags.env_num("NPC_TEST_NUM", 3, cast=int)

    assert len(calls) == 2                              # 换了坏值要再提醒


def test_env_num_unset_or_empty_stays_quiet(monkeypatch):
    """未设置/空串回落默认值是正常路径，不告警。"""
    calls = _capture(monkeypatch)
    monkeypatch.delenv("NPC_TEST_NUM", raising=False)

    assert config_flags.env_num("NPC_TEST_NUM", 3, cast=int) == 3
    monkeypatch.setenv("NPC_TEST_NUM", "")
    assert config_flags.env_num("NPC_TEST_NUM", 3, cast=int) == 3
    assert calls == []


def test_summary_ratio_clamped_with_warning(monkeypatch):
    """NPC_SUMMARY_RATIO 超出 [0,1] 钳到边界，同样告警不静默。"""
    calls = _capture(monkeypatch)
    monkeypatch.setenv("NPC_SUMMARY_RATIO", "2")

    assert config.summary_ratio() == 1.0
    assert [event for event, _ in calls] == ["env_value_clamped"]
    monkeypatch.setenv("NPC_SUMMARY_RATIO", "0.5")
    assert config.summary_ratio() == 0.5
    assert len(calls) == 1                              # 合法值不告警
