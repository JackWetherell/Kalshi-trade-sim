"""Interactive, human-only arming ceremony for live (production) trading.

Design (instruction.md §9):
* Requires a live config with ``allow_live_trading: true`` written by the operator.
* Requires an interactive TTY on stdin AND stdout; refuses under CI / agent environments.
* Displays bankroll, strategy, config hash, caps, max potential loss and the production
  hostname, then asks the operator to retype a fresh random challenge and the risk cap.
* The resulting ``ArmingToken`` lives only in process memory (never persisted), expires
  after at most 24h, and is bound to the process id — any restart disarms.

This module never reads credentials. Nothing in the repository calls it automatically.
"""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from kalshi_edge.config import AppConfig
from kalshi_edge.domain import Mode

MAX_ARMING_TTL = timedelta(hours=24)

#: Environment markers of non-human / automated contexts in which arming is refused.
FORBIDDEN_ENV_MARKERS = (
    "CI", "GITHUB_ACTIONS", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_REMOTE",
    "KALSHI_EDGE_NONINTERACTIVE", "PYTEST_CURRENT_TEST",
)


class ArmingRefused(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class ArmingToken:
    """Implements the ``exchange.hosts.LiveAuthorization`` protocol."""

    host: str
    config_hash: str
    issued_at: datetime
    expires_at: datetime
    max_total_risk: Decimal
    pid: int = field(default_factory=os.getpid)
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))

    def is_valid(self, now: datetime) -> bool:
        return (self.issued_at <= now < self.expires_at
                and self.expires_at - self.issued_at <= MAX_ARMING_TTL
                and self.pid == os.getpid())


def _automation_markers() -> list[str]:
    return [m for m in FORBIDDEN_ENV_MARKERS if os.environ.get(m)]


def interactive_arm(cfg: AppConfig, *, host: str, max_total_risk: Decimal, now: datetime,
                    ttl: timedelta = MAX_ARMING_TTL,
                    input_fn: Callable[[str], str] = input,
                    print_fn: Callable[[str], None] = print) -> ArmingToken:
    if cfg.mode is not Mode.LIVE:
        raise ArmingRefused("arming is only meaningful for mode=live")
    if cfg.allow_live_trading is not True:
        raise ArmingRefused("config does not set allow_live_trading: true (operator opt-in)")
    markers = _automation_markers()
    if markers:
        raise ArmingRefused(f"automated environment detected ({', '.join(markers)}); "
                            "live arming must be performed by a human in a terminal")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ArmingRefused("live arming requires an interactive TTY on stdin and stdout")
    ttl = min(ttl, MAX_ARMING_TTL)
    r = cfg.risk
    print_fn("=" * 72)
    print_fn("  LIVE TRADING ARMING — REAL MONEY")
    print_fn(f"  Production host       : {host}")
    print_fn(f"  Strategy              : {cfg.strategy.id.value}")
    print_fn(f"  Config hash           : {cfg.config_hash()}")
    print_fn(f"  Strategy capital      : ${cfg.strategy_capital}")
    print_fn(f"  Per-entry max loss    : {r.per_entry_max_loss_frac} of equity")
    print_fn(f"  Aggregate max loss    : {r.aggregate_open_loss_frac} of equity")
    print_fn(f"  Canary total risk cap : ${r.live_canary_total_risk}")
    print_fn(f"  MAX POTENTIAL LOSS    : ${max_total_risk}")
    print_fn(f"  Categories allowed    : {', '.join(r.category_allowlist) or '(none)'}")
    print_fn(f"  Arming expires        : {now + ttl:%Y-%m-%d %H:%M UTC} or on process exit")
    print_fn("=" * 72)
    challenge = secrets.token_hex(3).upper()
    expected = f"ARM {host} {challenge}"
    typed = input_fn(f"Type exactly '{expected}' to arm (anything else aborts): ").strip()
    if not secrets.compare_digest(typed, expected):
        raise ArmingRefused("challenge mismatch; not armed")
    typed_risk = input_fn("Retype the MAX POTENTIAL LOSS in dollars: ").strip().lstrip("$")
    try:
        if Decimal(typed_risk) != max_total_risk:
            raise ArmingRefused("risk confirmation mismatch; not armed")
    except ArithmeticError as exc:
        raise ArmingRefused("risk confirmation unreadable; not armed") from exc
    return ArmingToken(host=host, config_hash=cfg.config_hash(), issued_at=now,
                       expires_at=now + ttl, max_total_risk=max_total_risk)
