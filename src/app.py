"""服务装配：事件日志(.runtime/events.log) + 服务 + HTTP。"""
from __future__ import annotations

import os
from http.server import ThreadingHTTPServer
from pathlib import Path

from cashgate.common import Clock
from cashgate.events import EventStore
from cashgate.http import create_handler_class, health_payload
from cashgate.service import CashGateService

RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", ".runtime"))
EVENT_LOG = RUNTIME_DIR / "events.log"

_service: CashGateService | None = None


def build_service(event_log: str | Path | None = None, autostart_worker: bool = True) -> CashGateService:
    global _service
    store = EventStore(event_log or EVENT_LOG, Clock())
    service = CashGateService(store, Clock())
    if autostart_worker:
        service.start_worker()
    _service = service
    return service


def get_service() -> CashGateService:
    if _service is None:
        return build_service()
    return _service


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    service = get_service()
    handler = create_handler_class(service)
    return ThreadingHTTPServer((host, port), handler)
