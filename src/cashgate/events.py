"""事件存储：仅追加的 JSONL 日志。

每个事件同时带：
- seq / recorded_at：接收顺序与接收时间（服务时钟）
- payload 中的业务时间（value_date / due_date ...）：由调用方提供
- idempotency key：同一 (事件类型, key) 重复提交返回首次事件

写入逐行 flush + fsync；重启时整文件重放。末尾半行（崩溃截断）忽略。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

from .common import Clock, iso


class EventStore:
    def __init__(self, path: str | Path, clock: Clock) -> None:
        self._path = Path(path)
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._index: dict[tuple[str, str], int] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = self._path.read_bytes()
        lines = raw.splitlines(keepends=True)
        valid_bytes = 0
        for i, line_bytes in enumerate(lines):
            text = line_bytes.decode("utf-8").strip()
            if not text:
                valid_bytes += len(line_bytes)
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                if i < len(lines) - 1:
                    raise
                # 末尾半行（崩溃截断）：截掉它，使后续追加落在干净的行边界上
                with self._path.open("r+b") as f:
                    f.truncate(valid_bytes)
                break
            self._index_event(event)
            valid_bytes += len(line_bytes)
        # 确保文件以换行结尾，防止下一条事件拼到最后一行
        if valid_bytes and raw[valid_bytes - 1: valid_bytes] != b"\n":
            with self._path.open("r+b") as f:
                f.seek(valid_bytes)
                f.write(b"\n")
                f.flush()
                os.fsync(f.fileno())

    def _index_event(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        key = event.get("key")
        if key:
            self._index.setdefault((event["type"], key), event["seq"])

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """追加事件；key 命中时返回首次事件与 duplicated=True。"""
        with self._lock:
            if key is not None:
                seq = self._index.get((event_type, key))
                if seq is not None:
                    return self._events[seq - 1], True
            event = {
                "id": uuid.uuid4().hex,
                "seq": len(self._events) + 1,
                "type": event_type,
                "recorded_at": iso(self._clock.now()),
                "key": key,
                "payload": payload,
            }
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._index_event(event)
            return event, False

    def replay(self) -> Iterable[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def next_identity(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:10]}"
