from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

# 与 reference/domain.json 对齐；tournament_fee 为本地扩展的赛事报名费类型。
EVENT_TYPES = {
    "membership_due",   # 应收会费（流入）
    "sponsor_receipt",  # 赞助款（流入，可能延期）
    "payroll",          # 工资税费（流出，刚性）
    "refund",           # 家长退款（流出，可能高峰提前）
    "tournament_fee",   # 已签赛事合同报名费（流出，可能提前扣款）
    "investment_order", # 投资下单（流出）
    "maturity",         # 理财产品到期回款（流入）
}
INFLOW_TYPES = {"membership_due", "sponsor_receipt", "maturity"}
OUTFLOW_TYPES = {"payroll", "refund", "tournament_fee", "investment_order"}

INVESTMENT_STATES = {"proposed", "approved", "blocked", "settled"}
CURRENCIES = {"CNY", "HKD", "USD"}

SCENARIO_BASE = "base"
SCENARIO_WORST = "worst"

DEFAULT_ASSUMPTIONS: dict[str, Any] = {
    # 最坏情景假设
    "sponsor_delay_days": 7,        # 赞助款延期天数
    "sponsor_collect_rate": 0.85,   # 期内赞助款到账比例
    "membership_delay_days": 3,     # 会费催缴延迟天数
    "membership_collect_rate": 0.90,  # 期内会费收缴比例
    "refund_surge_factor": 1.5,     # 退款高峰放大倍数
    "refund_early_days": 3,         # 退款集中提前天数
    "tournament_early_days": 5,     # 联赛报名费提前扣款天数
    "maturity_delay_days": 2,       # 产品到期回款延迟天数
    # 闸门参数
    "horizon_days": 30,
    "min_reserve_balance": 0.0,
}
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> date:
    return datetime.now(timezone.utc).date()


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def money(value: float) -> float:
    return round(float(value) + 0.0, 2)


def signed_amount(event_type: str, amount: float) -> float:
    sign = 1 if event_type in INFLOW_TYPES else -1
    return money(abs(amount) * sign)
