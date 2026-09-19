"""应用服务层：命令处理、资金闸门评审、后台审批/告警处理器与只读视图。

后台处理器（_worker）扫描两类未完成工作：
1. decision_state=proposed 的订单：按提议时锁定的假设版本跑闸门，追加
   order.approved / order.blocked（含触发情景与首日缺口的不可变决策快照），
   阻断时产生 gate_blocked 告警；
2. 流动性告警：按"当前"假设重算，缺口出现则告警、消失则解除。

所有工作项都来自事件日志：进程重启后重放日志即可继续处理，无需额外队列。
"""
from __future__ import annotations

import copy
import logging
import threading
from datetime import date, timedelta
from typing import Any

from . import forecast
from .common import Clock, NotFoundError, ValidationError, iso, money, yuan
from .events import EventStore
from .forecast import (
    DEFAULT_ASSUMPTIONS,
    SCENARIOS,
    ExtraOrder,
    evaluate_gate,
    frozen_funds,
    liquidity_risk,
    projected_proceeds,
    run_scenario,
    series_summary,
)
from .state import State, build_state, order_lifecycle

log = logging.getLogger("cashgate")

VALID_LINE_REFS = (
    "membership_due",
    "sponsor_receipt",
    "contract",
    "payroll",
    "refund",
    "investment_order",
    "investment_maturity",
)
VALID_INFLOW_KINDS = ("membership_due", "sponsor_receipt")


class CashGateService:
    def __init__(self, store: EventStore, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock or Clock()
        self._lock = threading.RLock()
        self._wake = threading.Condition()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    # ============ 生命周期 ============
    def start_worker(self, interval: float = 0.2) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, args=(interval,), name="cashgate-worker", daemon=True
        )
        self._worker.start()

    def stop_worker(self) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=2)
            self._worker = None

    def _worker_loop(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                did = self.process_due()
            except Exception:  # pragma: no cover - 防御：单轮失败不杀线程
                log.exception("gate worker iteration failed")
                did = False
            with self._wake:
                self._wake.wait(interval if not did else 0.01)

    def process_due(self) -> bool:
        """执行一轮未完成审批与告警处理；有变更返回 True。供 worker 与测试调用。"""
        with self._lock:
            return self._process_pending_orders() | self._process_alerts()

    # ============ 基础工具 ============
    def _snapshot(self) -> State:
        return build_state(self._store.replay())

    def _append(self, event_type: str, payload: dict[str, Any], key: str | None = None):
        return self._store.append(event_type, payload, key)

    def _as_of(self, raw: str | None) -> date:
        if not raw:
            return self._clock.today()
        try:
            return date.fromisoformat(raw)
        except ValueError as e:
            raise ValidationError(f"无效日期: {raw}") from e

    def _cents(
        self,
        body: dict[str, Any],
        field: str = "amount",
        required: bool = True,
        allow_negative: bool = False,
    ) -> int:
        if field in body:
            try:
                value = money(body[field])
            except Exception as e:
                raise ValidationError(f"金额 {field} 无法解析") from e
        elif f"{field}_cents" in body:
            try:
                value = int(body[f"{field}_cents"])
            except (TypeError, ValueError) as e:
                raise ValidationError(f"金额 {field}_cents 必须为整数分") from e
        elif required:
            raise ValidationError(f"缺少金额字段 {field}")
        else:
            return 0
        if required and value == 0:
            raise ValidationError(f"金额 {field} 不能为零")
        if required and value < 0 and not allow_negative:
            raise ValidationError(f"金额 {field} 必须为正数")
        return value

    def _assumptions(self, state: State) -> dict[str, Any]:
        cur = state.current_assumptions()
        return copy.deepcopy(cur["assumptions"]) if cur else copy.deepcopy(DEFAULT_ASSUMPTIONS)

    def _assumptions_version(self, state: State, version: int) -> dict[str, Any]:
        for v in state.assumptions:
            if v["version"] == version:
                return copy.deepcopy(v["assumptions"])
        return copy.deepcopy(DEFAULT_ASSUMPTIONS)

    # ============ 账户与银行流水 ============
    def register_account(self, body: dict[str, Any]) -> dict[str, Any]:
        account_id = body.get("account_id") or self._store.next_identity("acct")
        name = str(body.get("name") or account_id)
        currency = str(body.get("currency", "CNY")).upper()
        if currency not in ("CNY", "HKD", "USD"):
            raise ValidationError("不支持的币种")
        event, dup = self._append(
            "account.registered",
            {"account_id": account_id, "name": name, "currency": currency},
            body.get("idempotency_key"),
        )
        state = self._snapshot()
        return self._account_view(state.accounts[account_id], dup)

    def record_statement_line(self, body: dict[str, Any]) -> dict[str, Any]:
        key = body.get("idempotency_key")
        if not key:
            raise ValidationError("银行流水必须提供 idempotency_key 以保证对账幂等")
        account_id = body.get("account_id")
        if not account_id:
            raise ValidationError("缺少 account_id")
        amount = self._cents(body, "amount", allow_negative=True)
        value_date = self._date(body.get("value_date"), "value_date")
        ref_type = body.get("ref_type")
        if ref_type is not None and ref_type not in VALID_LINE_REFS:
            raise ValidationError(f"ref_type 非法: {ref_type}")
        state = self._snapshot()
        if account_id not in state.accounts:
            raise NotFoundError(f"账户不存在: {account_id}")
        ref_id = body.get("ref_id")
        if bool(ref_type) ^ bool(ref_id):
            raise ValidationError("ref_type 与 ref_id 必须同时提供")
        payload = {
            "account_id": account_id,
            "amount_cents": amount,
            "value_date": value_date.isoformat(),
            "ref_type": ref_type,
            "ref_id": ref_id,
            "description": str(body.get("description", "")),
        }
        event, dup = self._append("statement.line_recorded", payload, str(key))
        return self._line_view(event["payload"], event["recorded_at"], event["key"], dup)

    def _date(self, raw: str | None, field: str) -> date:
        if not raw:
            raise ValidationError(f"缺少日期 {field}")
        try:
            return date.fromisoformat(raw)
        except ValueError as e:
            raise ValidationError(f"日期 {field} 非法: {raw}") from e

    # ============ 计划现金流 ============
    def schedule_inflow(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = body.get("kind")
        if kind not in VALID_INFLOW_KINDS:
            raise ValidationError(f"kind 必须为 {VALID_INFLOW_KINDS}")
        amount = self._cents(body)
        due = self._date(body.get("due_date"), "due_date")
        flow_id = body.get("flow_id") or self._store.next_identity(
            "due" if kind == "membership_due" else "spn"
        )
        payload = {
            "flow_id": flow_id,
            "kind": kind,
            "party": str(body.get("party", "")),
            "amount_cents": amount,
            "due_date": due.isoformat(),
        }
        event, dup = self._append("inflow.scheduled", payload, body.get("idempotency_key"))
        flow_id = event["payload"]["flow_id"]
        state = self._snapshot()
        return self._inflow_view(state.inflows[flow_id], self._clock.today(), dup)

    def sign_contract(self, body: dict[str, Any]) -> dict[str, Any]:
        amount = self._cents(body)
        pay_date = self._date(body.get("pay_date"), "pay_date")
        cid = body.get("contract_id") or self._store.next_identity("ctr")
        payload = {
            "contract_id": cid,
            "name": str(body.get("name", "赛事合同")),
            "amount_cents": amount,
            "pay_date": pay_date.isoformat(),
        }
        event, dup = self._append("contract.signed", payload, body.get("idempotency_key"))
        cid = event["payload"]["contract_id"]
        state = self._snapshot()
        return self._contract_view(state.contracts[cid], dup)

    def schedule_payroll(self, body: dict[str, Any]) -> dict[str, Any]:
        amount = self._cents(body)
        d = self._date(body.get("date"), "date")
        pid = body.get("payroll_id") or self._store.next_identity("pay")
        payload = {
            "payroll_id": pid,
            "name": str(body.get("name", "工资税费")),
            "amount_cents": amount,
            "date": d.isoformat(),
        }
        event, dup = self._append("payroll.scheduled", payload, body.get("idempotency_key"))
        pid = event["payload"]["payroll_id"]
        state = self._snapshot()
        return self._payroll_view(state.payrolls[pid], dup)

    def schedule_refund(self, body: dict[str, Any]) -> dict[str, Any]:
        amount = self._cents(body)
        d = self._date(body.get("date"), "date")
        rid = body.get("refund_id") or self._store.next_identity("rfd")
        payload = {
            "refund_id": rid,
            "member": str(body.get("member", "")),
            "amount_cents": amount,
            "date": d.isoformat(),
        }
        event, dup = self._append("refund.scheduled", payload, body.get("idempotency_key"))
        state = self._snapshot()
        return self._refund_view(state.refunds[rid], self._clock.today(), dup)

    def set_refund_reserve(self, body: dict[str, Any]) -> dict[str, Any]:
        amount = self._cents(body, "amount")
        event, dup = self._append(
            "refund.reserve_set", {"amount_cents": amount}, body.get("idempotency_key")
        )
        return {"refund_reserve_cents": amount, "refund_reserve": yuan(amount), "duplicate": dup}

    # ============ 产品 ============
    def define_product(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "短期理财")
        try:
            days = int(body["maturity_days"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValidationError("maturity_days 必须为正整数") from e
        if days <= 0:
            raise ValidationError("maturity_days 必须为正整数")
        rate = str(body.get("annual_rate", "0"))
        try:
            if float(rate) < 0:
                raise ValueError
        except ValueError as e:
            raise ValidationError("annual_rate 非法") from e
        pid = body.get("product_id") or self._store.next_identity("prd")
        payload = {
            "product_id": pid,
            "name": name,
            "maturity_days": days,
            "annual_rate": rate,
            "risk": str(body.get("risk", "low")),
        }
        event, dup = self._append("product.defined", payload, body.get("idempotency_key"))
        state = self._snapshot()
        return {**state.products[pid], "duplicate": dup}

    # ============ 预测假设（版本化，不改写历史） ============
    def update_assumptions(self, body: dict[str, Any]) -> dict[str, Any]:
        state = self._snapshot()
        base = self._assumptions(state)
        merged = self._merge_assumptions(base, body)
        version = (state.current_assumptions() or {}).get("version", 0) + 1 if state.assumptions else 1
        payload = {
            "version": version,
            "assumptions": merged,
            "effective_from": self._clock.today().isoformat(),
            "note": str(body.get("note", "")),
        }
        event, dup = self._append("assumptions.versioned", payload, body.get("idempotency_key"))
        if dup:
            payload = event["payload"]
        return {**payload, "duplicate": dup}

    def _merge_assumptions(self, base: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        merged = copy.deepcopy(base)
        scenario_tables = (
            "inflow_delay_days",
            "inflow_haircut_pct",
            "outflow_advance_days",
            "refund_surge_pct",
        )
        for table in scenario_tables:
            if table in body:
                value = body[table]
                if not isinstance(value, dict):
                    raise ValidationError(f"{table} 必须是各情景映射")
                for scn, num in value.items():
                    if scn not in SCENARIOS:
                        raise ValidationError(f"未知情景: {scn}")
                    if not isinstance(num, int) or num < 0:
                        raise ValidationError(f"{table}.{scn} 必须为非负整数")
                merged[table] = {scn: int(value.get(scn, merged[table][scn])) for scn in SCENARIOS}
        # 单调性：stress 不得轻于 adverse 不得轻于 base
        if merged["inflow_delay_days"]["stress"] < merged["inflow_delay_days"]["adverse"]:
            raise ValidationError("stress 的流入延后不得小于 adverse")
        if merged["refund_surge_pct"]["base"] != 0:
            raise ValidationError("base 情景的退款激增必须为 0")
        if "min_buffer_cents" in body:
            try:
                buf = int(body["min_buffer_cents"])
            except (TypeError, ValueError) as e:
                raise ValidationError("min_buffer_cents 必须为整数分") from e
            if buf < 0:
                raise ValidationError("min_buffer_cents 不可为负")
            merged["min_buffer_cents"] = buf
        if "horizon_days" in body:
            try:
                horizon = int(body["horizon_days"])
            except (TypeError, ValueError) as e:
                raise ValidationError("horizon_days 必须为整数") from e
            if horizon < 1 or horizon > 3650:
                raise ValidationError("horizon_days 超出 1..3650")
            merged["horizon_days"] = horizon
        return merged

    # ============ 投资下单（进入闸门） ============
    def propose_order(self, body: dict[str, Any]) -> dict[str, Any]:
        product_id = body.get("product_id")
        if not product_id:
            raise ValidationError("缺少 product_id")
        with self._lock:
            state = self._snapshot()
            product = state.products.get(product_id)
            if product is None:
                raise NotFoundError(f"产品不存在: {product_id}")
            amount = self._cents(body)
            as_of = self._as_of(body.get("as_of"))
            assumptions = self._assumptions(state)
            version = (state.current_assumptions() or {}).get("version", 0)
            if body.get("settlement_date"):
                settlement = self._date(body["settlement_date"], "settlement_date")
            else:
                settlement = as_of
            if settlement < as_of:
                raise ValidationError("结算日不得早于 as_of")
            maturity = settlement + timedelta(days=product["maturity_days"])
            proceeds = projected_proceeds(amount, product["annual_rate"], product["maturity_days"])
            oid = body.get("order_id") or self._store.next_identity("ord")
            payload = {
                "order_id": oid,
                "product_id": product_id,
                "amount_cents": amount,
                "proposed_at": iso(self._clock.now()),
                "as_of": as_of.isoformat(),
                "settlement_date": settlement.isoformat(),
                "maturity_date": maturity.isoformat(),
                "expected_proceeds_cents": proceeds,
                "assumptions_version": version,
            }
            event, dup = self._append("order.proposed", payload, body.get("idempotency_key"))
            if dup:
                payload = event["payload"]
                oid = payload["order_id"]
            self._notify()
            state = self._snapshot()
            view = self._order_view(state.orders[oid], as_of)
            view["duplicate"] = dup
            return view

    # ============ 后台：审批与告警 ============
    def _notify(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def _process_pending_orders(self) -> bool:
        state = self._snapshot()
        changed = False
        for o in list(state.orders.values()):
            if o["decision_state"] != "proposed":
                continue
            assumptions = self._assumptions_version(state, o.get("assumptions_version", 0))
            as_of = date.fromisoformat(o["as_of"])
            extra = ExtraOrder(
                amount_cents=o["amount_cents"],
                settlement_date=o["settlement_date"],
                maturity_date=o["maturity_date"],
                proceeds_cents=o["expected_proceeds_cents"],
            )
            result = evaluate_gate(state, as_of, assumptions, extra)
            decision = {
                "decided_at": iso(self._clock.now()),
                "as_of": as_of.isoformat(),
                "assumptions_version": o.get("assumptions_version", 0),
                "gate": self._jsonable_gate(result),
            }
            if result["approved"]:
                self._append(
                    "order.approved",
                    {"order_id": o["id"], "decision": decision},
                )
            else:
                self._append(
                    "order.blocked",
                    {"order_id": o["id"], "decision": decision},
                )
                alert_id = self._store.next_identity("alt")
                self._append(
                    "alert.raised",
                    {
                        "alert_id": alert_id,
                        "kind": "gate_blocked",
                        "scenario": result["triggered_scenario"],
                        "reason": (
                            f"订单 {o['id']} 在 {result['triggered_scenario']} 情景下击穿资金底线，"
                            f"首日缺口 {result['first_gap_date']}"
                        ),
                        "detail": {
                            "order_id": o["id"],
                            "triggered_scenario": result["triggered_scenario"],
                            "failing_scenarios": result["failing_scenarios"],
                            "first_gap_date": result["first_gap_date"],
                            "shortfall_cents": result["shortfall_cents"],
                            "investable_cap_cents": result["investable_cap_cents"],
                        },
                    },
                )
            self._append(
                "risk.evaluated",
                {
                    "kind": "gate",
                    "ref_id": o["id"],
                    "as_of": as_of.isoformat(),
                    "assumptions_version": o.get("assumptions_version", 0),
                    "approved": result["approved"],
                    "result": decision["gate"],
                },
            )
            changed = True
            state = self._snapshot()
        return changed

    def _reconcile_block_alerts(self, state: State) -> bool:
        """补偿：为每个已阻断但缺 gate_blocked 告警的订单补告警（崩溃恢复用）。"""
        covered = {
            a.get("detail", {}).get("order_id")
            for a in state.alerts.values()
            if a["kind"] == "gate_blocked"
        }
        changed = False
        for o in state.orders.values():
            if o["decision_state"] != "blocked" or o["id"] in covered:
                continue
            gate = (o.get("decision") or {}).get("gate", {})
            self._append(
                "alert.raised",
                {
                    "alert_id": self._store.next_identity("alt"),
                    "kind": "gate_blocked",
                    "scenario": gate.get("triggered_scenario"),
                    "reason": (
                        f"订单 {o['id']} 在 {gate.get('triggered_scenario')} 情景下击穿资金底线，"
                        f"首日缺口 {gate.get('first_gap_date')}"
                    ),
                    "detail": {
                        "order_id": o["id"],
                        "triggered_scenario": gate.get("triggered_scenario"),
                        "failing_scenarios": gate.get("failing_scenarios", []),
                        "first_gap_date": gate.get("first_gap_date"),
                        "shortfall_cents": gate.get("shortfall_cents", 0),
                        "investable_cap_cents": gate.get("investable_cap_cents", 0),
                    },
                },
            )
            changed = True
        return changed

    def _process_alerts(self) -> bool:
        state = self._snapshot()
        changed = self._reconcile_block_alerts(state)
        as_of = self._clock.today()
        assumptions = self._assumptions(state)
        risk = liquidity_risk(state, as_of, assumptions)
        open_alert = state.open_alert("liquidity_gap")
        changed = False
        if risk["triggered_scenario"] and open_alert is None:
            alert_id = self._store.next_identity("alt")
            self._append(
                "alert.raised",
                {
                    "alert_id": alert_id,
                    "kind": "liquidity_gap",
                    "scenario": risk["triggered_scenario"],
                    "reason": (
                        f"{risk['triggered_scenario']} 情景预计 {risk['first_gap_date']} "
                        f"出现现金缺口 {yuan(risk['shortfall_cents'])} 元"
                    ),
                    "detail": {
                        "triggered_scenario": risk["triggered_scenario"],
                        "first_gap_date": risk["first_gap_date"],
                        "shortfall_cents": risk["shortfall_cents"],
                    },
                },
            )
            changed = True
        elif not risk["triggered_scenario"] and open_alert is not None:
            self._append("alert.resolved", {"alert_id": open_alert["id"]})
            changed = True
        return changed

    def resolve_alert(self, alert_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._snapshot()
            alert = state.alerts.get(alert_id)
            if alert is None:
                raise NotFoundError(f"告警不存在: {alert_id}")
            if alert["status"] != "open":
                return self._alert_view(alert)
            self._append("alert.resolved", {"alert_id": alert_id})
            return self._alert_view(self._snapshot().alerts[alert_id])

    # ============ 只读视图 ============
    def position(self, as_of_raw: str | None = None) -> dict[str, Any]:
        as_of = self._as_of(as_of_raw)
        state = self._snapshot()
        assumptions = self._assumptions(state)
        balance, accounts = forecast.bank_balance_as_of(state, as_of)
        floor = state.reserve_cents + int(assumptions.get("min_buffer_cents", 0))
        horizon = int(assumptions["horizon_days"])
        scenarios_out: dict[str, Any] = {}
        for scn in SCENARIOS:
            rows = run_scenario(state, as_of, assumptions, scn, horizon)
            summary = series_summary(rows)
            scenarios_out[scn] = {
                **summary,
                "investable_cap_cents": summary["min_balance_cents"] - floor,
            }
        cap = min(v["investable_cap_cents"] for v in scenarios_out.values())
        receivables = 0
        for it in state.inflows.values():
            receivables += self._outstanding(it, as_of)
        obligations = 0
        for table in (state.contracts, state.payrolls, state.refunds):
            for it in table.values():
                obligations += self._outstanding(it, as_of)
        cur = state.current_assumptions()
        return {
            "as_of": as_of.isoformat(),
            "bank_balance_cents": balance,
            "bank_balance": yuan(balance),
            "accounts": accounts,
            "receivables_cents": receivables,
            "receivables": yuan(receivables),
            "obligations_cents": obligations,
            "obligations": yuan(obligations),
            "frozen": frozen_funds(state, as_of, assumptions),
            "investable_cap_cents": cap,
            "investable_cap": yuan(max(0, cap)),
            "binding_scenario": min(SCENARIOS, key=lambda k: scenarios_out[k]["investable_cap_cents"]),
            "assumptions_version": (cur or {}).get("version", 0),
            "scenarios": {
                scn: {
                    "min_balance_cents": v["min_balance_cents"],
                    "min_balance": yuan(v["min_balance_cents"]),
                    "min_balance_date": v["min_balance_date"],
                    "end_balance_cents": v["end_balance_cents"],
                    "end_balance": yuan(v["end_balance_cents"]),
                    "investable_cap_cents": v["investable_cap_cents"],
                    "investable_cap": yuan(max(0, v["investable_cap_cents"])),
                }
                for scn, v in scenarios_out.items()
            },
        }

    def daily_forecast(self, scenario: str, as_of_raw: str | None = None) -> dict[str, Any]:
        if scenario not in SCENARIOS:
            raise ValidationError(f"未知情景: {scenario}")
        as_of = self._as_of(as_of_raw)
        state = self._snapshot()
        assumptions = self._assumptions(state)
        rows = run_scenario(state, as_of, assumptions, scenario)
        floor = state.reserve_cents + int(assumptions.get("min_buffer_cents", 0))
        return {
            "as_of": as_of.isoformat(),
            "scenario": scenario,
            "floor_cents": floor,
            "rows": [
                {
                    **r,
                    "inflow": yuan(r["inflow_cents"]),
                    "outflow": yuan(r["outflow_cents"]),
                    "net": yuan(r["net_cents"]),
                    "balance": yuan(r["balance_cents"]),
                    "below_floor": r["balance_cents"] < floor,
                }
                for r in rows
            ],
        }

    def scenario_diff(self, as_of_raw: str | None = None) -> dict[str, Any]:
        as_of = self._as_of(as_of_raw)
        state = self._snapshot()
        assumptions = self._assumptions(state)
        horizon = int(assumptions["horizon_days"])
        out: dict[str, Any] = {}
        base_min = base_end = None
        for scn in SCENARIOS:
            rows = run_scenario(state, as_of, assumptions, scn, horizon)
            summary = series_summary(rows)
            if scn == "base":
                base_min, base_end = summary["min_balance_cents"], summary["end_balance_cents"]
            out[scn] = {
                **summary,
                "delta_min_vs_base_cents": 0 if base_min is None else summary["min_balance_cents"] - base_min,
                "delta_end_vs_base_cents": 0 if base_end is None else summary["end_balance_cents"] - base_end,
            }
        return {"as_of": as_of.isoformat(), "scenarios": out}

    def maturities(self, as_of_raw: str | None = None) -> dict[str, Any]:
        as_of = self._as_of(as_of_raw)
        state = self._snapshot()
        schedule: list[dict[str, Any]] = []
        for o in state.orders.values():
            if o["decision_state"] == "blocked":
                continue
            actual = sum(
                ln["amount_cents"] for ln in o["maturity_lines"] if ln["value_date"] <= as_of
            ) or None
            schedule.append(
                {
                    "order_id": o["id"],
                    "product_id": o["product_id"],
                    "product_name": state.products.get(o["product_id"], {}).get("name", o["product_id"]),
                    "amount_cents": o["amount_cents"],
                    "amount": yuan(o["amount_cents"]),
                    "settlement_date": o["settlement_date"].isoformat(),
                    "maturity_date": o["maturity_date"].isoformat(),
                    "days_to_maturity": (o["maturity_date"] - as_of).days,
                    "expected_proceeds_cents": o["expected_proceeds_cents"],
                    "expected_proceeds": yuan(o["expected_proceeds_cents"]),
                    "actual_proceeds_cents": actual or None,
                    "state": order_lifecycle(o, as_of),
                }
            )
        schedule.sort(key=lambda r: r["maturity_date"])
        return {"as_of": as_of.isoformat(), "schedule": schedule}

    def list_orders(self, as_of_raw: str | None = None) -> dict[str, Any]:
        as_of = self._as_of(as_of_raw)
        state = self._snapshot()
        return {
            "orders": [self._order_view(o, as_of) for o in sorted(state.orders.values(), key=lambda x: x["proposed_at"])]
        }

    def get_order(self, order_id: str, as_of_raw: str | None = None) -> dict[str, Any]:
        as_of = self._as_of(as_of_raw)
        state = self._snapshot()
        o = state.orders.get(order_id)
        if o is None:
            raise NotFoundError(f"订单不存在: {order_id}")
        return self._order_view(o, as_of)

    def list_alerts(self) -> dict[str, Any]:
        state = self._snapshot()
        return {"alerts": [self._alert_view(a) for a in sorted(state.alerts.values(), key=lambda x: x["raised_at"])]}

    def list_assumptions(self) -> dict[str, Any]:
        state = self._snapshot()
        return {
            "current_version": (state.current_assumptions() or {}).get("version", 0),
            "versions": list(reversed(state.assumptions)),
            "effective": self._assumptions(state),
        }

    def ledger(self, as_of_raw: str | None = None) -> dict[str, Any]:
        """应收/应付与流水匹配明细，回答"哪些钱已到、哪些还悬着"。"""
        as_of = self._as_of(as_of_raw)
        state = self._snapshot()
        return {
            "as_of": as_of.isoformat(),
            "inflows": [self._inflow_view(it, as_of) for it in state.inflows.values()],
            "contracts": [self._contract_view(it, as_of) for it in state.contracts.values()],
            "payrolls": [self._payroll_view(it, as_of) for it in state.payrolls.values()],
            "refunds": [self._refund_view(it, as_of) for it in state.refunds.values()],
            "unmatched_lines": [
                self._line_view(
                    {
                        "account_id": ln["account_id"],
                        "amount_cents": ln["amount_cents"],
                        "value_date": ln["value_date"].isoformat(),
                        "ref_type": ln["ref_type"],
                        "ref_id": ln["ref_id"],
                        "description": ln["description"],
                    },
                    ln["recorded_at"],
                    ln["key"],
                )
                for ln in state.lines
                if not ln["matched"]
            ],
        }

    # ============ 序列化 ============
    def _outstanding(self, item: dict[str, Any], as_of: date) -> int:
        realized = sum(
            ln["amount_cents"] for ln in item["matched_lines"] if ln["value_date"] <= as_of
        )
        return max(0, item["amount_cents"] - realized)

    def _item_view(self, item: dict[str, Any], as_of: date, date_field: str) -> dict[str, Any]:
        outstanding = self._outstanding(item, as_of)
        return {
            "id": item["id"],
            "name": item["name"],
            "amount_cents": item["amount_cents"],
            "amount": yuan(item["amount_cents"]),
            date_field: item[date_field].isoformat(),
            "realized_cents": item["amount_cents"] - outstanding,
            "outstanding_cents": outstanding,
            "outstanding": yuan(outstanding),
            "settled": outstanding <= 0,
            "matched_lines": [
                {
                    "amount_cents": ln["amount_cents"],
                    "amount": yuan(ln["amount_cents"]),
                    "value_date": ln["value_date"].isoformat(),
                }
                for ln in item["matched_lines"]
            ],
        }

    def _inflow_view(self, item: dict[str, Any], as_of: date, duplicate: bool = False) -> dict[str, Any]:
        view = self._item_view(item, as_of, "due_date")
        view["kind"] = item["kind"]
        view["party"] = item.get("name", "")
        view["duplicate"] = duplicate
        return view

    def _contract_view(self, item: dict[str, Any], as_of: date | None = None, duplicate: bool = False) -> dict[str, Any]:
        view = self._item_view(item, as_of or self._clock.today(), "pay_date")
        view["duplicate"] = duplicate
        return view

    def _payroll_view(self, item: dict[str, Any], as_of: date | None = None, duplicate: bool = False) -> dict[str, Any]:
        view = self._item_view(item, as_of or self._clock.today(), "date")
        view["duplicate"] = duplicate
        return view

    def _refund_view(self, item: dict[str, Any], as_of: date, duplicate: bool = False) -> dict[str, Any]:
        view = self._item_view(item, as_of, "date")
        view["member"] = item.get("name", "")
        view["duplicate"] = duplicate
        return view

    def _account_view(self, a: dict[str, Any], duplicate: bool = False) -> dict[str, Any]:
        return {**a, "duplicate": duplicate}

    def _line_view(self, p: dict[str, Any], recorded_at: str, key: str, duplicate: bool = False) -> dict[str, Any]:
        return {
            "idempotency_key": key,
            "account_id": p["account_id"],
            "amount_cents": p["amount_cents"],
            "amount": yuan(p["amount_cents"]),
            "value_date": p["value_date"],
            "ref_type": p.get("ref_type"),
            "ref_id": p.get("ref_id"),
            "description": p.get("description", ""),
            "recorded_at": recorded_at,
            "duplicate": duplicate,
        }

    def _jsonable_gate(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "approved": result["approved"],
            "floor_cents": result["floor_cents"],
            "investable_cap_cents": result["investable_cap_cents"],
            "investable_cap": yuan(max(0, result["investable_cap_cents"])),
            "binding_scenario": result["binding_scenario"],
            "triggered_scenario": result["triggered_scenario"],
            "failing_scenarios": result["failing_scenarios"],
            "first_gap_date": result["first_gap_date"],
            "shortfall_cents": result["shortfall_cents"],
            "shortfall": yuan(result["shortfall_cents"]),
            "horizon_days": result["horizon_days"],
            "scenarios": result["scenarios"],
        }

    def _order_view(self, o: dict[str, Any], as_of: date) -> dict[str, Any]:
        actual_proceeds = next(
            (ln["amount_cents"] for ln in o["maturity_lines"] if ln["value_date"] <= as_of),
            None,
        )
        return {
            "order_id": o["id"],
            "product_id": o["product_id"],
            "amount_cents": o["amount_cents"],
            "amount": yuan(o["amount_cents"]),
            "state": order_lifecycle(o, as_of),
            "proposed_at": o["proposed_at"],
            "as_of": o["as_of"],
            "settlement_date": o["settlement_date"].isoformat(),
            "maturity_date": o["maturity_date"].isoformat(),
            "expected_proceeds_cents": o["expected_proceeds_cents"],
            "expected_proceeds": yuan(o["expected_proceeds_cents"]),
            "actual_proceeds_cents": actual_proceeds,
            "actual_proceeds": yuan(actual_proceeds) if actual_proceeds is not None else None,
            "assumptions_version": o.get("assumptions_version", 0),
            "decision": o["decision"],
        }

    def _alert_view(self, a: dict[str, Any], duplicate: bool = False) -> dict[str, Any]:
        return {
            "alert_id": a["id"],
            "kind": a["kind"],
            "status": a["status"],
            "scenario": a.get("scenario"),
            "reason": a.get("reason", ""),
            "detail": a.get("detail", {}),
            "raised_at": a["raised_at"],
            "resolved_at": a["resolved_at"],
            "duplicate": duplicate,
        }
