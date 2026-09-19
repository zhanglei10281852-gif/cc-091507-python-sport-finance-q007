"""现金流预测与资金闸门计算。

资金口径（避免重复计算，乱序安全）：
- 银行流水是资金移动的唯一权威：价值日 <= as_of 的构成期初余额，
  价值日更晚的按价值日进入逐日预测（含投资申购扣款与到期回款）；
- 预测引擎只对"还没有对应流水"的计划流量补预期流：合同/工资/退款按
  情景提前并对退款叠加激增，应收会费/赞助按情景延后与折损；
- 已批准订单若尚无申购/回款流水，补结算日流出与到期日流入；
  一旦流水到达（无论先后），以流水为准。

三个情景 base / adverse / stress；可投资上限 = 全情景逐日余额最低点
减去冻结底线（退款准备金 + 最低缓冲）后的最小值。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .common import yuan
from .state import State
SCENARIOS = ("base", "adverse", "stress")

DEFAULT_ASSUMPTIONS: dict[str, Any] = {
    "inflow_delay_days": {"base": 0, "adverse": 14, "stress": 30},
    "inflow_haircut_pct": {"base": 0, "adverse": 0, "stress": 10},
    "outflow_advance_days": {"base": 0, "adverse": 7, "stress": 14},
    "refund_surge_pct": {"base": 0, "adverse": 50, "stress": 100},
    "min_buffer_cents": 0,
    "horizon_days": 90,
}


@dataclass
class ExtraOrder:
    """闸门评审中模拟的新订单。"""

    amount_cents: int
    settlement_date: date
    maturity_date: date
    proceeds_cents: int


def _daterange(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days + 1)]


def projected_proceeds(amount_cents: int, annual_rate: str, days: int) -> int:
    rate = Decimal(annual_rate)
    interest = (Decimal(amount_cents) * rate / 100 * Decimal(days) / 365).quantize(1)
    return amount_cents + int(interest)


def _add(bucket: dict[date, dict[str, int]], d: date, kind: str, amount: int) -> None:
    if amount <= 0:
        return
    slot = bucket.setdefault(d, {"in": 0, "out": 0})
    slot[kind] += amount


def _covered_total(item: dict[str, Any]) -> int:
    return sum(ln["amount_cents"] for ln in item["matched_lines"])


def scenario_flows(
    state: State,
    as_of: date,
    assumptions: dict[str, Any],
    scenario: str,
    extra: ExtraOrder | None = None,
) -> dict[date, dict[str, int]]:
    """某情景下的预期（尚无银行流水的）每日流入/流出。"""
    delay = int(assumptions["inflow_delay_days"][scenario])
    haircut = int(assumptions["inflow_haircut_pct"][scenario])
    advance = int(assumptions["outflow_advance_days"][scenario])
    surge = int(assumptions["refund_surge_pct"][scenario])
    bucket: dict[date, dict[str, int]] = {}

    # 合同 / 工资税费：未被流水覆盖的余额按情景提前；已过提前扣款日仍无流水的，
    # 视为即刻支付（落在 as_of 当天）
    for table in (state.contracts, state.payrolls):
        for it in table.values():
            remaining = max(0, it["amount_cents"] - _covered_total(it))
            if remaining <= 0:
                continue
            planned = it.get("pay_date") or it.get("date")
            d = max(as_of, planned - timedelta(days=advance))
            _add(bucket, d, "out", remaining)

    # 家长退款：未覆盖余额提前且金额激增
    for it in state.refunds.values():
        remaining = max(0, it["amount_cents"] - _covered_total(it))
        if remaining <= 0:
            continue
        d = max(as_of, it["date"] - timedelta(days=advance))
        _add(bucket, d, "out", remaining * (100 + surge) // 100)

    # 应收会费 / 赞助：未收余额延后到账并折损
    for it in state.inflows.values():
        remaining = max(0, it["amount_cents"] - _covered_total(it))
        if remaining <= 0:
            continue
        d = max(as_of, it["due_date"] + timedelta(days=delay))
        _add(bucket, d, "in", remaining * (100 - haircut) // 100)

    # 已批准理财：只在流水尚未出现时补预期现金流（结算日已过仍无扣款流水的，
    # 流出即刻落在 as_of 当天）
    for o in state.orders.values():
        if o["decision_state"] != "approved":
            continue
        if not o["settlement_lines"]:
            _add(bucket, max(as_of, o["settlement_date"]), "out", o["amount_cents"])
        if not o["maturity_lines"]:
            _add(
                bucket,
                max(as_of, o["maturity_date"]),
                "in",
                o["expected_proceeds_cents"],
            )

    if extra is not None:
        _add(bucket, max(as_of, extra.settlement_date), "out", extra.amount_cents)
        _add(bucket, max(as_of, extra.maturity_date), "in", extra.proceeds_cents)

    return bucket


def bank_balance_as_of(state: State, as_of: date) -> tuple[int, list[dict[str, Any]]]:
    """全部银行流水按价值日汇总（投资划转是真实进出，计入余额）。"""
    by_account: dict[str, int] = {}
    for line in state.lines:
        if line["value_date"] <= as_of:
            by_account[line["account_id"]] = by_account.get(line["account_id"], 0) + line["amount_cents"]
    accounts = [
        {
            "account_id": aid,
            "name": state.accounts.get(aid, {}).get("name", aid),
            "currency": state.accounts.get(aid, {}).get("currency", "CNY"),
            "balance_cents": bal,
            "balance": yuan(bal),
        }
        for aid, bal in sorted(by_account.items())
    ]
    return sum(by_account.values()), accounts


def _future_known_lines(state: State, as_of: date) -> dict[date, dict[str, int]]:
    """价值日晚于 as_of 的已收银行流水（含提前扣款通知、在途回款），按价值日入账。"""
    bucket: dict[date, dict[str, int]] = {}
    for line in state.lines:
        if line["value_date"] > as_of:
            kind = "in" if line["amount_cents"] >= 0 else "out"
            _add(bucket, line["value_date"], kind, abs(line["amount_cents"]))
    return bucket


def run_scenario(
    state: State,
    as_of: date,
    assumptions: dict[str, Any],
    scenario: str,
    horizon_days: int | None = None,
    extra: ExtraOrder | None = None,
) -> list[dict[str, Any]]:
    days = horizon_days or int(assumptions["horizon_days"])
    start_balance, _ = bank_balance_as_of(state, as_of)
    flows = scenario_flows(state, as_of, assumptions, scenario, extra)
    for d, kv in _future_known_lines(state, as_of).items():
        for kind in ("in", "out"):
            _add(flows, d, kind, kv[kind])

    rows: list[dict[str, Any]] = []
    balance = start_balance
    for d in _daterange(as_of, days):
        kv = flows.get(d, {"in": 0, "out": 0})
        balance += kv["in"] - kv["out"]
        rows.append(
            {
                "date": d.isoformat(),
                "inflow_cents": kv["in"],
                "outflow_cents": kv["out"],
                "net_cents": kv["in"] - kv["out"],
                "balance_cents": balance,
            }
        )
    return rows


def series_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    worst = min(rows, key=lambda r: r["balance_cents"])
    return {
        "min_balance_cents": worst["balance_cents"],
        "min_balance_date": worst["date"],
        "end_balance_cents": rows[-1]["balance_cents"],
    }


def _floor(state: State, assumptions: dict[str, Any]) -> int:
    return state.reserve_cents + int(assumptions.get("min_buffer_cents", 0))


def evaluate_gate(
    state: State,
    as_of: date,
    assumptions: dict[str, Any],
    extra: ExtraOrder,
) -> dict[str, Any]:
    """对模拟订单跑全部情景；任一情景击穿冻结底线即阻断，记录触发情景与首日缺口。"""
    horizon = max(
        int(assumptions["horizon_days"]),
        (extra.maturity_date - as_of).days,
    )
    floor = _floor(state, assumptions)
    scenarios_out: dict[str, Any] = {}
    failing: list[tuple[str, int, str]] = []  # 情景、缺口、首日缺口日
    for scn in SCENARIOS:
        bare_rows = run_scenario(state, as_of, assumptions, scn, horizon)
        rows = run_scenario(state, as_of, assumptions, scn, horizon, extra)
        bare_min = min(r["balance_cents"] for r in bare_rows)
        summary = series_summary(rows)
        shortfall = floor - summary["min_balance_cents"]
        scenarios_out[scn] = {
            **summary,
            "investable_cap_cents": bare_min - floor,
            "shortfall_cents": max(0, shortfall),
        }
        if shortfall > 0:
            gap = next(r for r in rows if r["balance_cents"] < floor)
            failing.append((scn, shortfall, gap["date"]))

    cap = min(v["investable_cap_cents"] for v in scenarios_out.values())
    binding = min(SCENARIOS, key=lambda k: scenarios_out[k]["investable_cap_cents"])
    if failing:
        # 触发情景取缺口最大者（同等取更严苛情景），首日缺口取所有失败情景最早者
        order = {s: i for i, s in enumerate(SCENARIOS)}
        triggered, max_short, _ = max(failing, key=lambda x: (x[1], -order[x[0]]))
        first_gap_date = min(f[2] for f in failing)
    else:
        triggered, first_gap_date, max_short = None, None, 0
    return {
        "approved": not failing,
        "floor_cents": floor,
        "investable_cap_cents": cap,
        "binding_scenario": binding,
        "triggered_scenario": triggered,
        "failing_scenarios": [f[0] for f in failing],
        "first_gap_date": first_gap_date,
        "shortfall_cents": max_short,
        "horizon_days": horizon,
        "scenarios": scenarios_out,
    }


def liquidity_risk(
    state: State,
    as_of: date,
    assumptions: dict[str, Any],
) -> dict[str, Any]:
    """不模拟新订单：评估当前已批准组合在各情景下是否存在缺口（风险告警用）。"""
    floor = _floor(state, assumptions)
    horizon = int(assumptions["horizon_days"])
    out: dict[str, Any] = {}
    failing: list[tuple[str, int, str]] = []
    for scn in SCENARIOS:
        rows = run_scenario(state, as_of, assumptions, scn, horizon)
        worst = min(rows, key=lambda r: r["balance_cents"])
        shortfall = max(0, floor - worst["balance_cents"])
        out[scn] = {
            "min_balance_cents": worst["balance_cents"],
            "min_balance_date": worst["date"],
            "shortfall_cents": shortfall,
        }
        if shortfall > 0:
            failing.append((scn, shortfall, worst["date"]))
    if failing:
        order = {s: i for i, s in enumerate(SCENARIOS)}
        triggered, max_short, _ = max(failing, key=lambda x: (x[1], -order[x[0]]))
        first_gap_date = min(f[2] for f in failing)
    else:
        triggered, first_gap_date, max_short = None, None, 0
    return {
        "floor_cents": floor,
        "triggered_scenario": triggered,
        "first_gap_date": first_gap_date,
        "shortfall_cents": max_short,
        "scenarios": out,
    }


def frozen_funds(state: State, as_of: date, assumptions: dict[str, Any]) -> dict[str, Any]:
    """冻结资金构成：退款准备金、最低缓冲、已批准待扣款申购款。"""
    reserve = state.reserve_cents
    buffer_cents = int(assumptions.get("min_buffer_cents", 0))
    items: list[dict[str, Any]] = [
        {
            "reason": "refund_reserve",
            "amount_cents": reserve,
            "amount": yuan(reserve),
            "detail": "家长退款准备金",
        },
        {
            "reason": "min_buffer",
            "amount_cents": buffer_cents,
            "amount": yuan(buffer_cents),
            "detail": "管理员设定的最低现金缓冲",
        },
    ]
    committed = 0
    for o in state.orders.values():
        if o["decision_state"] != "approved":
            continue
        settled = any(ln["value_date"] <= as_of for ln in o["settlement_lines"])
        matured = any(ln["value_date"] <= as_of for ln in o["maturity_lines"])
        if not settled and not matured:
            committed += o["amount_cents"]
    if committed:
        items.append(
            {
                "reason": "approved_pending_settlement",
                "amount_cents": committed,
                "amount": yuan(committed),
                "detail": "已批准待扣款的理财申购款",
            }
        )
    total = sum(i["amount_cents"] for i in items)
    return {"total_cents": total, "total": yuan(total), "items": items}
