"""状态投影：重放事件日志，重建账户、现金流计划、产品、订单与告警。

两遍归约：
1. 顺序应用所有事件，建立计划项并收集银行流水（流水可能先于或晚于计划项到达）；
2. 按 ref_type/ref_id 把引用了计划项/订单的流水挂接上去——与到达顺序、
   价值日先后无关，保证乱序到达幂等；是否已实现由预测按 as_of + 价值日判定。

银行流水是资金移动的唯一权威来源（手工结算亦写成流水）。投资申购扣款与
到期回款是真实的银行账户进出，计入银行余额；预测引擎只对"尚无对应流水"
的部分补预期流，因此重复流水（幂等键拦截）与乱序到达都不会重复计算。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable

REF_INFLOW = {"membership_due", "sponsor_receipt"}
REF_OUTFLOW = {"contract", "payroll", "refund"}
INVESTMENT_REFS = {"investment_order", "investment_maturity"}


class State:
    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.lines: list[dict[str, Any]] = []
        self.inflows: dict[str, dict[str, Any]] = {}
        self.contracts: dict[str, dict[str, Any]] = {}
        self.payrolls: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.reserve_cents: int = 0
        self.products: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.assumptions: list[dict[str, Any]] = []
        self.alerts: dict[str, dict[str, Any]] = {}
        self.risk_evals: list[dict[str, Any]] = []
        self.last_seq: int = 0

    def obligations(self) -> dict[str, dict[str, Any]]:
        """ref_type:ref_id -> 计划项，供流水匹配。"""
        reg: dict[str, dict[str, Any]] = {}
        for kind, table in (
            ("membership_due", self.inflows),
            ("sponsor_receipt", self.inflows),
            ("contract", self.contracts),
            ("payroll", self.payrolls),
            ("refund", self.refunds),
        ):
            for iid, item in table.items():
                reg[f"{kind}:{iid}"] = item
        return reg

    def current_assumptions(self) -> dict[str, Any] | None:
        return self.assumptions[-1] if self.assumptions else None

    def open_alert(self, kind: str) -> dict[str, Any] | None:
        for a in self.alerts.values():
            if a["kind"] == kind and a["status"] == "open":
                return a
        return None


def _new_item(p: dict[str, Any], item_id: str, date_field: str, label: str) -> dict[str, Any]:
    amount = p["amount_cents"]
    return {
        "id": item_id,
        "name": p.get("name") or p.get("party") or p.get("member") or label,
        "amount_cents": amount,
        "matched_lines": [],  # {key, amount_cents, value_date, sign}
        date_field: date.fromisoformat(p[date_field]),
    }


def realized_cents(item: dict[str, Any], as_of: date) -> int:
    """价值日 <= as_of 的已匹配流水金额（取绝对值）。"""
    return sum(
        ln["amount_cents"]
        for ln in item["matched_lines"]
        if ln["value_date"] <= as_of
    )


def outstanding_cents(item: dict[str, Any], as_of: date) -> int:
    return max(0, item["amount_cents"] - realized_cents(item, as_of))


def build_state(events: Iterable[dict[str, Any]]) -> State:
    s = State()
    for ev in events:
        s.last_seq = ev["seq"]
        _apply(s, ev["type"], ev["payload"], ev)
    _link_references(s)
    return s


def _apply(s: State, t: str, p: dict[str, Any], ev: dict[str, Any]) -> None:
    if t == "account.registered":
        s.accounts[p["account_id"]] = {
            "id": p["account_id"],
            "name": p["name"],
            "currency": p.get("currency", "CNY"),
        }

    elif t == "statement.line_recorded":
        s.lines.append(
            {
                "key": ev["key"],
                "seq": ev["seq"],
                "account_id": p["account_id"],
                "amount_cents": p["amount_cents"],
                "value_date": date.fromisoformat(p["value_date"]),
                "recorded_at": ev["recorded_at"],
                "ref_type": p.get("ref_type"),
                "ref_id": p.get("ref_id"),
                "description": p.get("description", ""),
                "matched": False,
            }
        )

    elif t == "inflow.scheduled":
        s.inflows[p["flow_id"]] = {
            **_new_item(p, p["flow_id"], "due_date", p["kind"]),
            "kind": p["kind"],
            "flow_id": p["flow_id"],
        }

    elif t == "contract.signed":
        s.contracts[p["contract_id"]] = {
            **_new_item(p, p["contract_id"], "pay_date", "赛事合同"),
            "contract_id": p["contract_id"],
        }

    elif t == "payroll.scheduled":
        s.payrolls[p["payroll_id"]] = {
            **_new_item(p, p["payroll_id"], "date", "工资税费"),
            "payroll_id": p["payroll_id"],
        }

    elif t == "refund.scheduled":
        s.refunds[p["refund_id"]] = {
            **_new_item(p, p["refund_id"], "date", "家长退款"),
            "member": p.get("member", ""),
            "refund_id": p["refund_id"],
        }

    elif t == "refund.reserve_set":
        s.reserve_cents = p["amount_cents"]

    elif t == "product.defined":
        s.products[p["product_id"]] = {
            "id": p["product_id"],
            "name": p["name"],
            "maturity_days": p["maturity_days"],
            "annual_rate": p.get("annual_rate", "0"),
            "risk": p.get("risk", "low"),
        }

    elif t == "order.proposed":
        s.orders[p["order_id"]] = {
            "id": p["order_id"],
            "product_id": p["product_id"],
            "amount_cents": p["amount_cents"],
            "decision_state": "proposed",
            "proposed_at": p["proposed_at"],
            "as_of": p["as_of"],
            "settlement_date": date.fromisoformat(p["settlement_date"]),
            "maturity_date": date.fromisoformat(p["maturity_date"]),
            "expected_proceeds_cents": p["expected_proceeds_cents"],
            "assumptions_version": p.get("assumptions_version", 0),
            "decision": None,
            "settlement_lines": [],
            "maturity_lines": [],
        }

    elif t in ("order.approved", "order.blocked"):
        o = s.orders.get(p["order_id"])
        if o is not None:
            o["decision_state"] = "approved" if t == "order.approved" else "blocked"
            o["decision"] = p["decision"]

    elif t == "assumptions.versioned":
        s.assumptions.append(p)

    elif t == "alert.raised":
        s.alerts[p["alert_id"]] = {
            "id": p["alert_id"],
            "kind": p["kind"],
            "status": "open",
            "scenario": p.get("scenario"),
            "reason": p.get("reason", ""),
            "detail": p.get("detail", {}),
            "raised_at": ev["recorded_at"],
            "resolved_at": None,
        }

    elif t == "alert.resolved":
        a = s.alerts.get(p["alert_id"])
        if a is not None:
            a["status"] = "resolved"
            a["resolved_at"] = ev["recorded_at"]

    elif t == "risk.evaluated":
        s.risk_evals.append(p)


def _attach(line: dict[str, Any], bucket: list[dict[str, Any]]) -> None:
    bucket.append(
        {
            "key": line["key"],
            "amount_cents": abs(line["amount_cents"]),
            "value_date": line["value_date"],
        }
    )
    line["matched"] = True


def _link_references(s: State) -> None:
    """第二遍：银行流水与计划项/订单挂接，乱序与重复均安全。"""
    registry = s.obligations()
    for line in s.lines:
        rt, rid = line.get("ref_type"), line.get("ref_id")
        if not rt or not rid:
            continue
        if rt in REF_INFLOW | REF_OUTFLOW:
            item = registry.get(f"{rt}:{rid}")
            if item is not None:
                _attach(line, item["matched_lines"])
        elif rt == "investment_order":
            o = s.orders.get(rid)
            if o is not None:
                _attach(line, o["settlement_lines"])
        elif rt == "investment_maturity":
            o = s.orders.get(rid)
            if o is not None:
                _attach(line, o["maturity_lines"])


def order_lifecycle(o: dict[str, Any], as_of: date) -> str:
    """根据决策状态与银行流水价值日推导订单生命周期状态。"""
    if o["decision_state"] == "blocked":
        return "blocked"
    settled = any(ln["value_date"] <= as_of for ln in o["settlement_lines"])
    matured = any(ln["value_date"] <= as_of for ln in o["maturity_lines"])
    if matured:
        return "matured"
    if settled:
        return "settled"
    return o["decision_state"]  # proposed / approved
