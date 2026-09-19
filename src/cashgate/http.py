"""HTTP 路由：把 JSON 请求映射到 CashGateService，返回 JSON 响应。

路由表显式声明每个端点读取 body / query / 路径参数，避免隐式分支。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from .common import ServiceError
from .service import CashGateService

SERVICE_NAME = "体育俱乐部现金头寸与投资闸门"


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


def create_handler_class(service: CashGateService) -> type[BaseHTTPRequestHandler]:
    # method, path, service 方法名, 参数来源: "body" / "query" / "rest" / None
    routes: list[tuple[str, str, str, str | None]] = [
        ("GET", "/health", "", "health"),
        ("POST", "/accounts", "register_account", "body"),
        ("POST", "/bank/statement-lines", "record_statement_line", "body"),
        ("POST", "/receivables", "schedule_inflow", "body"),
        ("POST", "/contracts", "sign_contract", "body"),
        ("POST", "/payrolls", "schedule_payroll", "body"),
        ("POST", "/refunds", "schedule_refund", "body"),
        ("POST", "/refund-reserve", "set_refund_reserve", "body"),
        ("POST", "/products", "define_product", "body"),
        ("PUT", "/assumptions", "update_assumptions", "body"),
        ("GET", "/assumptions", "list_assumptions", None),
        ("POST", "/investment/orders", "propose_order", "body"),
        ("POST", "/processing/run", "", "process"),
        ("GET", "/investment/orders", "list_orders", "query"),
        ("GET", "/investment/maturities", "maturities", "query"),
        ("GET", "/alerts", "list_alerts", None),
        ("GET", "/position", "position", "query"),
        ("GET", "/forecast/daily", "daily_forecast", "forecast"),
        ("GET", "/forecast/scenario-diff", "scenario_diff", "query"),
        ("GET", "/ledger", "ledger", "query"),
        ("GET", "/investment/orders/{id}", "get_order", "id"),
        ("POST", "/alerts/{id}/resolve", "resolve_alert", "id"),
    ]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict[str, Any]:
            from .common import ValidationError

            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise ValidationError("请求体不是合法 JSON") from e
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            try:
                for m, pattern, fn_name, source in routes:
                    if m != method:
                        continue
                    ident = self._match(pattern, path)
                    if ident is False:
                        continue
                    self._invoke(fn_name, source, query, ident if ident is not True else None)
                    return
                self._send(404, {"error": "not_found", "message": f"无此路由: {method} {path}"})
            except ServiceError as e:
                self._send(
                    e.status,
                    {"error": type(e).__name__.replace("Error", "").lower(), "message": e.message, **e.extra},
                )

        @staticmethod
        def _match(pattern: str, path: str) -> str | bool:
            if "{id}" not in pattern:
                return path == pattern
            prefix, suffix = pattern.split("{id}")
            if path.startswith(prefix) and path.endswith(suffix) and len(path) > len(prefix) + len(suffix):
                ident = path[len(prefix): len(path) - len(suffix)] if suffix else path[len(prefix):]
                return ident if "/" not in ident else False
            return False

        def _invoke(self, fn_name: str, source: str | None, query: dict[str, str], ident: str | None) -> None:
            if source == "health":
                self._send(200, health_payload())
                return
            if source == "process":
                changed = service.process_due()
                self._send(200, {"processed": changed})
                return
            fn = getattr(service, fn_name)
            if source == "body":
                payload = fn(self._read_body())
            elif source == "query":
                payload = fn(query.get("as_of"))
            elif source == "forecast":
                payload = fn(query.get("scenario", "base"), query.get("as_of"))
            elif source == "id":
                payload = fn(ident) if fn_name == "resolve_alert" else fn(ident, query.get("as_of"))
            else:
                payload = fn()
            self._send(200, payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler
