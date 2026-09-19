from __future__ import annotations

import json
import os
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import service
from domain import today
from store import Store

SERVICE_NAME = '体育俱乐部现金头寸与投资闸门'

# 这些路由支持 Idempotency-Key 幂等重放（乱序/重复到达安全）
IDEMPOTENT_ROUTES = {
    ("POST", "/bank/entries"),
    ("POST", "/events"),
    ("POST", "/investments"),
}

Handler = Callable[[dict[str, Any], dict[str, Any], dict[str, str]], dict[str, Any]]


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


class Application:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.recovery = self.store.mutate(
            lambda state: service.recover_pending(state, today())
        )

    def dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        payload: dict[str, Any],
        idem_key: str | None,
    ) -> tuple[int, bool, dict[str, Any]]:
        if method == "GET" and path == "/health":
            return 200, False, health_payload()

        handler = self._match(method, path, query)
        if handler is None:
            raise service.ApiError(404, "Not Found")

        def _tx(state: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
            # 幂等检查与写入在同一事务内，避免并发同键双写
            if idem_key:
                cached = service.idem_get(state, idem_key)
                if cached is not None:
                    return True, cached["result"]
            result = handler(state, payload, query)
            if idem_key and (method, path) in IDEMPOTENT_ROUTES:
                ref_id = str(
                    result.get("investment_id")
                    or result.get("event_id")
                    or result.get("entry_id")
                    or ""
                )
                kind = path.strip("/").replace("/", ":")
                service.idem_put(state, idem_key, kind, ref_id, result)
            return False, result

        replayed, result = self.store.mutate(_tx)
        return 200, replayed, result

    def _match(self, method: str, path: str, query: dict[str, str]) -> Handler | None:
        as_of = self._as_of(query)
        horizon = int(query["horizon"]) if query.get("horizon") else None

        static_routes: list[tuple[str, str, Handler]] = [
            ("POST", "^/admin/accounts$", lambda s, p, q: service.create_account(s, p)),
            ("POST", "^/bank/entries$", lambda s, p, q: service.add_bank_entry(s, p)),
            ("POST", "^/events$", lambda s, p, q: service.add_event(s, p)),
            ("GET", "^/position$", lambda s, p, q: service.position_view(s, as_of, horizon)),
            ("GET", "^/forecast$", lambda s, p, q: service.build_forecast_safe(s, as_of, horizon)),
            ("GET", "^/frozen$", lambda s, p, q: service.frozen_view(s, as_of, horizon)),
            ("GET", "^/maturities$", lambda s, p, q: service.maturity_schedule(s)),
            ("GET", "^/assumptions$", lambda s, p, q: service.assumptions_view(s)),
            ("POST", "^/assumptions$", lambda s, p, q: service.update_assumptions(s, p)),
            ("POST", "^/investments$", lambda s, p, q: service.propose_investment(s, p)),
            ("GET", "^/investments$", lambda s, p, q: {"investments": s["investments"]}),
            ("GET", "^/alerts$", lambda s, p, q: {"alerts": s["alerts"]}),
            ("POST", "^/admin/recover$", lambda s, p, q: service.recover_pending(s, as_of)),
        ]
        for m, pattern, fn in static_routes:
            if m == method and re.fullmatch(pattern, path):
                return fn

        dynamic: list[tuple[str, re.Pattern[str], Callable[..., Handler]]] = [
            ("POST", re.compile(r"^/investments/([^/]+)/evaluate$"),
             lambda ref: (lambda s, p, q: service.evaluate_investment(s, ref, p))),
            ("POST", re.compile(r"^/investments/([^/]+)/settle$"),
             lambda ref: (lambda s, p, q: service.settle_investment(s, ref, p))),
            ("POST", re.compile(r"^/investments/([^/]+)/maturity$"),
             lambda ref: (lambda s, p, q: service.receive_maturity(s, ref, p))),
            ("GET", re.compile(r"^/investments/([^/]+)$"),
             lambda ref: (lambda s, p, q: service.get_investment(s, ref))),
            ("POST", re.compile(r"^/alerts/([^/]+)/ack$"),
             lambda ref: (lambda s, p, q: service.acknowledge_alert(s, ref, p))),
        ]
        for m, pattern, make in dynamic:
            match = pattern.fullmatch(path)
            if m == method and match:
                return make(match.group(1))
        return None

    @staticmethod
    def _as_of(query: dict[str, str]) -> date:
        raw = query.get("as_of")
        try:
            return date.fromisoformat(raw) if raw else today()
        except ValueError as exc:
            raise service.ApiError(400, "as_of 必须是 YYYY-MM-DD") from exc


class RequestHandler(BaseHTTPRequestHandler):
    def _read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise service.ApiError(400, "请求体必须是合法 JSON") from exc
        if not isinstance(data, dict):
            raise service.ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            payload = self._read_payload() if method == "POST" else {}
            idem_key = self.headers.get("Idempotency-Key") or payload.pop(
                "idempotency_key", None
            )
            status, replayed, body = self.server.app.dispatch(  # type: ignore[attr-defined]
                method, parsed.path, query, payload, idem_key
            )
            if replayed:
                body = {"replayed": True, **body}
        except service.ApiError as err:
            self._send(err.status, {"error": err.message})
            return
        except ValueError as err:
            self._send(400, {"error": str(err)})
            return
        self._send(status, body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, runtime_dir: str | None = None) -> ThreadingHTTPServer:
    runtime_dir = runtime_dir or os.getenv("RUNTIME_DIR", ".runtime")
    store = Store(runtime_dir)
    app = Application(store)
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.app = app  # type: ignore[attr-defined]
    return server
