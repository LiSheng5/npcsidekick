"""
RetryPolicy — 重试策略。

支持:
  - 固定延迟 (FixedDelay)
  - 指数退避 (ExponentialBackoff)
  - 自定义策略
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Optional


class RetryPolicy(ABC):
    """重试策略基类。"""

    @abstractmethod
    def should_retry(self, attempt: int, error: Optional[str] = None) -> bool:
        """判断是否应该重试。"""
        ...

    @abstractmethod
    def wait_seconds(self, attempt: int) -> float:
        """返回第 attempt 次重试前的等待秒数。"""
        ...


class ExponentialBackoff(RetryPolicy):
    """
    指数退避: 1s, 2s, 4s, 8s...
    适用于 API 限流、网络波动。
    """

    def __init__(self, max_attempts: int = 3, base_delay: float = 1.0, max_delay: float = 30.0):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    def should_retry(self, attempt: int, error: Optional[str] = None) -> bool:
        return attempt < self.max_attempts

    def wait_seconds(self, attempt: int) -> float:
        delay = self.base_delay * (2 ** (attempt - 1))
        return min(delay, self.max_delay)


