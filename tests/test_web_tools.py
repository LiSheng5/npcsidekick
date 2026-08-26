"""web_tools 补测（T6·2026-08-25）— 参数校验 / 解析 / 错误路径全覆盖。

零真实网络: urlopen 全量 monkeypatch。覆盖:
- WebSearchTool: Abstract+RelatedTopics 解析、max_results 截断、非 dict 项跳过、URLError
- WebFetchTool:  非法 URL 拒绝、HTML 剥壳(script/style/标签)、max_chars 截断、URLError
"""
import json
import urllib.error

import pytest

from agent.tools.builtin.web_tools import WebFetchTool, WebSearchTool
from agent.tools.schema import ToolCall, ToolResultStatus


def _call(tool, tool_input):
    return ToolCall(call_id="t1", tool=tool.schema.name, input=tool_input)


class _FakeResp:
    """urlopen 返回的伪响应（支持 with 上下文）。"""

    def __init__(self, payload=b"", headers=None):
        self._payload = payload
        self.headers = headers or {}

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── WebSearchTool ────────────────────────────────────────────

def test_search_parses_abstract_and_topics(monkeypatch):
    payload = json.dumps({
        "Heading": "时间",
        "AbstractText": "时间是物理学基本量。",
        "AbstractURL": "https://example.org/time",
        "RelatedTopics": [
            {"Text": "时区 概念", "FirstURL": "https://example.org/timezone"},
            {"Text": "时钟 历史", "FirstURL": "https://example.org/clock"},
        ],
    }).encode("utf-8")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=10: _FakeResp(payload))
    r = WebSearchTool().execute(_call(WebSearchTool(), {"query": "几点", "max_results": 5}))
    assert r.status == ToolResultStatus.SUCCESS
    assert r.data["total"] == 3                       # Abstract 1 + Related 2
    assert r.data["results"][0]["title"] == "时间"
    assert r.data["results"][1]["url"] == "https://example.org/timezone"


def test_search_max_results_truncates(monkeypatch):
    topics = [{"Text": f"条目{i}", "FirstURL": f"https://e.org/{i}"} for i in range(5)]
    payload = json.dumps({"RelatedTopics": topics}).encode("utf-8")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=10: _FakeResp(payload))
    r = WebSearchTool().execute(_call(WebSearchTool(), {"query": "x", "max_results": 2}))
    assert len(r.data["results"]) == 2                # 截断生效


def test_search_skips_non_dict_topics(monkeypatch):
    payload = json.dumps({"RelatedTopics": ["纯字符串项", {"Text": "有效项"}]}).encode("utf-8")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=10: _FakeResp(payload))
    r = WebSearchTool().execute(_call(WebSearchTool(), {"query": "x"}))
    assert [i["snippet"] for i in r.data["results"]] == ["有效项"]   # 字符串项被跳过


def test_search_url_error_returns_error_status(monkeypatch):
    def boom(req, timeout=10):
        raise urllib.error.URLError("断网演练")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = WebSearchTool().execute(_call(WebSearchTool(), {"query": "x"}))
    assert r.status == ToolResultStatus.ERROR
    assert "网络错误" in r.error


# ── WebFetchTool ─────────────────────────────────────────────

def test_fetch_rejects_non_http_url():
    r = WebFetchTool().execute(_call(WebFetchTool(), {"url": "ftp://example.org/file"}))
    assert r.status == ToolResultStatus.REJECTED
    assert "http" in r.error.lower()


def test_fetch_strips_html_and_truncates(monkeypatch):
    html = ("<html><head><style>body{color:red}</style></head><body>"
            "<script>evil()</script><h1>标题</h1><p>正文内容很多很多</p>"
            "</body></html>").encode("utf-8")
    fake = _FakeResp(html, headers={"Content-Type": "text/html; charset=utf-8"})
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=15: fake)
    r = WebFetchTool().execute(_call(WebFetchTool(), {"url": "https://e.org/a", "max_chars": 12}))
    assert r.status == ToolResultStatus.SUCCESS
    assert "script" not in r.data["content"] and "<" not in r.data["content"]
    assert len(r.data["content"]) <= 12               # max_chars 生效
    assert r.data["content_type"].startswith("text/html")


def test_fetch_url_error_returns_error_status(monkeypatch):
    def boom(req, timeout=15):
        raise urllib.error.URLError("DNS 演练失败")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = WebFetchTool().execute(_call(WebFetchTool(), {"url": "https://e.org/"}))
    assert r.status == ToolResultStatus.ERROR
    assert "获取失败" in r.error
