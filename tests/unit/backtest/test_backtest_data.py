"""Synthetic generator and recorded loader tests."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_edge.data.recorded import RecordedDataset
from kalshi_edge.data.synthetic import SCENARIOS, SyntheticConfig, SyntheticWorld
from kalshi_edge.domain import Outcome
from kalshi_edge.pricing import is_on_grid

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "kalshi"


def test_world_deterministic_and_well_formed() -> None:
    cfg = SyntheticConfig(n_events=60, days=25)
    a, b = SyntheticWorld(cfg), SyntheticWorld(cfg)
    assert [r.market.ticker for r in a.records] == [r.market.ticker for r in b.records]
    rec = a.records[5]
    t = rec.market.open_time.replace(hour=12)
    assert a.book_at(rec.market.ticker, t) == b.book_at(rec.market.ticker, t)
    cats = {r.category for r in a.records}
    assert cats == {"weather", "economics"}
    assert any(r.market.price_ranges[0].step < 0.01 for r in a.records)  # tapered markets exist
    for r in a.records[:40]:
        book = a.book_at(r.market.ticker, r.market.open_time.replace(hour=15))
        assert book is not None and not book.is_crossed_or_locked
        for side in (Outcome.YES, Outcome.NO):
            ask = book.best_ask(side)
            assert ask is None or is_on_grid(ask.price, r.market.price_ranges)
        assert r.market.rules_verified
        assert r.market.strike["strike_type"] in {"greater", "less", "between"}
    assert set(SCENARIOS) >= {"efficient", "favorite_underpriced", "favorite_overpriced"}


def test_economics_has_no_forecasts() -> None:
    w = SyntheticWorld(SyntheticConfig(n_events=60, days=25))
    assert set(w.station_map) and all(s.startswith("KXHIGH") for s in w.station_map)
    assert all(r.market.series_ticker not in w.station_map
               for r in w.records if r.category == "economics")


def _row(kind: str, ticker: str, payload: object, ts: str = "2026-10-12T12:00:00Z") -> str:
    return json.dumps({"received_ts": ts, "kind": kind, "ticker": ticker, "payload": payload})


def test_recorded_loader_counts_exclusions(tmp_path: Path) -> None:
    market = json.loads((FIX / "market_binary_tapered.json").read_text())["market"]
    market["fee_multiplier"] = 1
    market["category"] = "weather"
    book = json.loads((FIX / "orderbook.json").read_text())
    mve = json.loads((FIX / "market_mve.json").read_text())
    tk = market["ticker"]
    settle = dict(market, status="settled", result="yes", settlement_ts="2026-10-13T14:00:00Z")
    lines = [
        _row("market", tk, {"market": market}),
        _row("orderbook", tk, book, "2026-10-12T12:00:00Z"),
        _row("orderbook", tk, book, "2026-10-12T13:00:00Z"),
        _row("orderbook", "KXNOPE-1", book),
        _row("market", mve["market"]["ticker"], mve),
        _row("settlement", tk, {"market": settle}, "2026-10-13T15:00:00Z"),
        _row("bogus", tk, {}),
        "{not json",
        _row("orderbook", tk, {"orderbook_fp": {"yes_dollars": 1}}),
    ]
    d = tmp_path / "rec" / "2026-10-12"
    d.mkdir(parents=True)
    with gzip.open(d / "a.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    (tmp_path / "rec" / "manifest.json").write_text(
        json.dumps({"rules_verified_series": ["KXHIGHNY"]}))
    ds = RecordedDataset.load(tmp_path / "rec")
    assert ds.kind == "recorded"
    assert len(ds.records) == 1
    rec = ds.records[0]
    assert rec.result is not None and rec.result.value == "yes"
    assert rec.market.rules_verified  # via manifest allowlist
    assert ds.excluded["invalid_json"] == 1
    assert ds.excluded["unknown_kind:bogus"] == 1
    assert ds.excluded["orderbook_without_market_row"] == 1
    assert any(k.startswith("market_invalid") for k in ds.excluded)
    assert any(k.startswith("orderbook_invalid") for k in ds.excluded)
    t = datetime(2026, 10, 12, 12, 30, tzinfo=UTC)
    b = ds.book_at(tk, t)
    assert b is not None and b.ts == datetime(2026, 10, 12, 12, tzinfo=UTC)  # as of t, not later
    assert ds.book_at(tk, datetime(2026, 10, 12, 11, tzinfo=UTC)) is None
    assert ds.provenance["files"][0]["sha256"]
