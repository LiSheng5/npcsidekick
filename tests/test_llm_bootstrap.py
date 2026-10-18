"""create_app(None) 自动创建的大脑必须进 holder —— 否则 /api/talk 永远走兜底。"""
from __future__ import annotations

import server
from core.paths import Paths


def test_auto_built_llm_is_visible_in_holder(tmp_path, monkeypatch):
    (tmp_path / "personas").mkdir()
    (tmp_path / "store").mkdir()
    monkeypatch.setenv("AGENT_MODEL", "deepseek-flash")
    monkeypatch.setenv("NPC_API_KEY", "sk-test")
    monkeypatch.setenv("NPC_BASE_URL", "https://api.deepseek.com")
    monkeypatch.delenv("NPC_MODEL", raising=False)

    app = server.create_app(paths=Paths(root=tmp_path))

    assert app.state.llm_holder["client"] is not None
