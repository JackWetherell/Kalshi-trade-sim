"""Historical-data client: cutoff + historical markets / candlesticks / trades.

Raw response bytes are written immutably (``open(..., "xb")``, never overwritten) with a JSONL
manifest (retrieval time, endpoint, params, sha256). Candlesticks are flagged ``proxy_only``:
they are NOT executable prices and must never be used as fills.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_edge.exchange.rest import KalshiRestClient
from kalshi_edge.exchange.schema import SchemaError, parse_ts, require


@dataclass(frozen=True, slots=True)
class HistoricalCutoff:
    market_settled_ts: datetime
    trades_created_ts: datetime
    orders_updated_ts: datetime


def parse_cutoff(payload: Mapping[str, Any]) -> HistoricalCutoff:
    return HistoricalCutoff(
        market_settled_ts=parse_ts(require(payload, "market_settled_ts", "cutoff"),
                                   "market_settled_ts"),
        trades_created_ts=parse_ts(require(payload, "trades_created_ts", "cutoff"),
                                   "trades_created_ts"),
        orders_updated_ts=parse_ts(require(payload, "orders_updated_ts", "cutoff"),
                                   "orders_updated_ts"),
    )


class RawPageStore:
    """Append-only directory of raw JSON pages plus ``manifest.jsonl``."""

    def __init__(self, directory: str | Path, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._counter = len(list(self.dir.glob("page_*.json")))

    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.jsonl"

    def write(self, endpoint: str, params: Mapping[str, Any], content: bytes, *,
              proxy_only: bool = False) -> dict[str, Any]:
        sha = hashlib.sha256(content).hexdigest()
        slug = re.sub(r"[^A-Za-z0-9]+", "_", endpoint).strip("_")
        self._counter += 1
        name = f"page_{self._counter:06d}_{slug}_{sha[:12]}.json"
        with open(self.dir / name, "xb") as fh:  # 'x': refuse to overwrite
            fh.write(content)
        entry: dict[str, Any] = {
            "file": name, "endpoint": endpoint, "params": dict(params), "sha256": sha,
            "bytes": len(content), "retrieved_at": self._clock().isoformat(),
            "proxy_only": proxy_only,
        }
        with open(self.manifest_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def verify(self) -> list[str]:
        """Return a list of problems (hash mismatches / missing files); empty means intact."""
        problems: list[str] = []
        if not self.manifest_path.exists():
            return problems
        for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            f = self.dir / e["file"]
            if not f.exists():
                problems.append(f"missing {e['file']}")
            elif hashlib.sha256(f.read_bytes()).hexdigest() != e["sha256"]:
                problems.append(f"hash mismatch {e['file']}")
        return problems


def get_cutoff(client: KalshiRestClient, store: RawPageStore | None = None) -> HistoricalCutoff:
    resp = client.send("GET", "/historical/cutoff")
    if store is not None:
        store.write("/historical/cutoff", {}, resp.content)
    data = resp.json()
    if not isinstance(data, dict):
        raise SchemaError("cutoff: not an object")
    return parse_cutoff(data)


def _fetch_all(client: KalshiRestClient, store: RawPageStore, path: str,
               params: Mapping[str, Any], *, limit: int = 200, proxy_only: bool = False,
               max_pages: int = 1000) -> int:
    pages = 0
    for resp in client.paginate_responses(path, params, limit=limit, max_pages=max_pages):
        store.write(path, {**params, "limit": limit}, resp.content, proxy_only=proxy_only)
        pages += 1
    return pages


def fetch_historical_markets(client: KalshiRestClient, store: RawPageStore,
                             params: Mapping[str, Any] | None = None) -> int:
    return _fetch_all(client, store, "/historical/markets", params or {})


def fetch_historical_candlesticks(client: KalshiRestClient, store: RawPageStore, ticker: str, *,
                                  start_ts: int, end_ts: int, period_interval: int) -> int:
    """Candles are uncertain proxies (flagged ``proxy_only`` in the manifest)."""
    path = f"/historical/markets/{ticker}/candlesticks"
    resp = client.send("GET", path, params={"start_ts": start_ts, "end_ts": end_ts,
                                            "period_interval": period_interval})
    store.write(path, {"start_ts": start_ts, "end_ts": end_ts,
                       "period_interval": period_interval}, resp.content, proxy_only=True)
    return 1


def fetch_historical_trades(client: KalshiRestClient, store: RawPageStore,
                            params: Mapping[str, Any] | None = None) -> int:
    return _fetch_all(client, store, "/historical/trades", params or {})
