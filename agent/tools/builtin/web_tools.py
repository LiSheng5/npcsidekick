"""
Web 工具 — 搜索 & 获取网页内容
"""
import urllib.request
import urllib.error
import json
from typing import Any

from agent.tools.schema import ToolProtocol, ToolSchema, ToolCall, ToolResult, ToolResultStatus


class WebSearchTool(ToolProtocol):
    """
    网络搜索工具 — 使用 DuckDuckGo 或可替换的搜索后端。
    当前实现: 基础 HTTP 请求模式，可替换为真实搜索 API。
    """
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="web_search",
            description="搜索网络获取信息。返回标题和链接列表。",
            parameters={
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "max_results": {"type": "integer", "description": "最大结果数，默认 5"},
                },
                "required": ["query"],
            },
            category="web",
            tags=["web", "search", "internet"],
            is_readonly=True,
            estimated_duration_ms=3000,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        query = call.input.get("query", "")
        max_results = call.input.get("max_results", 5)

        # 使用 DuckDuckGo Instant Answer API (非官方但公开)
        try:
            url = f"https://api.duckduckgo.com/?q={urllib.request.quote(query)}&format=json&no_html=1"
            req = urllib.request.Request(url, headers={"User-Agent": "AI-Agent-Framework/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            results = []
            # Abstract
            if data.get("AbstractText"):
                results.append({"title": data.get("Heading", query), "snippet": data["AbstractText"], "url": data.get("AbstractURL", "")})

            # Related topics
            for topic in data.get("RelatedTopics", [])[:max_results]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append({
                        "title": topic.get("FirstURL", "").rsplit("/", 1)[-1].replace("_", " "),
                        "snippet": topic.get("Text", ""),
                        "url": topic.get("FirstURL", ""),
                    })

            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.SUCCESS, data={"query": query, "results": results[:max_results], "total": len(results)})

        except urllib.error.URLError as e:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=f"网络错误: {e}")
        except Exception as e:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=str(e))


class WebFetchTool(ToolProtocol):
    """获取网页内容并转为文本。"""
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="web_fetch",
            description="获取指定 URL 的网页内容，转为纯文本。",
            parameters={
                "properties": {
                    "url": {"type": "string", "description": "网页 URL"},
                    "max_chars": {"type": "integer", "description": "最大返回字符数，默认 10000"},
                },
                "required": ["url"],
            },
            category="web",
            tags=["web", "fetch", "internet"],
            is_readonly=True,
            estimated_duration_ms=5000,
            requires_approval=True,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        url = call.input.get("url", "")
        max_chars = call.input.get("max_chars", 10000)

        if not url.startswith(("http://", "https://")):
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.REJECTED, error="URL 必须以 http:// 或 https:// 开头")

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AI-Agent-Framework/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8", errors="replace")

            # 简单 HTML → text (strip tags)
            import re
            text = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()[:max_chars]

            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.SUCCESS, data={"url": url, "content": text, "content_type": content_type})

        except urllib.error.URLError as e:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=f"获取失败: {e}")
        except Exception as e:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=str(e))
