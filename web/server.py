"""
Web UI 后端 — FastAPI + SSE 流式推送。

将 AgentOrchestrator.run_stream() 的 StreamEvent 流
转为 Server-Sent Events 推送到浏览器。

启动:
  python -m web.server
  python main.py --web
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent.orchestrator import AgentOrchestrator
from agent.llm.types import StreamEvent, StreamEventType
from agent.logging_config import log


# ── 内部事件（不推送到前端）──────────────────────────
_INTERNAL_EVENT_TYPES: set[StreamEventType] = {
    StreamEventType.THINKING,     # 内部推理
    StreamEventType.REFLECTION,   # 反思决策
    StreamEventType.STEP_PROGRESS, # 未使用
}

# ── 允许的 Origin（loopback only）────────────────────
_ALLOWED_ORIGIN_PREFIXES = ("http://127.0.0.1", "http://localhost")


def serialize_event(event: StreamEvent) -> dict:
    """将 StreamEvent 转为 JSON 安全的 dict。只传行为层数据。"""
    return {
        "type": event.type.value,
        "data": event.data,
        "message": event.message,
        "timestamp": event.timestamp,
    }


def _verify_auth(request: Request) -> None:
    """FastAPI 依赖:验证 token + Origin（P0-3）。

    Token 通过 ?token=... 或 Authorization: Bearer <token> 传递。
    Origin 只允许 loopback。
    """
    # 1. Origin 检查
    origin = request.headers.get("origin", "")
    if origin and not any(origin.startswith(p) for p in _ALLOWED_ORIGIN_PREFIXES):
        raise HTTPException(status_code=403, detail="不允许的 Origin")

    # 2. Token 检查
    token = request.app.state.web_token
    provided = (
        request.query_params.get("token")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    if not provided or provided != token:
        raise HTTPException(status_code=401, detail="未授权: token 无效或缺失")


def create_app(token: str) -> FastAPI:
    """构建 FastAPI 应用（工厂函数，便于测试）。

    Args:
        token: Web 认证 token（启动时随机生成）。
    """
    app = FastAPI(title="NPCSidekick NPCSidekick")
    app.state.web_token = token

    # 全局 orchestrator（惰性初始化）
    _orch: Optional[AgentOrchestrator] = None
    _init_lock = False

    def get_orch() -> AgentOrchestrator:
        nonlocal _orch, _init_lock
        if _orch is None:
            if _init_lock:
                raise RuntimeError("Orchestrator 正在初始化中")
            _init_lock = True
            try:
                _orch = AgentOrchestrator()
                _orch.initialize()
                log.info("web_orchestrator_initialized")
            finally:
                _init_lock = False
        return _orch

    # ── API Endpoints（所有端点需要认证）────────────────

    @app.post("/api/chat", dependencies=[Depends(_verify_auth)])
    async def chat(request: Request):
        """非流式聊天 — 返回完整答案。"""
        body = await request.json()
        message = body.get("message", "").strip()
        if not message:
            return JSONResponse({"error": "消息不能为空"}, status_code=422)

        orch = get_orch()
        start = time.time()
        answer = orch.run_chat(message)
        duration = time.time() - start

        return JSONResponse({
            "answer": answer,
            "duration_sec": round(duration, 2),
        })

    @app.post("/api/chat/stream", dependencies=[Depends(_verify_auth)])
    async def chat_stream(request: Request):
        """SSE 流式聊天 — 推送 StreamEvent 序列。"""
        body = await request.json()
        message = body.get("message", "").strip()
        if not message:
            return JSONResponse({"error": "消息不能为空"}, status_code=422)

        orch = get_orch()

        async def event_stream():
            try:
                async for event in orch.run_stream(message):
                    if event.type in _INTERNAL_EVENT_TYPES:
                        continue
                    data = json.dumps(serialize_event(event), ensure_ascii=False)
                    yield f"data: {data}\n\n"
                yield "data: [DONE]\n\n"
            except Exception:
                log.exception("stream_error")
                error_data = json.dumps({
                    "type": "error",
                    "data": {"error": "internal_error"},
                    "message": "处理请求时发生内部错误，请查看服务端日志。",
                    "timestamp": datetime.now().isoformat(),
                }, ensure_ascii=False)
                yield f"data: {error_data}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/history", dependencies=[Depends(_verify_auth)])
    async def history():
        """行为日志 — 返回执行历史。"""
        orch = get_orch()
        return JSONResponse({
            "history": orch._execution_history,
            "summary": orch.get_execution_summary(),
        })

    # 静态文件（必须最后注册，否则会拦截 API 路由）
    # 2026-09-07: 通用 Agent 聊天台迁到 web/agent_static/ —— 与 NPC Runtime 的
    # web/static/ 彻底分开。两服务不再共用一份静态目录（原先 index.html 会挂着
    # /api/chat/stream 被 NPC 服务 Serve，在 8765 上是个点不开的废页）。
    static_dir = Path(__file__).parent / "agent_static"
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


# ── 模块级 app（uvicorn 需要 `web.server:app` 语法）───
# 从 settings 读取 token；如果 settings 不可用则用临时 token。
try:
    from agent.settings import get_settings
    _settings = get_settings()
    _token = _settings.web_token
except Exception:
    import secrets
    _token = secrets.token_hex(16)

app = create_app(token=_token)


# ── 直接启动 ───────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    from agent.settings import get_settings

    settings = get_settings()
    token = settings.web_token
    host = settings.web_host
    port = settings.web_port
    url = f"http://{host}:{port}/?token={token}"
    print(f"NPCSidekick Web UI: {url}")
    uvicorn.run(
        "web.server:app",
        host=host,
        port=port,
        log_level="info",
    )
