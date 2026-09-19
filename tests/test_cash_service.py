from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import Application
from store import Store

D0 = date(2026, 9, 19)


def iso(d: date) -> str:
    return d.isoformat()


class CashServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(Store(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None, idem: str | None = None):
        status, replayed, body = self.app.dispatch(
            method, path, {}, payload or {}, idem
        )
        self.assertEqual(200, status, body)
        return replayed, body

    def seed_club(self, opening: float = 100000.0) -> None:
        """典型俱乐部账本：报名费提前、赞助延期、退款高峰的压力情景。"""
        self.call("POST", "/admin/accounts", {"account_id": "BOC-CNY", "currency": "CNY"})
        self.call("POST", "/bank/entries", {
            "account_id": "BOC-CNY", "amount": opening,
            "value_date": iso(D0), "memo": "期初余额",
        })
        self.call("POST", "/events", {  # 工资税费 刚性流出
            "type": "payroll", "amount": 20000, "currency": "CNY",
            "scheduled_date": iso(D0 + timedelta(days=10)), "contract_ref": "HR-09"})
        self.call("POST", "/events", {  # 已签赛事合同报名费
            "type": "tournament_fee", "amount": 15000, "currency": "CNY",
            "scheduled_date": iso(D0 + timedelta(days=12)), "contract_ref": "LEAGUE-AUTUMN"})
        self.call("POST", "/events", {  # 家长退款（基准日）
            "type": "refund", "amount": 10000, "currency": "CNY",
            "scheduled_date": iso(D0 + timedelta(days=15))})
        self.call("POST", "/events", {  # 赞助款（可能延期）
            "type": "sponsor_receipt", "amount": 30000, "currency": "CNY",
            "scheduled_date": iso(D0 + timedelta(days=5)), "contract_ref": "SP-01"})
        self.call("POST", "/events", {  # 应收会费
            "type": "membership_due", "amount": 40000, "currency": "CNY",
            "scheduled_date": iso(D0 + timedelta(days=20))})

    # 1. 汇总 + 按日滚动预测 + 情景差异

    def test_rolling_forecast_and_worst_case_cap(self) -> None:
        self.seed_club()
        _, _, pos = self.app.dispatch("GET", "/position", {"as_of": iso(D0)}, {}, None)
        self.assertEqual({"CNY": 100000.0}, pos["bank_balances"])
        # 基准情景最低点 85000（第15天退款后），最坏情景 65000（第10天工资时）
        fc = pos["forecast"]
        self.assertEqual(85000.0, fc["scenarios"]["base"]["min_balance"]["CNY"])
        self.assertEqual(65000.0, fc["scenarios"]["worst"]["min_balance"]["CNY"])
        self.assertEqual({"CNY": 65000.0}, pos["investable_cap"])
        # 情景差异为负：最坏比基准更差
        variances = [v for v in fc["scenario_variance"] if v["currency"] == "CNY"]
        self.assertTrue(any(v["delta"] < 0 for v in variances))
        # 按日滚动：第 31 行（0..30 天），含余额序列
        rows = [r for r in fc["scenarios"]["worst"]["daily"] if r["currency"] == "CNY"]
        self.assertEqual(31, len(rows))
        self.assertEqual(100000.0, rows[0]["balance"])

    # 2. 资金闸门：安全单通过，缺口扩大阻止并记录触发情景

    def test_gate_approves_safe_order(self) -> None:
        self.seed_club()
        _, body = self.call("POST", "/investments", {
            "product": "7天通知存款", "amount": 60000, "currency": "CNY",
            "order_date": iso(D0), "settle_date": iso(D0 + timedelta(days=2)),
            "maturity_date": iso(D0 + timedelta(days=25)), "expected_proceeds": 500,
            "auto_evaluate": True,
        })
        self.assertEqual("approved", body["state"])
        self.assertEqual(0, body["assumption_version_at_decision"])
        self.assertEqual(65000.0, body["gate"]["investable_cap_at_decision"])

    def test_gate_blocks_order_when_worst_case_gap_widens(self) -> None:
        self.seed_club()
        _, body = self.call("POST", "/investments", {
            "product": "30天理财", "amount": 70000, "currency": "CNY",
            "order_date": iso(D0), "settle_date": iso(D0 + timedelta(days=2)),
            "maturity_date": iso(D0 + timedelta(days=28)), "expected_proceeds": 800,
            "auto_evaluate": True,
        })
        self.assertEqual("blocked", body["state"])
        violations = body["gate"]["violations"]
        self.assertTrue(violations)
        self.assertTrue(all(v["scenario"] == "worst" for v in violations))
        self.assertTrue(any(v.get("reason") == "amount_exceeds_investable_cap" for v in violations))
        # 风险告警持久化，记录触发情景
        _, alerts = self.call("GET", "/alerts")
        open_alerts = [a for a in alerts["alerts"] if a["status"] == "open"]
        self.assertEqual(1, len(open_alerts))
        self.assertEqual("worst", open_alerts[0]["trigger_scenario"])
        self.assertEqual(body["investment_id"], open_alerts[0]["investment_id"])

    # 3. 假设调整只升版本，不改写已批准批次

    def test_assumption_change_does_not_rewrite_approved_batch(self) -> None:
        self.seed_club()
        _, inv = self.call("POST", "/investments", {
            "product": "7天通知存款", "amount": 60000, "currency": "CNY",
            "order_date": iso(D0), "settle_date": iso(D0 + timedelta(days=2)),
            "maturity_date": iso(D0 + timedelta(days=25)), "expected_proceeds": 500,
            "auto_evaluate": True,
        })
        self.assertEqual("approved", inv["state"])
        snapshot_version = inv["assumption_version_at_decision"]
        # 管理员大幅收紧假设：退款翻倍、赞助全额延迟
        self.call("POST", "/assumptions", {
            "params": {"refund_surge_factor": 3.0, "sponsor_delay_days": 20,
                       "sponsor_collect_rate": 0.2},
            "changed_by": "treasurer", "reason": "联赛风险预案",
        })
        _, inv2 = self.call("GET", f"/investments/{inv['investment_id']}")
        self.assertEqual("approved", inv2["state"])  # 不被回溯改判
        self.assertEqual(snapshot_version, inv2["assumption_version_at_decision"])
        self.assertEqual(snapshot_version, inv2["gate"]["assumption_version"])
        # 新假设下可投资上限下降
        _, _, pos = self.app.dispatch("GET", "/position", {"as_of": iso(D0)}, {}, None)
        self.assertLess(pos["investable_cap"]["CNY"], 65000.0)
        self.assertEqual(1, pos["assumption_version"])
        # 重复 evaluate 已批准批次：保持原判
        _, reeval = self.call("POST", f"/investments/{inv['investment_id']}/evaluate", {})
        self.assertEqual("approved", reeval["investment"]["state"])

    # 4. 幂等：银行对账乱序、回款乱序/重复

    def test_bank_entries_idempotent_out_of_order(self) -> None:
        self.seed_club()
        payload = {"account_id": "BOC-CNY", "amount": 1234.0,
                   "value_date": iso(D0 + timedelta(days=3)), "ref": "STMT-42"}
        replay1, first = self.call("POST", "/bank/entries", payload, idem="bank-42")
        replay2, second = self.call("POST", "/bank/entries", payload, idem="bank-42")
        self.assertFalse(replay1)
        self.assertTrue(replay2)
        self.assertEqual(first["entry_id"], second["entry_id"])
        _, _, pos = self.app.dispatch("GET", "/position", {"as_of": iso(D0)}, {}, None)
        self.assertEqual(101234.0, pos["bank_balances"]["CNY"])  # 只入账一次

    def test_maturity_arrives_out_of_order_and_is_idempotent(self) -> None:
        self.seed_club()
        _, inv = self.call("POST", "/investments", {
            "product": "7天通知存款", "amount": 60000, "currency": "CNY",
            "order_date": iso(D0), "settle_date": iso(D0 + timedelta(days=2)),
            "maturity_date": iso(D0 + timedelta(days=25)), "expected_proceeds": 500,
            "auto_evaluate": True,
        })
        inv_id = inv["investment_id"]
        # 回款先于结算指令乱序到达：允许
        _, mat1 = self.call("POST", f"/investments/{inv_id}/maturity", {
            "account_id": "BOC-CNY", "proceeds": 60500,
            "value_date": iso(D0 + timedelta(days=25))})
        self.assertNotIn("replayed", mat1)
        self.assertEqual("matured", mat1["investment"]["state"])
        # 重复回款：幂等，不产生第二笔入账
        _, mat2 = self.call("POST", f"/investments/{inv_id}/maturity", {
            "account_id": "BOC-CNY", "proceeds": 99999,
            "value_date": iso(D0 + timedelta(days=26))})
        self.assertTrue(mat2["replayed"])
        _, _, pos = self.app.dispatch("GET", "/position", {"as_of": iso(D0 + timedelta(days=26))}, {}, None)
        self.assertEqual(100000.0 + 60500.0, pos["bank_balances"]["CNY"])

    # 5. 重启恢复：未完成审批继续处理，告警仍在

    def test_restart_resumes_pending_approvals_and_alerts(self) -> None:
        self.seed_club()
        # 不自动过闸的申请
        _, inv_ok = self.call("POST", "/investments", {
            "product": "稳健周开", "amount": 60000, "currency": "CNY",
            "order_date": iso(D0), "settle_date": iso(D0 + timedelta(days=2)),
            "maturity_date": iso(D0 + timedelta(days=25)), "expected_proceeds": 500,
        })
        _, inv_bad = self.call("POST", "/investments", {
            "product": "激进月开", "amount": 70000, "currency": "CNY",
            "order_date": iso(D0), "settle_date": iso(D0 + timedelta(days=2)),
            "maturity_date": iso(D0 + timedelta(days=28)),
        })
        self.assertEqual("proposed", inv_ok["state"])
        self.assertEqual("proposed", inv_bad["state"])
        # 模拟重启：新 Application 从同一目录加载并执行恢复
        restarted = Application(Store(self.tmp.name))
        self.assertIn(inv_ok["investment_id"], restarted.recovery["reevaluated"])
        self.assertIn(inv_bad["investment_id"], restarted.recovery["reevaluated"])
        self.assertEqual(1, len(restarted.recovery["open_alerts"]))
        _, _, ok = restarted.dispatch("GET", f"/investments/{inv_ok['investment_id']}", {}, {}, None)
        _, _, bad = restarted.dispatch("GET", f"/investments/{inv_bad['investment_id']}", {}, {}, None)
        self.assertEqual("approved", ok["state"])
        self.assertEqual("blocked", bad["state"])
        # 再次重启不会重复判定/重复告警
        again = Application(Store(self.tmp.name))
        self.assertEqual([], again.recovery["reevaluated"])
        _, _, alerts = again.dispatch("GET", "/alerts", {}, {}, None)
        self.assertEqual(1, len([a for a in alerts["alerts"] if a["status"] == "open"]))

    # 6. 冻结资金原因 + 到期回款安排

    def test_frozen_funds_reasons_and_maturity_schedule(self) -> None:
        self.seed_club()
        self.call("POST", "/investments", {
            "product": "7天通知存款", "amount": 60000, "currency": "CNY",
            "order_date": iso(D0), "settle_date": iso(D0 + timedelta(days=2)),
            "maturity_date": iso(D0 + timedelta(days=25)), "expected_proceeds": 500,
            "auto_evaluate": True,
        })
        _, _, pos = self.app.dispatch("GET", "/position", {"as_of": iso(D0)}, {}, None)
        reasons = " ".join(f["reason"] for f in pos["frozen_funds"])
        self.assertIn("工资税费", reasons)
        self.assertIn("退款准备金", reasons)
        self.assertIn("赛事报名费", reasons)
        self.assertIn("在投/待结算批次", reasons)
        _, mat = self.call("GET", "/maturities")
        self.assertEqual(1, len(mat["schedule"]))
        self.assertEqual(60500.0, mat["schedule"][0]["expected_proceeds"])
        self.assertEqual(iso(D0 + timedelta(days=25)), mat["schedule"][0]["maturity_date"])

    def test_concurrent_same_idempotency_key_single_write(self) -> None:
        self.call("POST", "/admin/accounts", {"account_id": "BOC-CNY", "currency": "CNY"})
        results: list[tuple] = []

        def worker() -> None:
            results.append(self.app.dispatch(
                "POST", "/bank/entries", {},
                {"account_id": "BOC-CNY", "amount": 500.0,
                 "value_date": iso(D0), "ref": "RACE"},
                "race-key",
            ))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entry_ids = {r[2]["entry_id"] for r in results}
        self.assertEqual(1, len(entry_ids))  # 8 个并发只入账一笔
        self.assertEqual(7, sum(1 for r in results if r[1]))  # 其余 7 个命中重放

    # 7. 结算下单真实扣减银行余额

    def test_settlement_debits_bank_account(self) -> None:
        self.seed_club()
        _, inv = self.call("POST", "/investments", {
            "product": "7天通知存款", "amount": 60000, "currency": "CNY",
            "order_date": iso(D0), "settle_date": iso(D0 + timedelta(days=2)),
            "maturity_date": iso(D0 + timedelta(days=25)),
            "auto_evaluate": True,
        })
        _, settled = self.call("POST", f"/investments/{inv['investment_id']}/settle", {
            "account_id": "BOC-CNY"})
        self.assertEqual("settled", settled["investment"]["state"])
        _, _, pos = self.app.dispatch("GET", "/position", {"as_of": iso(D0 + timedelta(days=2))}, {}, None)
        self.assertEqual(40000.0, pos["bank_balances"]["CNY"])


if __name__ == "__main__":
    unittest.main()
