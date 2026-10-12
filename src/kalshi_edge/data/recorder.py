"""Prospective order-book recorder (public data only, no credentials).

Historical candles/trades cannot reconstruct depth, so real executable backtests need
snapshots recorded going forward. Output: gzip JSONL under
``<data_dir>/recorded/<YYYY-MM-DD>/snapshots-<HH>.jsonl.gz``, one object per line::

    {"received_ts": ISO8601, "kind": "market"|"orderbook"|"settlement",
     "ticker": str, "payload": <raw Kalshi JSON, unmodified>}

Raw payloads are preserved so normalization can be re-run when schemas change.
Files are append-only; a per-day ``manifest.json`` records provenance.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_edge.exchange.normalize import is_supported_binary
from kalshi_edge.exchange.rest import KalshiRestClient
from kalshi_edge.exchange.schema import SCHEMA_VERSION

log = logging.getLogger(__name__)


class SnapshotRecorder:
    def __init__(self, client: KalshiRestClient, data_dir: Path, *,
                 series_allowlist: Iterable[str], page_limit: int = 200,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.client = client
        self.root = Path(data_dir) / "recorded"
        self.series = tuple(series_allowlist)
        if not self.series:
            raise ValueError("recorder requires an explicit series allowlist (be polite to "
                             "the API and keep files small)")
        self.page_limit = page_limit
        self.clock = clock
        self._seen_open: set[str] = set()

    def _path(self, ts: datetime) -> Path:
        d = self.root / ts.date().isoformat()
        d.mkdir(parents=True, exist_ok=True)
        manifest = d / "manifest.json"
        if not manifest.exists():
            manifest.write_text(json.dumps({
                "dataset": "kalshi-recorded-snapshots", "source": "Kalshi Trade API v2 (public)",
                "base_url": getattr(self.client, "base_url", ""),
                "api_schema_version": SCHEMA_VERSION, "created_at": ts.isoformat(),
                "series_allowlist": list(self.series),
                "terms_notes": "Public market data via official API; respect rate limits and "
                               "the Kalshi developer agreement.",
            }, indent=2))
        return d / f"snapshots-{ts:%H}.jsonl.gz"

    def _write(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        path = self._path(self.clock())
        with gzip.open(path, "at", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    def record_once(self) -> dict[str, int]:
        rows: list[dict[str, Any]] = []
        counts = {"markets": 0, "orderbooks": 0, "settlements": 0, "errors": 0}
        open_now: set[str] = set()
        for series in self.series:
            try:
                markets = list(self.client.paginate(
                    "/markets", "markets", {"status": "open", "series_ticker": series},
                    limit=self.page_limit))
            except Exception as exc:
                log.warning("market list failed for %s: %s", series, exc)
                counts["errors"] += 1
                continue
            for m in markets:
                ticker = str(m.get("ticker", ""))
                rows.append({"received_ts": self.clock().isoformat(), "kind": "market",
                             "ticker": ticker, "payload": m})
                counts["markets"] += 1
                ok, _ = is_supported_binary(m)
                if not ok:
                    continue
                open_now.add(ticker)
                try:
                    before = self.clock()
                    book = self.client.get(f"/markets/{ticker}/orderbook")
                    rows.append({"received_ts": before.isoformat(), "kind": "orderbook",
                                 "ticker": ticker, "payload": book})
                    counts["orderbooks"] += 1
                except Exception as exc:
                    log.warning("orderbook failed for %s: %s", ticker, exc)
                    counts["errors"] += 1
        # Markets that dropped out of the open list: capture their final state/result.
        for ticker in sorted(self._seen_open - open_now):
            try:
                data = self.client.get(f"/markets/{ticker}")
                rows.append({"received_ts": self.clock().isoformat(), "kind": "settlement",
                             "ticker": ticker, "payload": data})
                counts["settlements"] += 1
            except Exception as exc:
                log.warning("final state fetch failed for %s: %s", ticker, exc)
                counts["errors"] += 1
                open_now.add(ticker)  # retry next round
        self._seen_open = open_now
        self._write(rows)
        return counts
