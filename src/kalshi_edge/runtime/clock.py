"""Clock implementations: wall clock for paper/demo/live, settable clock for replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ReplayClock:
    """Event-time clock for backtests; only moves forward."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ReplayClock requires a timezone-aware datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        if ts < self._now:
            raise ValueError(f"replay clock cannot move backwards ({ts} < {self._now})")
        self._now = ts

    def advance(self, delta: timedelta) -> None:
        self.set(self._now + delta)
