"""HTTP 端到端测试：真实端口上的 JSON API。"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cashgate.common import Clock
from cashgate.events import EventStore
from cashgate.http import create_handler_class
from cashgate.service import CashGateService

T0 = datetime(2026, 9, 19, 10, 0, 0)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.clock = Clock(lambda: T0)
        self.store = EventStore(Path(self.tmp) / "events.log", self.clock)
        self.service = CashGateService(self.store, self.clock)
        self.server = ThreadingHTTPServer(("127.0.0.1", free_port()), create_handler_class(self.service))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def seed(self) -> None:
        self.req("POST", "/accounts", {"account_id": "bank-1", "name": "基本户"})
        self.req("POST", "/bank/statement-lines",
                 {"idempotency_key": "open", "account_id": "bank-1", "amount": 500000, "value_date": "2026-09-19"})
        self.req("POST", "/receivables",
                 {"flow_id": "due-1", "kind": "membership_due", "amount": 200000, "due_date": "2026-10-10"})
        self.req("POST", "/contracts",
                 {"contract_id": "ctr-1", "amount": 60000, "pay_date": "2026-10-01"})
        self.req("POST", "/payrolls", {"payroll_id": "pay-1", "amount": 120000, "date": "2026-09-30"})
        self.req("POST", "/refunds", {"refund_id": "rfd-1", "amount": 90000, "date": "2026-10-05"})
        self.req("POST", "/refund-reserve", {"amount": 50000})
        self.req("POST", "/products",
                 {"product_id": "prd-30", "name": "30天", "maturity_days": 30, "annual_rate": "3"})

    def test_health(self) -> None:
        status, body = self.req("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

    def test_position_and_gate_flow(self) -> None:
        self.seed()
        status, pos = self.req("GET", "/position")
        self.assertEqual(200, status)
        self.assertEqual("500000.00", pos["bank_balance"])
        self.assertEqual("90000.00", pos["investable_cap"])

        status, order = self.req("POST", "/investment/orders", {"product_id": "prd-30", "amount": 200000})
        self.assertEqual(200, status)
        oid = order["order_id"]
        # 显式触发处理（也可由后台 worker 完成）
        status, _ = self.req("POST", "/processing/run", {})
        self.assertEqual(200, status)
        status, decided = self.req("GET", f"/investment/orders/{oid}")
        self.assertEqual("blocked", decided["state"])
        self.assertIsNotNone(decided["decision"]["gate"]["triggered_scenario"])

        status, alerts = self.req("GET", "/alerts")
        self.assertTrue(any(a["kind"] == "gate_blocked" for a in alerts["alerts"]))

    def test_statement_idempotency_over_http(self) -> None:
        self.seed()
        payload = {"idempotency_key": "dup-1", "account_id": "bank-1", "amount": -60000,
                   "value_date": "2026-10-01", "ref_type": "contract", "ref_id": "ctr-1"}
        s1, b1 = self.req("POST", "/bank/statement-lines", payload)
        s2, b2 = self.req("POST", "/bank/statement-lines", payload)
        self.assertEqual(200, s1)
        self.assertFalse(b1["duplicate"])
        self.assertTrue(b2["duplicate"])
        _, pos = self.req("GET", "/position?as_of=2026-10-02")
        self.assertEqual(44000000, pos["bank_balance_cents"])

    def test_assumptions_versions_endpoint(self) -> None:
        self.seed()
        s, b = self.req("PUT", "/assumptions", {"inflow_delay_days": {"stress": 45}})
        self.assertEqual(200, s)
        self.assertEqual(1, b["version"])
        s, listed = self.req("GET", "/assumptions")
        self.assertEqual(1, listed["current_version"])
        self.assertEqual(45, listed["effective"]["inflow_delay_days"]["stress"])

    def test_forecast_and_maturities_views(self) -> None:
        self.seed()
        s, daily = self.req("GET", "/forecast/daily?scenario=stress")
        self.assertEqual(200, s)
        self.assertEqual(91, len(daily["rows"]))
        s, diff = self.req("GET", "/forecast/scenario-diff")
        self.assertIn("stress", diff["scenarios"])
        s, mat = self.req("GET", "/investment/maturities")
        self.assertEqual(200, s)
        self.assertEqual([], mat["schedule"])

    def test_validation_errors_are_400(self) -> None:
        s, body = self.req("POST", "/bank/statement-lines", {"account_id": "bank-1"})
        self.assertEqual(400, s)
        self.assertIn("message", body)
        s, _ = self.req("GET", "/forecast/daily?scenario=nope")
        self.assertEqual(400, s)
        s, _ = self.req("GET", "/nope")
        self.assertEqual(404, s)


class WorkerAutoTest(unittest.TestCase):
    def test_worker_processes_proposal_automatically(self) -> None:
        tmp = tempfile.mkdtemp()
        clock = Clock(lambda: T0)
        store = EventStore(Path(tmp) / "events.log", clock)
        service = CashGateService(store, clock)
        service.start_worker(interval=0.02)
        try:
            service.register_account({"account_id": "bank-1"})
            service.record_statement_line(
                {"idempotency_key": "open", "account_id": "bank-1", "amount": 500000, "value_date": "2026-09-19"})
            service.define_product({"product_id": "p", "maturity_days": 30, "annual_rate": "2"})
            order = service.propose_order({"product_id": "p", "amount": 10000})
            deadline = datetime.now().timestamp() + 3
            import time

            while datetime.now().timestamp() < deadline:
                view = service.get_order(order["order_id"])
                if view["state"] != "proposed":
                    break
                time.sleep(0.03)
            self.assertEqual("approved", service.get_order(order["order_id"])["state"])
        finally:
            service.stop_worker()


if __name__ == "__main__":
    unittest.main()
