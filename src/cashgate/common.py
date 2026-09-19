"""通用类型：时钟、金额工具与异常。"""
from __future__ import annotations

import threading
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable


# 金额使用整数分，避免浮点误差；币种精度（quantity_precision=6 来自 reference/domain.json）
CENT = Decimal("0.01")
QUANTITY_Q = Decimal("0.000001")


def money(value: int | float | str | Decimal) -> int:
    """把任意金额表示转换为整数分（四舍五入）。"""
    q = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    return int(q * 100)


def yuan(cents: int) -> str:
    """整数分 -> 两位小数字符串，供 JSON 输出。"""
    return str((Decimal(cents) / 100).quantize(CENT))


def qty(value: float | str | Decimal) -> str:
    """产品份额量化为 6 位小数字符串。"""
    return str(Decimal(str(value)).quantize(QUANTITY_Q, rounding=ROUND_HALF_UP))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


class Clock:
    """可注入时钟；recording_time 一律取真实 UTC 时间，业务时间由调用方提供。"""

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.Lock()
        self._fn = now_fn or utc_now

    def now(self) -> datetime:
        with self._lock:
            return self._fn()

    def today(self) -> date:
        return self.now().date()


class ServiceError(Exception):
    """所有业务错误的基类，携带 HTTP 状态码。"""

    status = 400

    def __init__(self, message: str, **extra: object) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra


class ValidationError(ServiceError):
    status = 400


class NotFoundError(ServiceError):
    status = 404


class ConflictError(ServiceError):
    """重复提交（幂等命中）以外的冲突，例如状态机非法转移。"""

    status = 409
