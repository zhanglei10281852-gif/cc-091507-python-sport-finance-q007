from __future__ import annotations

from datetime import date
from typing import Any

from domain import (
    CURRENCIES,
    EVENT_TYPES,
    SCENARIO_WORST,
    money,
    now_iso,
    parse_date,
)
from forecast import build_forecast, current_assumptions


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        raise ApiError(400, f"缺少字段: {key}")
    return payload[key]


def _amount(payload: dict[str, Any], key: str = "amount") -> float:
    raw = _require(payload, key)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"{key} 必须是数字") from exc
    if value <= 0:
        raise ApiError(400, f"{key} 必须为正数")
    return money(value)


def _currency(payload: dict[str, Any]) -> str:
    currency = _require(payload, "currency")
    if currency not in CURRENCIES:
        raise ApiError(400, f"不支持的币种: {currency}")
    return currency


def _date(payload: dict[str, Any], key: str) -> Any:
    raw = _require(payload, key)
    try:
        return parse_date(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"{key} 必须是 YYYY-MM-DD") from exc


def _next_id(state: dict[str, Any], kind: str, prefix: str) -> str:
    n = int(state["seq"].get(kind, 0)) + 1
    state["seq"][kind] = n
    return f"{prefix}-{n:04d}"


def idem_get(state: dict[str, Any], key: str) -> dict[str, Any] | None:
    return state["idempotency"].get(key)


def idem_put(state: dict[str, Any], key: str, kind: str, ref_id: str, result: dict[str, Any]) -> None:
    state["idempotency"][key] = {
        "kind": kind,
        "ref_id": ref_id,
        "received_at": now_iso(),
        "result": result,
    }


# ---------- 基础数据 ----------

def create_account(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    account_id = str(_require(payload, "account_id"))
    currency = _currency(payload)
    if any(a["account_id"] == account_id for a in state["accounts"]):
        raise ApiError(409, f"账户已存在: {account_id}")
    account = {
        "account_id": account_id,
        "currency": currency,
        "name": payload.get("name", account_id),
        "opened_at": now_iso(),
    }
    state["accounts"].append(account)
    return account


def add_bank_entry(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    account_id = str(_require(payload, "account_id"))
    account = next(
        (a for a in state["accounts"] if a["account_id"] == account_id), None
    )
    if account is None:
        raise ApiError(404, f"账户不存在: {account_id}")
    amount = _amount(payload)
    if payload.get("direction", "credit") == "debit":
        amount = -amount
    value_date = _date(payload, "value_date")
    entry = {
        "entry_id": _next_id(state, "bank_entry", "BE"),
        "account_id": account_id,
        "currency": account["currency"],
        "amount": money(amount),
        "value_date": value_date.isoformat(),
        "received_at": payload.get("received_at") or now_iso(),
        "memo": payload.get("memo", ""),
        "ref": payload.get("ref", ""),
    }
    state["bank_entries"].append(entry)
    return entry


def add_event(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    etype = str(_require(payload, "type"))
    if etype not in EVENT_TYPES:
        raise ApiError(400, f"未知事件类型: {etype}")
    event = {
        "event_id": _next_id(state, "event", "EV"),
        "type": etype,
        "amount": _amount(payload),
        "currency": _currency(payload),
        "scheduled_date": _date(payload, "scheduled_date").isoformat(),
        "contract_ref": payload.get("contract_ref", ""),
        "committed": bool(payload.get("committed", True)),
        "realized": bool(payload.get("realized", False)),
        "note": payload.get("note", ""),
        "created_at": now_iso(),
    }
    state["cash_events"].append(event)
    return event


# ---------- 假设版本 ----------

def update_assumptions(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    params = payload.get("params")
    if not isinstance(params, dict) or not params:
        raise ApiError(400, "params 必须是非空对象")
    cur = current_assumptions(state)
    merged = {**cur["params"], **params}
    rev = {
        "version": int(cur["version"]) + 1,
        "params": merged,
        "changed_by": payload.get("changed_by", "admin"),
        "reason": payload.get("reason", ""),
        "created_at": now_iso(),
    }
    state["assumptions"].append(rev)
    return rev


# ---------- 资金闸门 ----------

def _gate_violations(
    state: dict[str, Any], candidate: dict[str, Any], as_of: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # 可投资上限以不含候选批次的最坏情景计算
    cap_before = build_forecast(state, as_of).investable_cap.get(
        candidate["currency"], 0.0
    )
    # 加入候选批次后滚动预测，检查窗口内是否击穿保底
    forecast = build_forecast(state, as_of, candidate=candidate)
    floor = money(forecast.assumptions["min_reserve_balance"])
    violations: list[dict[str, Any]] = []
    for row in forecast.daily[SCENARIO_WORST]:
        if row["balance"] < floor:
            violations.append(
                {
                    "scenario": SCENARIO_WORST,
                    "date": row["date"],
                    "currency": row["currency"],
                    "projected_balance": row["balance"],
                    "required_minimum": floor,
                    "gap": money(floor - row["balance"]),
                }
            )
    if money(candidate["amount"]) > money(cap_before):
        violations.append(
            {
                "scenario": SCENARIO_WORST,
                "date": None,
                "currency": candidate["currency"],
                "projected_balance": None,
                "required_minimum": floor,
                "gap": money(candidate["amount"] - cap_before),
                "reason": "amount_exceeds_investable_cap",
            }
        )
    snapshot = forecast.to_dict()
    snapshot["investable_cap"] = forecast.investable_cap
    snapshot["investable_cap_before_order"] = {candidate["currency"]: cap_before}
    return violations, snapshot


def _raise_alert(
    state: dict[str, Any], investment_id: str, violations: list[dict[str, Any]]
) -> dict[str, Any]:
    alert = {
        "alert_id": _next_id(state, "alert", "AL"),
        "kind": "gate_blocked",
        "status": "open",
        "investment_id": investment_id,
        "trigger_scenario": SCENARIO_WORST,
        "violations": violations,
        "created_at": now_iso(),
        "acknowledged_at": None,
        "acknowledged_by": None,
    }
    state["alerts"].append(alert)
    return alert


def propose_investment(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """登记投资申请（proposed）；不自动过闸，等待 evaluate_gate 审批。"""
    order_date = _date(payload, "order_date")
    settle_date = _date(payload, "settle_date")
    maturity_date = _date(payload, "maturity_date")
    if not order_date <= settle_date <= maturity_date:
        raise ApiError(400, "日期需满足 order_date <= settle_date <= maturity_date")
    investment = {
        "investment_id": _next_id(state, "investment", "INV"),
        "product": str(_require(payload, "product")),
        "amount": _amount(payload),
        "currency": _currency(payload),
        "order_date": order_date.isoformat(),
        "settle_date": settle_date.isoformat(),
        "maturity_date": maturity_date.isoformat(),
        "expected_proceeds": money(payload.get("expected_proceeds", 0.0)),
        "state": "proposed",
        "assumption_version_at_decision": None,
        "gate": None,
        "created_at": now_iso(),
        "decided_at": None,
        "maturity_received": None,
    }
    state["investments"].append(investment)
    if bool(payload.get("auto_evaluate", False)):
        evaluate_gate(state, investment, order_date)
    return investment


def evaluate_investment(state: dict[str, Any], investment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    inv = _get_investment(state, investment_id)
    as_of = parse_date(payload["as_of"]) if payload.get("as_of") else None
    gate = evaluate_gate(state, inv, as_of)
    return {"investment": inv, "gate": gate}


def evaluate_gate(
    state: dict[str, Any], investment: dict[str, Any], as_of: date | None = None
) -> dict[str, Any]:
    """对单笔 proposed 批次执行资金闸门；幂等：已批准/已阻止的批次不再改判。"""
    if investment["state"] != "proposed":
        return investment["gate"] or {}
    as_of = as_of or parse_date(investment["order_date"])
    candidate = {
        "state": "approved",
        "currency": investment["currency"],
        "amount": investment["amount"],
        "order_date": investment["order_date"],
        "settle_date": investment["settle_date"],
        "maturity_date": investment["maturity_date"],
        "expected_proceeds": investment["expected_proceeds"],
    }
    violations, forecast_snapshot = _gate_violations(state, candidate, as_of)
    rev = current_assumptions(state)
    gate = {
        "evaluated_at": now_iso(),
        "assumption_version": rev["version"],
        "decision": "blocked" if violations else "approved",
        "violations": violations,
        "investable_cap_at_decision": forecast_snapshot["investable_cap_before_order"][
            investment["currency"]
        ],
        "forecast_snapshot": forecast_snapshot,
    }
    investment["gate"] = gate
    investment["assumption_version_at_decision"] = rev["version"]
    investment["decided_at"] = gate["evaluated_at"]
    if violations:
        investment["state"] = "blocked"
        _raise_alert(state, investment["investment_id"], violations)
    else:
        investment["state"] = "approved"
    return gate


def settle_investment(state: dict[str, Any], investment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    inv = _get_investment(state, investment_id)
    if inv["state"] != "approved":
        raise ApiError(409, f"批次状态为 {inv['state']}，无法结算")
    account_id = str(_require(payload, "account_id"))
    account = next(
        (a for a in state["accounts"] if a["account_id"] == account_id), None
    )
    if account is None:
        raise ApiError(404, f"账户不存在: {account_id}")
    if account["currency"] != inv["currency"]:
        raise ApiError(400, "结算账户币种与批次不一致")
    entry = {
        "entry_id": _next_id(state, "bank_entry", "BE"),
        "account_id": account_id,
        "currency": inv["currency"],
        "amount": -money(inv["amount"]),
        "value_date": payload.get("value_date") or inv["settle_date"],
        "received_at": now_iso(),
        "memo": f"投资下单 {inv['investment_id']} ({inv['product']})",
        "ref": inv["investment_id"],
    }
    state["bank_entries"].append(entry)
    inv["state"] = "settled"
    inv["settle_entry_id"] = entry["entry_id"]
    return {"investment": inv, "bank_entry": entry}


def receive_maturity(state: dict[str, Any], investment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """产品到期回款。允许乱序：重复回款幂等返回，实际金额/日期以首次登记为准。"""
    inv = _get_investment(state, investment_id)
    if inv.get("maturity_received"):
        return {"investment": inv, "bank_entry": _entry_by_ref(state, inv["investment_id"], "maturity"),
                "replayed": True}
    if inv["state"] not in ("approved", "settled"):
        raise ApiError(409, f"批次状态为 {inv['state']}，无到期回款")
    account_id = str(_require(payload, "account_id"))
    account = next(
        (a for a in state["accounts"] if a["account_id"] == account_id), None
    )
    if account is None:
        raise ApiError(404, f"账户不存在: {account_id}")
    if account["currency"] != inv["currency"]:
        raise ApiError(400, "回款账户币种与批次不一致")
    proceeds = money(payload.get("proceeds", inv["amount"] + inv["expected_proceeds"]))
    value_date = payload.get("value_date") or inv["maturity_date"]
    entry = {
        "entry_id": _next_id(state, "bank_entry", "BE"),
        "account_id": account_id,
        "currency": inv["currency"],
        "amount": proceeds,
        "value_date": value_date,
        "received_at": payload.get("received_at") or now_iso(),
        "memo": f"到期回款 {inv['investment_id']} ({inv['product']})",
        "ref": f"{inv['investment_id']}:maturity",
    }
    state["bank_entries"].append(entry)
    inv["state"] = "matured"
    inv["maturity_received"] = {
        "entry_id": entry["entry_id"],
        "amount": proceeds,
        "value_date": value_date,
        "received_at": entry["received_at"],
    }
    return {"investment": inv, "bank_entry": entry}


def _entry_by_ref(state: dict[str, Any], investment_id: str, kind: str) -> dict[str, Any] | None:
    ref = investment_id if kind != "maturity" else f"{investment_id}:maturity"
    return next((e for e in state["bank_entries"] if e["ref"] == ref), None)


def _get_investment(state: dict[str, Any], investment_id: str) -> dict[str, Any]:
    inv = next(
        (i for i in state["investments"] if i["investment_id"] == investment_id),
        None,
    )
    if inv is None:
        raise ApiError(404, f"批次不存在: {investment_id}")
    return inv


def acknowledge_alert(state: dict[str, Any], alert_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    alert = next((a for a in state["alerts"] if a["alert_id"] == alert_id), None)
    if alert is None:
        raise ApiError(404, f"告警不存在: {alert_id}")
    if alert["status"] == "open":
        alert["status"] = "acknowledged"
        alert["acknowledged_at"] = now_iso()
        alert["acknowledged_by"] = payload.get("by", "admin")
    return alert


# ---------- 查询视图 ----------

def position_view(state: dict[str, Any], as_of: date, horizon: int | None) -> dict[str, Any]:
    forecast = build_forecast(state, as_of, horizon_days=horizon)
    frozen: list[dict[str, Any]] = []
    # 刚性准备金（以最坏情景口径）
    for currency, items in forecast.reserves.items():
        for key, label in (
            ("payroll", "工资税费"),
            ("refunds_worst", "家长退款准备金(最坏情景)"),
            ("tournament_fees", "已签赛事报名费"),
            ("min_reserve", "最低保底余额"),
        ):
            if items.get(key):
                frozen.append(
                    {"reason": label, "currency": currency, "amount": items[key]}
                )
    # 已批准/在途批次占用
    for inv in state["investments"]:
        if inv["state"] in ("approved", "settled"):
            frozen.append(
                {
                    "reason": f"在投/待结算批次 {inv['investment_id']} ({inv['product']})",
                    "currency": inv["currency"],
                    "amount": inv["amount"],
                    "ref": inv["investment_id"],
                    "maturity_date": inv["maturity_date"],
                }
            )
    return {
        "as_of": as_of.isoformat(),
        "bank_balances": forecast.opening,
        "reserves": forecast.reserves,
        "frozen_funds": frozen,
        "investable_cap": forecast.investable_cap,
        "assumption_version": forecast.assumption_version,
        "forecast": forecast.to_dict(),
    }


def maturity_schedule(state: dict[str, Any]) -> dict[str, Any]:
    rows = [
        {
            "investment_id": inv["investment_id"],
            "product": inv["product"],
            "currency": inv["currency"],
            "principal": inv["amount"],
            "expected_proceeds": money(
                inv["amount"] + float(inv.get("expected_proceeds", 0.0))
            ),
            "maturity_date": inv["maturity_date"],
            "state": inv["state"],
            "received": inv.get("maturity_received"),
        }
        for inv in sorted(state["investments"], key=lambda i: i["maturity_date"])
        if inv["state"] in ("approved", "settled", "matured")
    ]
    return {"schedule": rows}


def recover_pending(state: dict[str, Any], as_of: date | None = None) -> dict[str, Any]:
    """重启恢复：补判未完成的闸门审批（按批次自身下单日，已决批次不改判），
    并汇总仍未处理的风险告警。"""
    decided: list[str] = []
    for inv in state["investments"]:
        if inv["state"] == "proposed":
            evaluate_gate(state, inv, parse_date(inv["order_date"]))
            decided.append(inv["investment_id"])
    open_alerts = [
        {"alert_id": a["alert_id"], "investment_id": a["investment_id"],
         "kind": a["kind"], "created_at": a["created_at"]}
        for a in state["alerts"] if a["status"] == "open"
    ]
    return {"reevaluated": decided, "open_alerts": open_alerts}


def get_investment(state: dict[str, Any], investment_id: str) -> dict[str, Any]:
    return _get_investment(state, investment_id)


def assumptions_view(state: dict[str, Any]) -> dict[str, Any]:
    cur = current_assumptions(state)
    return {
        "current": cur,
        "history": state["assumptions"],
        "note": "调整假设只产生新版本，不回溯改写已批准批次（批次保存当时版本与闸门快照）",
    }


def build_forecast_safe(state: dict[str, Any], as_of: date, horizon: int | None) -> dict[str, Any]:
    return build_forecast(state, as_of, horizon_days=horizon).to_dict()


def frozen_view(state: dict[str, Any], as_of: date, horizon: int | None) -> dict[str, Any]:
    return {"frozen_funds": position_view(state, as_of, horizon)["frozen_funds"]}
