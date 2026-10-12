"""Reconcile local order/fill state against the venue before (and during) trading."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from kalshi_edge.domain import ZERO, AccountSnapshot, Outcome
from kalshi_edge.execution.engine import ExecutionEngine


@dataclass(slots=True)
class ReconciliationReport:
    ok: bool
    issues: list[str] = field(default_factory=list)
    account: AccountSnapshot | None = None
    new_fills: int = 0


def reconcile(engine: ExecutionEngine) -> ReconciliationReport:
    """Fetch authoritative venue state and compare with the local store.

    Fails closed: any exception or mismatch yields ``ok=False`` which blocks trading.
    """
    issues: list[str] = []
    try:
        new_fills = engine.sync_fills()
        engine.resolve_unresolved()
        engine.refresh_open_orders()
        account = engine.venue.get_account()
        venue_open = {o.client_order_id: o for o in engine.venue.list_open_orders()}
    except Exception as exc:  # network/API failure: never interpreted as permission to trade
        return ReconciliationReport(False, [f"venue_query_failed: {exc}"])

    store = engine.store
    if store.unresolved_orders():
        issues.append("unresolved_orders_remain")

    local_open = {o.client_order_id: o for o in store.open_orders()}
    for cid in venue_open:
        if store.get_order(cid) is None:
            issues.append(f"untracked_venue_order:{cid}")
    for cid, order in local_open.items():
        if cid not in venue_open:
            issues.append(f"local_open_not_on_venue:{cid}:{order.state.value}")

    # Positions implied by our fills (for unsettled tickers) must match the venue.
    settled = store.settled_tickers()
    implied: dict[tuple[str, Outcome], Decimal] = defaultdict(lambda: ZERO)
    for f in store.fills():
        if f.ticker not in settled:
            implied[(f.ticker, f.outcome)] += f.quantity
    reported = {(p.ticker, p.outcome): p.quantity for p in account.positions}
    for key in set(implied) | set(reported):
        if key[0] in settled:
            continue
        if implied.get(key, ZERO) != reported.get(key, ZERO):
            issues.append(f"position_mismatch:{key[0]}:{key[1].value}:"
                          f"local={implied.get(key, ZERO)}:venue={reported.get(key, ZERO)}")
    if account.cash < ZERO:
        issues.append("negative_cash")
    return ReconciliationReport(not issues, issues, account, len(new_fills))
