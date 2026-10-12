"""SQLite (WAL) persistence for decisions, orders, fills, settlements and risk state.

Single writer per state directory (enforced by ``runtime.lock.SingleInstanceLock``).
``client_order_id`` is the primary key of ``orders`` so an intent can never be
persisted (and therefore never submitted) twice.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_edge.domain import Fill, OrderIntent, OrderState, Outcome, SettlementResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    outcome TEXT,
    approved INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    payload TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    model_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    outcome TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    max_cost TEXT NOT NULL,
    state TEXT NOT NULL,
    exchange_order_id TEXT,
    filled_quantity TEXT NOT NULL DEFAULT '0',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    exchange_order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    outcome TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    is_taker INTEGER NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    ticker TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    ts TEXT NOT NULL,
    payout TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class DuplicateIntent(Exception):
    """An intent with this client_order_id already exists; never resubmit blindly."""


@dataclass(frozen=True, slots=True)
class StoredOrder:
    client_order_id: str
    decision_id: str
    mode: str
    strategy_id: str
    ticker: str
    event_ticker: str
    outcome: Outcome
    quantity: Decimal
    limit_price: Decimal
    max_cost: Decimal
    state: OrderState
    exchange_order_id: str | None
    filled_quantity: Decimal
    created_at: datetime
    updated_at: datetime
    note: str


def _json_default(o: object) -> str:
    if isinstance(o, Decimal | datetime):
        return str(o) if isinstance(o, Decimal) else o.isoformat()
    return str(o)


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, sort_keys=True)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---------------------------------------------------------------- audit log
    def log_event(self, ts: datetime, kind: str, payload: Any) -> None:
        self._conn.execute("INSERT INTO events(ts, kind, payload) VALUES (?,?,?)",
                           (ts.isoformat(), kind, dumps(payload)))

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT ts, kind, payload FROM events ORDER BY id DESC LIMIT ?", (limit,)))

    def record_decision(self, *, decision_id: str, ts: datetime, mode: str, strategy_id: str,
                        ticker: str, outcome: str | None, approved: bool,
                        reasons: Sequence[str], payload: Any, config_hash: str,
                        model_version: str) -> None:
        self._conn.execute(
            "INSERT INTO decisions(decision_id, ts, mode, strategy_id, ticker, outcome, approved,"
            " reasons, payload, config_hash, model_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, ts.isoformat(), mode, strategy_id, ticker, outcome, int(approved),
             json.dumps(list(reasons)), dumps(payload), config_hash, model_version))

    def decision_counts(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(approved),0) AS a FROM decisions").fetchone()
        return {"decisions": int(row["n"]), "approved": int(row["a"])}

    # ------------------------------------------------------------------- orders
    def insert_intent(self, intent: OrderIntent) -> None:
        now = intent.created_at.isoformat()
        try:
            self._conn.execute(
                "INSERT INTO orders(client_order_id, decision_id, mode, strategy_id, ticker,"
                " event_ticker, outcome, quantity, limit_price, max_cost, state, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (intent.client_order_id, intent.decision_id, intent.mode.value,
                 intent.strategy_id.value, intent.ticker, intent.event_ticker,
                 intent.outcome.value, str(intent.quantity), str(intent.limit_price),
                 str(intent.max_cost), OrderState.INTENT.value, now, now))
        except sqlite3.IntegrityError as exc:
            raise DuplicateIntent(intent.client_order_id) from exc

    def update_order(self, client_order_id: str, state: OrderState, ts: datetime, *,
                     exchange_order_id: str | None = None,
                     filled_quantity: Decimal | None = None, note: str | None = None) -> None:
        cur = self.get_order(client_order_id)
        if cur is None:
            raise KeyError(client_order_id)
        if cur.state.is_terminal and state is not cur.state:
            # Terminal states are final; record the anomaly instead of overwriting.
            self.log_event(ts, "order_state_conflict",
                           {"client_order_id": client_order_id, "have": cur.state.value,
                            "got": state.value})
            return
        self._conn.execute(
            "UPDATE orders SET state=?, updated_at=?, exchange_order_id=COALESCE(?, "
            "exchange_order_id), filled_quantity=COALESCE(?, filled_quantity), "
            "note=COALESCE(?, note) WHERE client_order_id=?",
            (state.value, ts.isoformat(), exchange_order_id,
             None if filled_quantity is None else str(filled_quantity), note, client_order_id))

    def _row_to_order(self, r: sqlite3.Row) -> StoredOrder:
        return StoredOrder(
            client_order_id=r["client_order_id"], decision_id=r["decision_id"], mode=r["mode"],
            strategy_id=r["strategy_id"], ticker=r["ticker"], event_ticker=r["event_ticker"],
            outcome=Outcome(r["outcome"]), quantity=Decimal(r["quantity"]),
            limit_price=Decimal(r["limit_price"]), max_cost=Decimal(r["max_cost"]),
            state=OrderState(r["state"]), exchange_order_id=r["exchange_order_id"],
            filled_quantity=Decimal(r["filled_quantity"]),
            created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]), note=r["note"])

    def get_order(self, client_order_id: str) -> StoredOrder | None:
        r = self._conn.execute("SELECT * FROM orders WHERE client_order_id=?",
                               (client_order_id,)).fetchone()
        return None if r is None else self._row_to_order(r)

    def orders(self, states: Sequence[OrderState] | None = None) -> list[StoredOrder]:
        if states:
            marks = ",".join("?" * len(states))
            q = f"SELECT * FROM orders WHERE state IN ({marks}) ORDER BY created_at"
            rows = self._conn.execute(q, [s.value for s in states])
        else:
            rows = self._conn.execute("SELECT * FROM orders ORDER BY created_at")
        return [self._row_to_order(r) for r in rows]

    def unresolved_orders(self) -> list[StoredOrder]:
        return self.orders([OrderState.INTENT, OrderState.SUBMITTING, OrderState.UNKNOWN])

    def open_orders(self) -> list[StoredOrder]:
        return self.orders([OrderState.RESTING, OrderState.PARTIALLY_FILLED])

    # -------------------------------------------------------------------- fills
    def add_fill(self, fill: Fill) -> bool:
        """Insert a fill; returns False if it was a duplicate (idempotent)."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO fills(fill_id, client_order_id, exchange_order_id, ticker,"
            " outcome, quantity, price, fee, is_taker, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fill.fill_id, fill.client_order_id, fill.exchange_order_id, fill.ticker,
             fill.outcome.value, str(fill.quantity), str(fill.price), str(fill.fee),
             int(fill.is_taker), fill.ts.isoformat()))
        return cur.rowcount == 1

    def fills(self, ticker: str | None = None) -> list[Fill]:
        if ticker:
            rows = self._conn.execute("SELECT * FROM fills WHERE ticker=? ORDER BY ts", (ticker,))
        else:
            rows = self._conn.execute("SELECT * FROM fills ORDER BY ts")
        return [Fill(r["fill_id"], r["client_order_id"], r["exchange_order_id"], r["ticker"],
                     Outcome(r["outcome"]), Decimal(r["quantity"]), Decimal(r["price"]),
                     Decimal(r["fee"]), bool(r["is_taker"]), datetime.fromisoformat(r["ts"]))
                for r in rows]

    # -------------------------------------------------------------- settlements
    def record_settlement(self, ticker: str, result: SettlementResult, ts: datetime,
                          payout: Decimal) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO settlements(ticker, result, ts, payout) VALUES (?,?,?,?)",
            (ticker, result.value, ts.isoformat(), str(payout)))
        return cur.rowcount == 1

    def settled_tickers(self) -> set[str]:
        return {r["ticker"] for r in self._conn.execute("SELECT ticker FROM settlements")}

    def settlements(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM settlements ORDER BY ts"))

    # ---------------------------------------------------------------------- kv
    def put(self, key: str, value: Any) -> None:
        self._conn.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                           " SET value=excluded.value", (key, dumps(value)))

    def get(self, key: str) -> Any | None:
        r = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return None if r is None else json.loads(r["value"])
