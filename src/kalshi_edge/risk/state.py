"""Equity tracking and loss-stop evaluation (daily / weekly / peak-to-trough)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_edge.config import RiskLimits
from kalshi_edge.domain import ONE


def _day_key(ts: datetime) -> str:
    return ts.date().isoformat()


def _week_key(ts: datetime) -> str:
    iso = ts.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@dataclass(slots=True)
class RiskState:
    """Mutable risk ledger persisted between runs.

    ``current_equity`` must be marked *conservatively*: cash + locked order cash + open
    positions valued at what could be realised now (best bid, or zero if none).
    """

    day: str
    week: str
    start_of_day_equity: Decimal
    start_of_week_equity: Decimal
    peak_equity: Decimal
    current_equity: Decimal
    orders_today: int = 0
    daily_halt: bool = False
    weekly_halt: bool = False  # latched: human review required
    drawdown_kill: bool = False  # latched: kill switch engaged

    @classmethod
    def fresh(cls, equity: Decimal, now: datetime) -> RiskState:
        return cls(_day_key(now), _week_key(now), equity, equity, equity, equity)

    def roll(self, now: datetime) -> None:
        """Start a new day/week if the calendar moved (UTC). Latched halts persist."""
        day, week = _day_key(now), _week_key(now)
        if day != self.day:
            self.day = day
            self.start_of_day_equity = self.current_equity
            self.orders_today = 0
            self.daily_halt = False
        if week != self.week:
            self.week = week
            self.start_of_week_equity = self.current_equity

    def mark(self, equity: Decimal, now: datetime) -> None:
        self.roll(now)
        self.current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity

    def evaluate_stops(self, limits: RiskLimits) -> list[str]:
        """Trip stops exactly at their bounds (<=). Returns newly-tripped stop names."""
        tripped: list[str] = []
        daily_floor = self.start_of_day_equity * (ONE - limits.daily_stop_frac)
        if not self.daily_halt and self.current_equity <= daily_floor:
            self.daily_halt = True
            tripped.append("daily_stop")
        if (not self.weekly_halt
                and self.current_equity
                <= self.start_of_week_equity * (ONE - limits.weekly_stop_frac)):
            self.weekly_halt = True
            tripped.append("weekly_stop")
        if (not self.drawdown_kill
                and self.current_equity <= self.peak_equity * (ONE - limits.max_drawdown_frac)):
            self.drawdown_kill = True
            tripped.append("max_drawdown")
        return tripped

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RiskState:
        return cls(
            day=str(data["day"]), week=str(data["week"]),
            start_of_day_equity=Decimal(data["start_of_day_equity"]),
            start_of_week_equity=Decimal(data["start_of_week_equity"]),
            peak_equity=Decimal(data["peak_equity"]),
            current_equity=Decimal(data["current_equity"]),
            orders_today=int(data.get("orders_today", 0)),
            daily_halt=bool(data.get("daily_halt", False)),
            weekly_halt=bool(data.get("weekly_halt", False)),
            drawdown_kill=bool(data.get("drawdown_kill", False)),
        )
