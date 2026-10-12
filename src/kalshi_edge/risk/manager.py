"""Hard pre-trade risk manager. Ordinary deterministic code; fails closed.

Every proposal passes through ``HardRiskManager.check`` twice in the runtime: once at
discovery and again immediately before submission with a freshly fetched book and
account snapshot. All rejection reasons are returned so they can be logged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from kalshi_edge.config import RiskLimits
from kalshi_edge.domain import (
    ONE,
    ZERO,
    AccountSnapshot,
    MarketStatus,
    Mode,
    OrderBook,
    TradeProposal,
)
from kalshi_edge.fees import FeeSchedule
from kalshi_edge.pricing import ask_depth_notional, floor_to_step, is_on_grid, spread, walk_asks
from kalshi_edge.risk.exposure import Exposure, cluster_keys
from kalshi_edge.risk.state import RiskState


@dataclass(frozen=True, slots=True)
class HealthStatus:
    reconciled: bool
    feed_ok: bool
    unresolved_orders: int
    clock_skew_seconds: float = 0.0
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RiskContext:
    now: datetime
    mode: Mode
    book: OrderBook  # freshly fetched for the final check
    account: AccountSnapshot
    exposure: Exposure
    risk_state: RiskState
    health: HealthStatus
    kill_engaged: bool
    submission_permitted: bool  # mode/arming gate (live requires a valid arming token)
    strategy_capital: Decimal
    account_equity: Decimal  # conservative mark of the venue account
    pending_cash: Decimal = ZERO  # cash committed by intents not yet in ``account``
    live_max_total_risk: Decimal | None = None  # amount the operator confirmed at arming


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    quantity: Decimal
    max_loss: Decimal
    reasons: tuple[str, ...]
    metrics: Mapping[str, str] = field(default_factory=dict)


class HardRiskManager:
    def __init__(self, limits: RiskLimits, fees: FeeSchedule, *,
                 slippage_buffer: Decimal = ZERO) -> None:
        self.limits = limits
        self.fees = fees
        self.slippage_buffer = slippage_buffer

    # ------------------------------------------------------------------ helpers
    def max_loss_for(self, proposal: TradeProposal, book: OrderBook, qty: Decimal) -> Decimal:
        """Worst-case cash for ``qty``: qty*limit + rounded fees of the walked fills."""
        fills = walk_asks(book, proposal.outcome, qty)
        fee = self.fees.fee_for_fills(fills, multiplier=proposal.market.fee_multiplier)
        # If depth is short, fees on unfilled remainder are bounded by a full-price fee.
        filled = sum((f.quantity for f in fills), ZERO)
        if filled < qty:
            fee += self.fees.fee(qty - filled, proposal.limit_price,
                                 multiplier=proposal.market.fee_multiplier)
        return qty * proposal.limit_price + fee

    def equity_base(self, ctx: RiskContext) -> Decimal:
        """Tighter of designated strategy equity and the venue account's equity."""
        # Gains above designated capital are not re-levered without operator review.
        strategy_equity = min(ctx.strategy_capital, ctx.risk_state.current_equity)
        return max(ZERO, min(strategy_equity, ctx.account_equity))

    # ------------------------------------------------------------------- check
    def check(self, proposal: TradeProposal, ctx: RiskContext) -> RiskDecision:
        L = self.limits
        reasons: list[str] = []
        m = proposal.market
        book = ctx.book
        metrics: dict[str, str] = {}

        # 1. Kill switch and latched stops dominate everything.
        if ctx.kill_engaged:
            reasons.append("kill_switch_engaged")
        rs = ctx.risk_state
        if rs.drawdown_kill:
            reasons.append("max_drawdown_latched")
        if rs.weekly_halt:
            reasons.append("weekly_stop_latched_human_review_required")
        if rs.daily_halt:
            reasons.append("daily_stop_active")

        # 2. Mode / arming / strategy eligibility.
        if not ctx.submission_permitted:
            reasons.append("mode_does_not_permit_submission")
        if ctx.mode in (Mode.LIVE, Mode.DEMO) and not proposal.live_eligible:
            reasons.append("strategy_or_estimate_not_live_eligible")

        # 3. Operational health.
        h = ctx.health
        if not h.reconciled:
            reasons.append("not_reconciled")
        if not h.feed_ok:
            reasons.append("feed_unhealthy")
        if h.unresolved_orders > 0:
            reasons.append("unresolved_prior_orders")
        if abs(h.clock_skew_seconds) > L.max_clock_skew_seconds:
            reasons.append("clock_skew")
        if rs.orders_today >= L.max_orders_per_day:
            reasons.append("daily_order_count_limit")

        # 4. Market eligibility.
        if m.market_type != "binary":
            reasons.append("not_binary")
        if m.status is not MarketStatus.OPEN:
            reasons.append(f"market_status_{m.status.value}")
        if not m.rules_verified:
            reasons.append("market_rules_not_verified")
        if not L.category_allowlist or m.category not in L.category_allowlist:
            reasons.append("category_not_allowlisted")
        if m.result is not None:
            reasons.append("market_already_resolved")
        secs_to_close = (m.close_time - ctx.now).total_seconds()
        if secs_to_close < L.min_seconds_to_close:
            reasons.append("too_close_to_close")
        if book.ticker != m.ticker:
            reasons.append("book_market_mismatch")

        # 5. Price band and grid.
        p_limit = proposal.limit_price
        floor_applies = not (ctx.mode is Mode.BACKTEST and proposal.strategy_id.value
                             == "positive-ev-ablation")
        fresh_best = book.best_ask(proposal.outcome)
        if floor_applies and not (proposal.expected_price > L.min_price_exclusive
                                  and p_limit > L.min_price_exclusive
                                  and fresh_best is not None
                                  and fresh_best.price > L.min_price_exclusive):
            reasons.append("price_not_above_threshold")
        if p_limit > L.max_price:
            reasons.append("price_above_max")
        if not m.price_ranges or not is_on_grid(p_limit, m.price_ranges):
            reasons.append("limit_price_off_grid_or_unknown_grid")

        # 6. Book freshness, sanity, depth.
        age = (ctx.now - book.ts).total_seconds()
        metrics["book_age_s"] = f"{age:.3f}"
        if age > L.max_book_age_seconds or age < -L.max_clock_skew_seconds:
            reasons.append("stale_or_future_book")
        if book.is_crossed_or_locked:
            reasons.append("crossed_or_locked_book")
        best = book.best_ask(proposal.outcome)
        if best is None:
            reasons.append("no_ask_liquidity")
        depth = ask_depth_notional(book, proposal.outcome, p_limit)
        metrics["depth_notional"] = str(depth)
        if depth < L.min_depth_notional:
            reasons.append("insufficient_depth")
        sp = spread(book, proposal.outcome)
        if sp is None or sp > L.max_spread:
            reasons.append("spread_unknown_or_too_wide")

        # 7. Independent estimate and edge, recomputed on the fresh book.
        est = proposal.estimate
        needs_estimate = proposal.strategy_id.value in ("positive-ev-80", "positive-ev-ablation")
        if needs_estimate:
            if est is None:
                reasons.append("missing_probability_estimate")
            else:
                if est.outcome is not proposal.outcome or est.ticker != m.ticker:
                    reasons.append("estimate_mismatch")
                if not est.usable_for_live:
                    reasons.append("estimate_not_independent_or_not_calibrated")
                f_age = (ctx.now - est.as_of).total_seconds()
                if f_age > L.max_forecast_age_seconds or f_age < -L.max_clock_skew_seconds:
                    reasons.append("stale_forecast")

        # 8. Sizing against hard caps (reduce quantity; never increase).
        qty = proposal.quantity
        step = m.quantity_step
        if qty <= ZERO or step <= ZERO:
            reasons.append("non_positive_quantity")
            qty = ZERO
        base = self.equity_base(ctx)
        metrics["equity_base"] = str(base)
        per_entry_cap = base * L.per_entry_max_loss_frac
        agg_room = base * L.aggregate_open_loss_frac - ctx.exposure.total
        clusters = cluster_keys(m, m.ticker)
        cluster_room = min(
            (base * L.cluster_loss_frac - ctx.exposure.cluster_total(c) for c in clusters),
            default=ZERO,
        )
        cash_room = ctx.account.cash - ctx.pending_cash - L.fee_reserve
        caps = {"per_entry": per_entry_cap, "aggregate": agg_room, "cluster": cluster_room,
                "cash": cash_room}
        if ctx.mode is Mode.LIVE:
            caps["live_canary_total"] = L.live_canary_total_risk - ctx.exposure.total
            if ctx.live_max_total_risk is None:
                reasons.append("live_without_confirmed_risk_budget")
            else:
                caps["live_armed_total"] = ctx.live_max_total_risk - ctx.exposure.total
        cap = min(caps.values())
        metrics.update({f"room_{k}": str(v) for k, v in caps.items()})

        qty = floor_to_step(qty, step) if qty > ZERO else ZERO
        while qty > ZERO and self.max_loss_for(proposal, book, qty) > cap:
            # Shrink proportionally, then by one step, until within the tightest cap.
            ml = self.max_loss_for(proposal, book, qty)
            shrunk = floor_to_step(qty * cap / ml, step) if cap > ZERO else ZERO
            qty = shrunk if shrunk < qty else qty - step
        if qty <= ZERO:
            binding = min(caps, key=lambda k: caps[k])
            reasons.append(f"size_zero_after_caps:{binding}")
            qty = ZERO
        max_loss = self.max_loss_for(proposal, book, qty) if qty > ZERO else ZERO

        # Executable VWAP for the final quantity must itself clear the screen.
        if floor_applies and qty > ZERO:
            vfills = walk_asks(book, proposal.outcome, qty)
            vq = sum((f.quantity for f in vfills), ZERO)
            if vq > ZERO:
                fresh_vwap = sum((f.price * f.quantity for f in vfills), ZERO) / vq
                if fresh_vwap <= L.min_price_exclusive:
                    reasons.append("fresh_vwap_not_above_threshold")

        # Edge re-check at the final quantity on the fresh book.
        if needs_estimate and est is not None and qty > ZERO:
            fills = walk_asks(book, proposal.outcome, qty)
            filled = sum((f.quantity for f in fills), ZERO)
            if filled < qty:
                reasons.append("insufficient_depth_for_quantity")
            else:
                vwap = sum((f.price * f.quantity for f in fills), ZERO) / filled
                fee_pc = self.fees.fee_for_fills(fills, multiplier=m.fee_multiplier) / filled
                breakeven = vwap + fee_pc
                edge = est.q_safe - breakeven - self.slippage_buffer
                metrics.update({"vwap": str(vwap), "fee_per_contract": str(fee_pc),
                                "q_safe": str(est.q_safe), "edge": str(edge)})
                if est.q_safe <= breakeven:
                    reasons.append("q_safe_not_above_breakeven")
                if edge < L.min_edge:
                    reasons.append("edge_below_minimum")
                if vwap > p_limit:
                    reasons.append("vwap_above_limit")
        if p_limit >= ONE or p_limit <= ZERO:
            reasons.append("limit_price_out_of_range")

        approved = not reasons
        return RiskDecision(approved, qty if approved else ZERO,
                            max_loss if approved else ZERO, tuple(reasons), metrics)
