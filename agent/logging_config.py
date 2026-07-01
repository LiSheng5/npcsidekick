"""
结构化日志配置 — 基于 structlog + Rich 渲染。

Reasonix 模式: event.Sink — 所有内部事件通过结构化日志发出，
外部可插拔渲染器 (JSON 生产 / Console 开发 / 测试捕获)。

使用:
  from agent.logging_config import logger, configure_logging
  configure_logging(level="INFO", mode="console")  # 开发模式
  configure_logging(level="WARNING", mode="json")  # 生产模式

  log = logger.bind(component="orchestrator")
  log.info("plan_generated", task_id="task_001", steps_count=3)
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

import structlog

# Ensure stdout handles UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


# ── Shared Processors ─────────────────────────────────────


def _add_component(_, __, event_dict: dict) -> dict:
    """确保 event_dict 中有 component 字段。"""
    if "component" not in event_dict:
        event_dict["component"] = "agent"
    return event_dict


def _rename_event_to_message(_, __, event_dict: dict) -> dict:
    """将 event 字段重命名为 message (标准日志字段)。"""
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


_shared_processors = [
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
    _add_component,
    _rename_event_to_message,
]


# ── Logging Configuration ─────────────────────────────────


def configure_logging(
    level: str = "INFO",
    mode: str = "console",
    log_file: Optional[str] = None,
) -> None:
    """
    配置 structlog。

    Args:
      level: 日志级别 (DEBUG / INFO / WARNING / ERROR)
      mode: "console" — Rich 渲染, 适合开发
            "json"   — JSON 输出, 适合生产
            "test"   — 捕获到列表, 适合测试
      log_file: 可选的日志文件路径
    """
    # ── 标准库日志桥接 ─────────────────────────────────
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # 抑制 noisy 第三方库
    for noisy in ("httpx", "openai", "chromadb", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # ── 选择渲染器 ─────────────────────────────────────
    if mode == "json":
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
    elif mode == "test":
        # 测试模式: 纯文本, CaptureLogger 可捕获
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        # 开发模式: Rich 彩色输出
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    processors = _shared_processors + [
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        renderer,
    ]

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 文件输出
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(file_handler)


def get_logger(name: str = "agent") -> structlog.stdlib.BoundLogger:
    """获取绑定组件名的 logger。"""
    return structlog.get_logger(name).bind(component=name)


# ── Default logger (no configuration needed) ──────────────
# 在首次使用前调用 configure_logging(), 否则用标准 logging 输出


class _LazyLogger:
    """延迟初始化 — 如果未调用 configure_logging(), 使用合理默认值。"""
    _configured = False

    def __getattr__(self, name):
        if not _LazyLogger._configured:
            configure_logging(level="INFO", mode="console")
            _LazyLogger._configured = True
        return getattr(structlog.get_logger("agent"), name)


# 全局 logger 别名 — 方便导入
# from agent.logging_config import log
# log.info("something_happened")
log: structlog.stdlib.BoundLogger = _LazyLogger()  # type: ignore[assignment]
