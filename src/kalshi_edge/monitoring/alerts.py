"""Operator alerts: always logged + persisted to the audit store; optional hook."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kalshi_edge.execution.store import Store

log = logging.getLogger("kalshi_edge.alerts")

ALERT_KINDS = frozenset({
    "order_submitted", "fill", "daily_stop", "weekly_stop", "max_drawdown", "stale_feed",
    "repeated_rejects", "unmatched_fill", "account_mismatch", "kill_switch", "reconcile_failed",
    "ambiguous_submit",
})


class AlertSink:
    def __init__(self, store: Store, hook: Callable[[str, dict[str, Any]], None] | None = None
                 ) -> None:
        self.store = store
        self.hook = hook
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def alert(self, now: datetime, kind: str, data: dict[str, Any]) -> None:
        level = logging.INFO if kind in ("order_submitted", "fill") else logging.WARNING
        log.log(level, "ALERT %s", kind, extra={"data": {"kind": kind, **data}})
        self.store.log_event(now, f"alert:{kind}", data)
        self.sent.append((kind, data))
        if self.hook is not None:
            try:
                self.hook(kind, data)
            except Exception:  # alert delivery must never break the trader
                log.exception("alert hook failed")
