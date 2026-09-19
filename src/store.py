from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def default_state() -> dict[str, Any]:
    return {
        "accounts": [],
        "bank_entries": [],
        "cash_events": [],
        "investments": [],
        "assumptions": [],
        "alerts": [],
        "idempotency": {},
        "seq": {},
    }


class Store:
    """JSON 文件持久化，进程内加锁 + 临时文件原子替换，保证重启可恢复。"""

    def __init__(self, runtime_dir: str | os.PathLike[str]) -> None:
        self.dir = Path(runtime_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_state()
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        state = default_state()
        state.update(data)
        return state

    def _atomic_save(self) -> None:
        fd, tmp_name = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def mutate(self, fn: Callable[[dict[str, Any]], T]) -> T:
        with self._lock:
            result = fn(self.state)
            self._atomic_save()
            return result

    def view(self, fn: Callable[[dict[str, Any]], T]) -> T:
        with self._lock:
            return fn(self.state)
