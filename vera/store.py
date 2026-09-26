"""Thread-safe, versioned in-memory context store."""

from __future__ import annotations

import threading
from typing import Any, Optional

SCOPES = ("category", "merchant", "customer", "trigger")


class ContextStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[tuple[str, str], dict[str, Any]] = {}

    def put(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[bool, int]:
        """Store a context. Returns (accepted, current_version)."""
        key = (scope, context_id)
        with self._lock:
            cur = self._data.get(key)
            if cur is not None and cur["version"] >= version:
                return False, cur["version"]
            self._data[key] = {"version": version, "payload": payload}
            return True, version

    def get(self, scope: str, context_id: Optional[str]) -> Optional[dict]:
        if not context_id:
            return None
        with self._lock:
            entry = self._data.get((scope, context_id))
            return entry["payload"] if entry else None

    def version(self, scope: str, context_id: Optional[str]) -> int:
        if not context_id:
            return 0
        with self._lock:
            entry = self._data.get((scope, context_id))
            return entry["version"] if entry else 0

    def all(self, scope: str) -> dict[str, dict]:
        with self._lock:
            return {cid: e["payload"] for (s, cid), e in self._data.items() if s == scope}

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in SCOPES}
        with self._lock:
            for (scope, _) in self._data:
                counts[scope] = counts.get(scope, 0) + 1
        return counts

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
