from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from domain import (
    DEFAULT_ASSUMPTIONS,
    SCENARIO_BASE,
    SCENARIO_WORST,
    money,
    parse_date,
    signed_amount,
)

SCENARIOS = (SCENARIO_BASE, SCENARIO_WORST)
RESERVE_CATEGORIES = ("payroll", "refund", "tournament_fee")


@dataclass
class Forecast:
    as_of: Any
    horizon_days: int
    assumption_version: int
    assumptions: dict[str, Any]
    opening: dict[str, float]
    daily: dict[str, list[dict[str, Any]]]
    min_balance: dict[str, dict[str, float]]
    reserves: dict[str, dict[str, float]]
    investable_cap: dict[str, float]
    variance: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "horizon_days": self.horizon_days,
            "assumption_version": self.assumption_version,
            "assumptions": self.assumptions,
            "opening_balance": self.opening,
            "scenarios": {
                name: {
                    "daily": rows,
                    "min_balance": self.min_balance[name],
                }
                for name, rows in self.daily.items()
            },
            "reserves": self.reserves,
            "investable_cap": self.investable_cap,
            "scenario_variance": self.variance,
        }


def current_assumptions(state: dict[str, Any]) -> dict[str, Any]:
    if state["assumptions"]:
        return state["assumptions"][-1]
    return {"version": 0, "params": dict(DEFAULT_ASSUMPTIONS)}


def bank_balances(state: dict[str, Any]) -> dict[str, float]:
    balances = {acc["currency"]: 0.0 for acc in state["accounts"]}
    for entry in state["bank_entries"]:
        balances[entry["currency"]] = money(
            balances.get(entry["currency"], 0.0) + entry["amount"]
        )
    return balances


def _day_index(d: Any, as_of: Any, horizon: int) -> int | None:
    delta = (d - as_of).days
    if delta < 0:
        delta = 0  # 逾期未达的现金流滚动到当日
    if delta > horizon:
        return None  # 落在预测窗口外
    return delta


def _collect_flows(
    state: dict[str, Any],
    scenario: str,
    as_of: Any,
    horizon: int,
    params: dict[str, Any],
) -> tuple[dict[str, dict[int, float]], dict[str, dict[str, float]]]:
    """返回 (按日流量 {币种: {日序: 净额}}, 准备金口径 {币种: {类别: 合计}})。"""
    flows: dict[str, dict[int, float]] = {}
    reserves: dict[str, dict[str, float]] = {}

    def add_flow(currency: str, idx: int, amount: float) -> None:
        flows.setdefault(currency, {})
        flows[currency][idx] = money(flows[currency].get(idx, 0.0) + amount)

    def add_reserve(currency: str, category: str, amount: float) -> None:
        reserves.setdefault(currency, {})
        reserves[currency][category] = money(
            reserves[currency].get(category, 0.0) + amount
        )

    for ev in state["cash_events"]:
        if not ev.get("committed", True) or ev.get("realized", False):
            continue
        currency = ev["currency"]
        d = parse_date(ev["scheduled_date"])
        amount = abs(ev["amount"])
        etype = ev["type"]

        if scenario == SCENARIO_WORST:
            if etype == "sponsor_receipt":
                d = d + timedelta(days=int(params["sponsor_delay_days"]))
                amount *= float(params["sponsor_collect_rate"])
            elif etype == "membership_due":
                d = d + timedelta(days=int(params["membership_delay_days"]))
                amount *= float(params["membership_collect_rate"])
            elif etype == "refund":
                d = d - timedelta(days=int(params["refund_early_days"]))
                amount *= float(params["refund_surge_factor"])
            elif etype == "tournament_fee":
                d = d - timedelta(days=int(params["tournament_early_days"]))

        idx = _day_index(d, as_of, horizon)
        if idx is None:
            continue
        add_flow(currency, idx, signed_amount(etype, amount))
        if etype in RESERVE_CATEGORIES:
            add_reserve(currency, etype, amount)

    # 在途投资：approved 含下单流出与到期回款；settled 仅含到期回款；
    # matured 已由银行回款入账，不再预测。
    for inv in state["investments"]:
        if inv["state"] not in ("approved", "settled"):
            continue
        currency = inv["currency"]
        if inv["state"] == "approved":
            settle_idx = _day_index(
                parse_date(inv.get("settle_date") or inv["order_date"]),
                as_of,
                horizon,
            )
            if settle_idx is not None:
                add_flow(currency, settle_idx, -money(inv["amount"]))
        maturity = parse_date(inv["maturity_date"])
        if scenario == SCENARIO_WORST:
            maturity = maturity + timedelta(
                days=int(params.get("maturity_delay_days", 0))
            )
        mat_idx = _day_index(maturity, as_of, horizon)
        if mat_idx is not None:
            proceeds = money(inv["amount"] + float(inv.get("expected_proceeds", 0.0)))
            add_flow(currency, mat_idx, proceeds)

    return flows, reserves


def build_forecast(
    state: dict[str, Any],
    as_of: Any,
    horizon_days: int | None = None,
    candidate: dict[str, Any] | None = None,
) -> Forecast:
    rev = current_assumptions(state)
    params = {**DEFAULT_ASSUMPTIONS, **rev["params"]}
    horizon = int(horizon_days or params["horizon_days"])
    opening = bank_balances(state)

    sim_state: dict[str, Any] = {
        "accounts": state["accounts"],
        "bank_entries": state["bank_entries"],
        "cash_events": state["cash_events"],
        "investments": list(state["investments"]),
    }
    if candidate is not None:
        sim_state["investments"].append(candidate)

    currencies = set(opening)
    for ev in sim_state["cash_events"]:
        currencies.add(ev["currency"])
    for inv in sim_state["investments"]:
        currencies.add(inv["currency"])
    currencies = set(currencies)

    per_scenario: dict[str, tuple[dict, dict]] = {}
    for scenario in SCENARIOS:
        per_scenario[scenario] = _collect_flows(
            sim_state, scenario, as_of, horizon, params
        )

    daily: dict[str, list[dict[str, Any]]] = {}
    min_balance: dict[str, dict[str, float]] = {}
    for scenario in SCENARIOS:
        flows, _ = per_scenario[scenario]
        rows: list[dict[str, Any]] = []
        min_by_currency: dict[str, float] = {}
        for c in currencies:
            balance = opening.get(c, 0.0)
            flows_c = flows.get(c, {})
            for i in range(horizon + 1):
                net = money(flows_c.get(i, 0.0))
                balance = money(balance + net)
                rows.append(
                    {
                        "date": (as_of + timedelta(days=i)).isoformat(),
                        "currency": c,
                        "net_flow": net,
                        "balance": balance,
                    }
                )
                if c not in min_by_currency or balance < min_by_currency[c]:
                    min_by_currency[c] = balance
        rows.sort(key=lambda r: (r["date"], r["currency"]))
        daily[scenario] = rows
        min_balance[scenario] = min_by_currency

    worst_rows = {(r["date"], r["currency"]): r for r in daily[SCENARIO_WORST]}
    variance = [
        {
            "date": r["date"],
            "currency": r["currency"],
            "base_balance": r["balance"],
            "worst_balance": worst_rows[(r["date"], r["currency"])]["balance"],
            "delta": money(
                worst_rows[(r["date"], r["currency"])]["balance"] - r["balance"]
            ),
        }
        for r in daily[SCENARIO_BASE]
    ]

    reserves: dict[str, dict[str, float]] = {}
    investable_cap: dict[str, float] = {}
    floor = money(params["min_reserve_balance"])
    _, reserves_base = per_scenario[SCENARIO_BASE]
    _, reserves_worst = per_scenario[SCENARIO_WORST]
    for c in currencies:
        rb = reserves_base.get(c, {})
        rw = reserves_worst.get(c, {})
        reserves[c] = {
            "payroll": money(rb.get("payroll", 0.0)),
            "refunds_base": money(rb.get("refund", 0.0)),
            "refunds_worst": money(rw.get("refund", 0.0)),
            "tournament_fees": money(rw.get("tournament_fee", 0.0)),
            "min_reserve": floor,
        }
        # 可投资上限：最坏情景滚动最低点扣除保底金额后的余量
        investable_cap[c] = money(
            max(0.0, min_balance[SCENARIO_WORST].get(c, 0.0) - floor)
        )

    return Forecast(
        as_of=as_of,
        horizon_days=horizon,
        assumption_version=int(rev["version"]),
        assumptions=params,
        opening={c: opening.get(c, 0.0) for c in sorted(currencies)},
        daily=daily,
        min_balance=min_balance,
        reserves=reserves,
        investable_cap=investable_cap,
        variance=variance,
    )
