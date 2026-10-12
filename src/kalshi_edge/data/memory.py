"""In-memory MarketDataProvider (fixtures, tests, and the backtest replay feed)."""

from __future__ import annotations

from collections.abc import Sequence

from kalshi_edge.domain import Market, MarketStatus, OrderBook


class InMemoryMarketData:
    def __init__(self) -> None:
        self.markets: dict[str, Market] = {}
        self.books: dict[str, OrderBook] = {}

    def upsert_market(self, market: Market) -> None:
        self.markets[market.ticker] = market

    def set_book(self, book: OrderBook) -> None:
        self.books[book.ticker] = book

    def list_markets(self) -> Sequence[Market]:
        return [m for m in self.markets.values() if m.status is MarketStatus.OPEN]

    def get_market(self, ticker: str) -> Market | None:
        return self.markets.get(ticker)

    def get_orderbook(self, ticker: str) -> OrderBook | None:
        return self.books.get(ticker)
