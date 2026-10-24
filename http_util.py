"""HTTP 层共享小工具 —— 游戏面（server.py）与控制台（console_api.py）共用。

只放两侧同构的请求处理；业务校验留在各自模块。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import HTTPException, Request


async def json_body(request: Request) -> Dict[str, Any]:
    """请求体 → JSON 对象；不是合法 JSON / 不是对象 → 400（两侧报错文案一致）。"""
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return data
