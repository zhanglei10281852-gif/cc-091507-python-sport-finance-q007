"""服务层测试：资金闸门、幂等对账、假设版本、重启恢复。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cashgate.common import Clock
from cashgate.events import EventStore
from cashgate.service import CashGateService

T0 = datetime(2026, 9, 19, 10, 0, 0)


def make_service(tmp: str) -> CashGateService:
    clock = Clock(lambda: T0)
    return CashGateService(EventStore(Path(tmp) / "events.log", clock), clock)


def seed_world(svc: CashGateService) -> None:
    """银行 50 万；应收会费 20 万；赞助 8 万；合同 6 万；工资 12 万；退款 9 万；准备金 5 万。"""
    svc.register_account({"account_id": "bank-1", "name": "基本户"})
    svc.record_statement_line(
        {"idempotency_key": "open", "account_id": "bank-1", "amount": 500000, "value_date": "2026-09-19"}
    )
    svc.schedule_inflow({"flow_id": "due-1", "kind": "membership_due", "amount": 200000, "due_date": "2026-10-10"})
    svc.schedule_inflow({"flow_id": "spn-1", "kind": "sponsor_receipt", "amount": 80000, "due_date": "2026-11-01"})
    svc.sign_contract({"contract_id": "ctr-1", "name": "秋季联赛报名费", "amount": 60000, "pay_date": "2026-10-01"})
    svc.schedule_payroll({"payroll_id": "pay-1", "amount": 120000, "date": "2026-09-30"})
    svc.schedule_refund({"refund_id": "rfd-1", "amount": 90000, "date": "2026-10-05"})
    svc.set_refund_reserve({"amount": 50000})
    svc.define_product({"product_id": "prd-30", "name": "30天理财", "maturity_days": 30, "annual_rate": "3"})


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        seed_world(self.svc)

    def test_position_worst_case_cap_is_stress(self) -> None:
        pos = self.svc.position()
        self.assertEqual("stress", pos["binding_scenario"])
        # stress: 50万 - 工资12万 - 合同6万 - 退款18万 = 14万；底线 5万 → 上限 9万
        self.assertEqual(9000000, pos["investable_cap_cents"])
        self.assertEqual(14000000, pos["scenarios"]["stress"]["min_balance_cents"])

    def test_order_over_cap_blocked_with_scenario(self) -> None:
        o = self.svc.propose_order({"product_id": "prd-30", "amount": 200000})
        self.svc.process_due()
        o = self.svc.get_order(o["order_id"])
        self.assertEqual("blocked", o["state"])
        gate = o["decision"]["gate"]
        self.assertIn(gate["triggered_scenario"], ("adverse", "stress"))
        self.assertIsNotNone(gate["first_gap_date"])
        self.assertGreater(gate["shortfall_cents"], 0)
        alerts = self.svc.list_alerts()["alerts"]
        blocked = [a for a in alerts if a["kind"] == "gate_blocked"]
        self.assertEqual(1, len(blocked))
        self.assertEqual(o["order_id"], blocked[0]["detail"]["order_id"])

    def test_order_within_cap_approved(self) -> None:
        o = self.svc.propose_order({"product_id": "prd-30", "amount": 50000})
        self.svc.process_due()
        o = self.svc.get_order(o["order_id"])
        self.assertEqual("approved", o["state"])
        self.assertTrue(o["decision"]["gate"]["approved"])

    def test_blocked_order_does_not_consume_capacity(self) -> None:
        big = self.svc.propose_order({"product_id": "prd-30", "amount": 200000})
        self.svc.process_due()
        self.assertEqual("blocked", self.svc.get_order(big["order_id"])["state"])
        pos = self.svc.position()
        # 阻断单不占用资金：上限仍是 9 万
        self.assertEqual(9000000, pos["investable_cap_cents"])
        small = self.svc.propose_order({"product_id": "prd-30", "amount": 90000})
        self.svc.process_due()
        self.assertEqual("approved", self.svc.get_order(small["order_id"])["state"])


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        self.svc.register_account({"account_id": "bank-1", "name": "基本户"})

    def test_duplicate_statement_line_collapses(self) -> None:
        body = {
            "idempotency_key": "tx-1",
            "account_id": "bank-1",
            "amount": -60000,
            "value_date": "2026-10-01",
            "ref_type": "contract",
            "ref_id": "ctr-1",
        }
        first = self.svc.record_statement_line(body)
        second = self.svc.record_statement_line({**body, "description": "重复推送"})
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        pos = self.svc.position("2026-10-02")
        # 只扣一次
        self.assertEqual(-6000000, pos["bank_balance_cents"])

    def test_line_before_contract_then_contract_matches(self) -> None:
        # 银行流水（提前扣款）先到，合同后签：乱序到达仍能匹配
        self.svc.record_statement_line(
            {
                "idempotency_key": "fee-early",
                "account_id": "bank-1",
                "amount": -60000,
                "value_date": "2026-09-25",
                "ref_type": "contract",
                "ref_id": "ctr-1",
            }
        )
        self.svc.sign_contract(
            {"contract_id": "ctr-1", "amount": 60000, "pay_date": "2026-10-01"}
        )
        ledger = self.svc.ledger("2026-09-26")
        ctr = ledger["contracts"][0]
        self.assertTrue(ctr["settled"])
        self.assertEqual(0, ctr["outstanding_cents"])

    def test_sponsor_late_arrival_updates_forecast(self) -> None:
        seed_world(self.svc)
        # 赞助延期：先签预期 11-01，后银行通知实际 11-20 才到（未来价值日流水不重复计算）
        self.svc.record_statement_line(
            {
                "idempotency_key": "spn-late",
                "account_id": "bank-1",
                "amount": 80000,
                "value_date": "2026-11-20",
                "ref_type": "sponsor_receipt",
                "ref_id": "spn-1",
            }
        )
        ledger = self.svc.ledger("2026-09-19")
        spn = next(x for x in ledger["inflows"] if x["id"] == "spn-1")
        # 9-19 时价值日未到：仍是应收，但已被流水锁定（预测不再重复计入）
        self.assertEqual(8000000, spn["outstanding_cents"])
        daily = self.svc.daily_forecast("base", "2026-09-19")
        row = next(r for r in daily["rows"] if r["date"] == "2026-11-20")
        self.assertEqual(8000000, row["inflow_cents"])
        row_old = next(r for r in daily["rows"] if r["date"] == "2026-11-01")
        self.assertEqual(0, row_old["inflow_cents"])
        # 价值日过后视为已收
        settled = self.svc.ledger("2026-11-21")
        spn2 = next(x for x in settled["inflows"] if x["id"] == "spn-1")
        self.assertTrue(spn2["settled"])


class AssumptionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        seed_world(self.svc)

    def test_assumption_change_does_not_rewrite_approved_batch(self) -> None:
        order = self.svc.propose_order({"product_id": "prd-30", "amount": 50000})
        self.svc.process_due()
        order = self.svc.get_order(order["order_id"])
        self.assertEqual("approved", order["state"])
        snapshot = order["decision"]["gate"]
        self.assertEqual(0, order["assumptions_version"])  # 锁定默认版本

        # 管理员大幅收紧假设（stress 流入延后 60 天、折损 30%）→ 新版本
        self.svc.update_assumptions(
            {
                "inflow_delay_days": {"stress": 60},
                "inflow_haircut_pct": {"stress": 30},
                "note": "赞助风险上升",
            }
        )
        versions = self.svc.list_assumptions()
        self.assertEqual(1, versions["current_version"])

        # 历史批次的决策快照不被改写
        again = self.svc.get_order(order["order_id"])
        self.assertEqual(0, again["assumptions_version"])
        self.assertEqual(snapshot, again["decision"]["gate"])
        self.assertEqual("approved", again["state"])

        # 新订单按新假设评审；原来 9 万的单子现在会被阻断
        new_order = self.svc.propose_order({"product_id": "prd-30", "amount": 90000})
        self.svc.process_due()
        new_order = self.svc.get_order(new_order["order_id"])
        self.assertEqual("blocked", new_order["state"])
        self.assertEqual(1, new_order["assumptions_version"])

    def test_invalid_monotonic_assumptions_rejected(self) -> None:
        from cashgate.common import ValidationError

        with self.assertRaises(ValidationError):
            self.svc.update_assumptions({"inflow_delay_days": {"stress": 1, "adverse": 14}})


class RestartTest(unittest.TestCase):
    def test_pending_order_processed_after_restart(self) -> None:
        tmp = tempfile.mkdtemp()
        svc = make_service(tmp)
        seed_world(svc)
        order = svc.propose_order({"product_id": "prd-30", "amount": 200000})
        # 未处理即“崩溃”
        del svc

        svc2 = make_service(tmp)
        pending = svc2.list_orders()["orders"]
        self.assertEqual("proposed", pending[0]["state"])
        svc2.process_due()
        decided = svc2.get_order(order["order_id"])
        self.assertEqual("blocked", decided["state"])
        self.assertIsNotNone(decided["decision"]["gate"]["triggered_scenario"])

    def test_open_alert_survives_restart_and_auto_resolves(self) -> None:
        tmp = tempfile.mkdtemp()
        svc = make_service(tmp)
        seed_world(svc)
        # 批准一笔恰好顶到 stress 上限（9 万）的订单
        big = svc.propose_order({"product_id": "prd-30", "amount": 90000})
        svc.process_due()
        self.assertEqual("approved", svc.get_order(big["order_id"])["state"])
        self.assertFalse(any(a["kind"] == "liquidity_gap" and a["status"] == "open"
                             for a in svc.list_alerts()["alerts"]))
        # 批准后退款高峰继续扩大：新增 5 万退款（stress 翻倍为 10 万）→ 缺口出现
        svc.schedule_refund({"refund_id": "rfd-2", "amount": 50000, "date": "2026-10-06"})
        svc.process_due()
        gap_alerts = [a for a in svc.list_alerts()["alerts"]
                      if a["kind"] == "liquidity_gap" and a["status"] == "open"]
        self.assertTrue(gap_alerts)
        self.assertEqual("stress", gap_alerts[0]["scenario"])
        del svc

        svc2 = make_service(tmp)
        alerts = svc2.list_alerts()["alerts"]
        self.assertTrue(any(a["kind"] == "liquidity_gap" and a["status"] == "open" for a in alerts))
        # 资金到账使缺口消失后，下一轮自动解除
        svc2.record_statement_line(
            {"idempotency_key": "pay-cover", "account_id": "bank-1", "amount": 300000, "value_date": "2026-09-19"}
        )
        svc2.process_due()
        self.assertFalse(any(a["kind"] == "liquidity_gap" and a["status"] == "open"
                             for a in svc2.list_alerts()["alerts"]))


class ViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        seed_world(self.svc)

    def test_scenario_diff_and_daily(self) -> None:
        diff = self.svc.scenario_diff()
        self.assertLessEqual(
            diff["scenarios"]["stress"]["min_balance_cents"],
            diff["scenarios"]["base"]["min_balance_cents"],
        )
        daily = self.svc.daily_forecast("stress")
        self.assertEqual(91, len(daily["rows"]))
        # 提高最低缓冲后，stress 低点低于冻结底线
        self.svc.update_assumptions({"min_buffer_cents": 15000000})
        daily = self.svc.daily_forecast("stress")
        self.assertTrue(any(r["below_floor"] for r in daily["rows"]))
        self.assertGreater(daily["floor_cents"], 5000000)

    def test_frozen_funds_reasons(self) -> None:
        order = self.svc.propose_order({"product_id": "prd-30", "amount": 50000})
        self.svc.process_due()
        self.assertEqual("approved", self.svc.get_order(order["order_id"])["state"])
        frozen = self.svc.position("2026-09-19")["frozen"]
        reasons = {i["reason"] for i in frozen["items"]}
        self.assertIn("refund_reserve", reasons)
        self.assertIn("approved_pending_settlement", reasons)
        self.assertEqual(10000000, frozen["total_cents"])

    def test_maturities_schedule_and_settlement_lifecycle(self) -> None:
        order = self.svc.propose_order(
            {"product_id": "prd-30", "amount": 50000, "settlement_date": "2026-09-22"}
        )
        self.svc.process_due()
        oid = order["order_id"]
        mat = self.svc.maturities("2026-09-19")["schedule"][0]
        self.assertEqual("2026-10-22", mat["maturity_date"])
        self.assertEqual("approved", mat["state"])

        # 银行扣款流水乱序到达（晚于结算日才推送，价值日仍为 9-22）
        self.svc.record_statement_line(
            {"idempotency_key": "buy", "account_id": "bank-1", "amount": -50000,
             "value_date": "2026-09-22", "ref_type": "investment_order", "ref_id": oid}
        )
        self.assertEqual("settled", self.svc.get_order(oid, "2026-09-23")["state"])
        # 到期回款
        self.svc.record_statement_line(
            {"idempotency_key": "redeem", "account_id": "bank-1", "amount": 50123,
             "value_date": "2026-10-22", "ref_type": "investment_maturity", "ref_id": oid}
        )
        done = self.svc.get_order(oid, "2026-10-23")
        self.assertEqual("matured", done["state"])
        self.assertEqual(5012300, done["actual_proceeds_cents"])

    def test_expected_proceeds_calculation(self) -> None:
        order = self.svc.propose_order({"product_id": "prd-30", "amount": 100000})
        # 10,000,000 分 * 3% * 30/365 = 24657.53 → 24658 分利息
        self.assertEqual(10024658, order["expected_proceeds_cents"])


if __name__ == "__main__":
    unittest.main()
