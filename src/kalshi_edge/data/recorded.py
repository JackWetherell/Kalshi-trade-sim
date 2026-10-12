"""Loader for RECORDED market data written by the operator's recorder.

Files: ``data/recorded/**/*.jsonl.gz`` -- gzip JSON lines, one object per line::

    {"received_ts": "<ISO8601>", "kind": "market"|"orderbook"|"settlement",
     "ticker": "<str>", "payload": <raw Kalshi JSON>}

* ``market``: raw market object (optionally wrapped as ``{"market": {...}}``). It must carry a
  fee multiplier (``fee_multiplier``) and status open when recorded; normalised with
  ``exchange.normalize.normalize_market`` (so unsupported/malformed markets are excluded).
  ``category`` is read from the payload.
* ``orderbook``: raw ``{"orderbook_fp": {"yes_dollars": ..., "no_dollars": ...}}``; the book's
  ``ts`` is ``received_ts`` (the only trustworthy time we have).
* ``settlement``: a market object (or ``{"market": ...}``) with ``result`` yes/no/other
  (other => void) and optional ``settlement_ts``; else ``{"ticker","result","ts"}``.

Rows that are unknown, malformed or fail normalisation are counted in ``excluded`` with a
reason; nothing is silently dropped. Markets with no settlement row stay in the universe with
``result=None`` (unresolved), so there is no survivorship filter. ``rules_verified`` is only
set for series listed in the dataset's ``manifest.json`` key ``rules_verified_series``; otherwise
the risk manager will (correctly) refuse to trade. No forecast data is recorded, so C/D cannot
trade a recorded dataset.
"""

from __future__ import annotations

import bisect
import gzip
import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kalshi_edge.data.replay import MarketRecord, ReplayDataset
from kalshi_edge.domain import Market, OrderBook, SettlementResult
from kalshi_edge.exchange.normalize import normalize_market, normalize_orderbook
from kalshi_edge.exchange.schema import SchemaError, parse_ts

KINDS = frozenset({"market", "orderbook", "settlement"})


def _unwrap(payload: Any, key: str) -> Any:
    if isinstance(payload, Mapping) and key in payload and isinstance(payload[key], Mapping):
        return payload[key]
    return payload


class RecordedDataset(ReplayDataset):
    def __init__(self) -> None:
        super().__init__(kind="recorded", scenario="recorded", restamp_books=False)
        self._snap_ts: dict[str, list[datetime]] = {}
        self._snaps: dict[str, list[OrderBook]] = {}
        self._all_times: list[datetime] = []

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, root: Path) -> RecordedDataset:
        ds = cls()
        files = sorted(root.glob("**/*.jsonl.gz")) if root.exists() else []
        manifest: dict[str, Any] = {}
        mpath = root / "manifest.json"
        if mpath.exists():
            try:
                manifest = json.loads(mpath.read_text())
            except json.JSONDecodeError:
                ds._exclude("manifest_unreadable")
        verified = set(manifest.get("rules_verified_series", []))
        markets: dict[str, Market] = {}
        results: dict[str, tuple[SettlementResult, datetime]] = {}
        book_rows: list[tuple[datetime, str, Any, str]] = []
        file_info = []
        n_rows = 0
        for f in files:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            file_info.append({"file": str(f.relative_to(root)), "sha256": digest})
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    n_rows += 1
                    ds._ingest(line, f"{f.name}:{line_no}", markets, results, book_rows)
        # orderbooks need the market to exist (ticker) but not the other way round
        by_ticker: dict[str, list[tuple[datetime, OrderBook]]] = {}
        for ts, ticker, payload, src in sorted(book_rows, key=lambda x: (x[0], x[1])):
            if ticker not in markets:
                ds._exclude("orderbook_without_market_row")
                continue
            try:
                book = normalize_orderbook(payload, ticker=ticker, ts=ts, received_ts=ts,
                                           source=src)
            except SchemaError as exc:
                ds._exclude(f"orderbook_invalid:{str(exc)[:60]}")
                continue
            by_ticker.setdefault(ticker, []).append((ts, book))
        for tk, lst in by_ticker.items():
            ds._snap_ts[tk] = [t for t, _ in lst]
            ds._snaps[tk] = [b for _, b in lst]
        ds._all_times = sorted({t for ts_list in ds._snap_ts.values() for t in ts_list})
        for tk, m in sorted(markets.items()):
            if m.series_ticker in verified:
                m = _verified(m)
            res = results.get(tk)
            ds.records.append(MarketRecord(
                m, res[0] if res else None, res[1] if res else None, m.event_ticker,
                m.category, (m.close_time - m.open_time).total_seconds() / 3600.0))
        ds.records.sort(key=lambda r: (r.market.open_time, r.market.ticker))
        if ds._all_times:
            ds.start, ds.end = ds._all_times[0], ds._all_times[-1] + timedelta(seconds=1)
        ds.provenance = {
            "label": "recorded data", "root": str(root), "files": file_info, "n_rows": n_rows,
            "dataset_manifest": manifest, "rules_verified_series": sorted(verified),
            "n_markets": len(ds.records),
            "n_orderbook_snapshots": sum(len(v) for v in ds._snaps.values()),
            "n_settled": len(results)}
        return ds

    def _exclude(self, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1

    def _ingest(self, line: str, src: str, markets: dict[str, Market],
                results: dict[str, tuple[SettlementResult, datetime]],
                book_rows: list[tuple[datetime, str, Any, str]]) -> None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            self._exclude("invalid_json")
            return
        if not isinstance(obj, dict):
            self._exclude("row_not_object")
            return
        kind, ticker, payload = obj.get("kind"), obj.get("ticker"), obj.get("payload")
        if kind not in KINDS:
            self._exclude(f"unknown_kind:{kind}")
            return
        if not isinstance(ticker, str) or not ticker or payload is None:
            self._exclude("missing_ticker_or_payload")
            return
        try:
            ts = parse_ts(obj.get("received_ts"), "received_ts")
        except SchemaError:
            self._exclude("bad_received_ts")
            return
        if kind == "orderbook":
            book_rows.append((ts, ticker, payload, src))
        elif kind == "market":
            try:
                m = normalize_market(_unwrap(payload, "market"), source=src)
            except SchemaError as exc:
                self._exclude(f"market_invalid:{str(exc)[:70]}")
                return
            if m.ticker != ticker:
                self._exclude("market_ticker_mismatch")
                return
            markets.setdefault(ticker, m)  # first listing snapshot = metadata known at listing
        else:
            body = _unwrap(payload, "market")
            raw = body.get("result") if isinstance(body, Mapping) else None
            if raw not in ("yes", "no", "void", "scalar", "cancelled", "canceled"):
                self._exclude(f"settlement_result_unusable:{raw}")
                return
            res = (SettlementResult(raw) if raw in ("yes", "no") else SettlementResult.VOID)
            sts_raw = body.get("settlement_ts") or body.get("ts")
            try:
                sts = parse_ts(sts_raw, "settlement_ts") if sts_raw else ts
            except SchemaError:
                sts = ts
            results.setdefault(ticker, (res, sts))

    # ------------------------------------------------------------------ queries
    def book_at(self, ticker: str, t: datetime) -> OrderBook | None:
        ts_list = self._snap_ts.get(ticker)
        if not ts_list:
            return None
        i = bisect.bisect_right(ts_list, t) - 1
        return self._snaps[ticker][i] if i >= 0 else None

    def decision_times(self, interval: timedelta) -> list[datetime]:
        out: list[datetime] = []
        last: datetime | None = None
        for t in self._all_times:
            if last is None or t - last >= interval:
                out.append(t)
                last = t
        return out

    def exclusion_summary(self) -> Counter[str]:
        return Counter(self.excluded)


def _verified(m: Market) -> Market:
    import dataclasses

    return dataclasses.replace(m, rules_verified=True)
