# Architecture Decision Record — kalshi-edge

Date: 2026-10-12. Status: research prototype; **live trading disabled**.

## ADR-1 Ground-truth verification and its limits
- Official Kalshi docs and APIs were **unreachable from the build container** (network policy denied
  `docs.kalshi.com`, `api.elections.kalshi.com`, `demo-api.kalshi.co`, `external-api.kalshi.com`, and all
  weather-data hosts). API facts were confirmed only through web-search snippets of docs.kalshi.com
  (details, URLs and every unverified assumption: "Exchange adapter" section below).
- Consequence: the exchange adapter is **live-unverified** (`exchange.schema.SCHEMA_VERSION`). Parsers are
  strict and fail closed, so a schema surprise excludes markets or blocks trading instead of mis-pricing.
  First operator task on a networked machine: `uv run kalshi-edge doctor --network`, then a short
  `data record` run and the `-m network` tests to confirm payload shapes.

## ADR-2 Single local Python process
Python ≥3.12, `uv` + `pyproject.toml` + `uv.lock`, pydantic v2 configs (unknown keys rejected), Typer CLI,
SQLite WAL (single writer, guarded by an `fcntl` lock), gzip-JSONL raw capture (Parquet optional later),
`httpx` (sync) and `websockets`. No microservices. Postgres would only need a new `Store`.

## ADR-3 One decision pipeline for all modes
`runtime.loop.TradingLoop` runs scan → estimate → strategy → fresh book/ledger → `HardRiskManager` →
persisted intent → venue → reconcile. Mode-specific parts are only the `MarketDataProvider`, `Clock` and
`ExecutionVenue`: `PaperVenue` (backtest + paper), `DemoExecutionVenue`, `LiveExecutionVenue`.
The backtester drives the same loop with a `ReplayClock`.

## ADR-4 Money and prices
`decimal.Decimal` everywhere in accounting; floats are refused by `domain.D`. Executable price = depth-walk of
the **ask** side (YES ask = 1 − NO bid). Limit price = worst level touched, required to be on the market's
`price_ranges` grid (missing grid ⇒ no trade). Max loss of a long binary = qty × limit + fees.

## ADR-5 Fees
`fee = round_up(rate × multiplier × C × P × (1−P))`, taker 0.07, maker 0.0175, checked against Kalshi's
published example (100 @ $0.55 → $1.74). The exact rounding unit / accumulator after sub-penny pricing is
unresolved, so each fill is rounded **up to $0.01** (never cheaper than reality). Configurable stress multiplier.

## ADR-6 Strategies
A/B are backtest/paper research controls; the risk manager rejects them in demo/live. C trades only with an
*independent* estimate whose calibration status is ACCEPTED under predeclared criteria
(docs/TRADING_POLICY.md) and `q_safe − p − f − slippage ≥ min_edge` with `q_safe > p + f` strictly. D drops the
0.80 floor (backtest only). Sizing: 10%-Kelly on `q_safe` or a fixed budget, *then* hard caps. Details:
docs/MODELS.md.

## ADR-7 Risk and safety model
- Hard ceilings live in `config.py` (YAML can only lower them). Caps: per-entry 1% (≤2%), aggregate 10%,
  cluster 5% (per event and per category+settlement date), daily 3%, weekly 6% (latched), drawdown 10%
  (latches the kill switch), ≤10 orders/day, cash minus fee reserve. Equity base = min(designated capital,
  conservative mark, venue equity). Gains are not re-levered automatically.
- Kill switch: a file outside config; unreadable ⇒ engaged; reset only interactively.
- Idempotency: `client_order_id` is the SQLite primary key; ambiguous submits become UNKNOWN and are
  resolved by query, never by blind resubmit; unresolved orders block new trading; reconciliation runs every step.
- Live: deny-by-default `LiveExecutionVenue` + `ProductionOrderGuard` on every HTTP request. It needs
  (1) an operator-written config with `allow_live_trading: true` (`live` mode only), (2) an operator-provisioned
  production key that is never in the repo, and (3) an interactive TTY arming ceremony with a random challenge,
  refused under CI/agent environments. The token lives in memory only, expires in ≤24h and on restart. Paper
  and demo processes remove production credential env vars at startup.

## ADR-8 Probability model
The only independent model so far is a weather daily-high threshold model (forecast mean/sd → normal CDF with
continuity correction) plus isotonic/Platt calibration and a Wilson-bound `q_safe`. No live forecast feed is
wired in (hosts unreachable), so in paper/demo/live **C has no estimator and produces NO TRADE**, by design.

## ADR-9 Data and backtesting honesty
Historical candles and trades cannot reconstruct depth, so the backtester replays recorded order-book
snapshots (`kalshi-edge data record`) or seeded **synthetic** worlds used for engine validation only.
docs/RESULTS.md must say NO EVIDENCE YET until real recorded data supports a conclusion.

---

## Exchange adapter (src/kalshi_edge/exchange) - verification status, 2026-10-12

### Environment limitation
The build container's network policy blocked every Kalshi host (docs.kalshi.com, api.elections.kalshi.com,
demo-api.kalshi.co, external-api.kalshi.com). Nothing below was verified against a live endpoint or the
full docs pages. Verification was limited to **WebSearch snippets** of docs.kalshi.com. Fixtures in
`tests/fixtures/kalshi/` are hand-written to the documented shapes, NOT recorded responses.
`schema.SCHEMA_VERSION` records this ("live-unverified").

### Verified (search snippets of official docs, 2026-10-12)
- Orderbook: `GET /markets/{ticker}/orderbook` -> `orderbook_fp` with `yes_dollars` / `no_dollars`, each a list of
  `[price_dollars_string, count_fp_string]`, ascending price, best bid last, bids only. No auth.
  https://docs.kalshi.com/getting_started/orderbook_responses , https://docs.kalshi.com/python-sdk/models/OrderbookCountFp
- Create Order V2: `POST /portfolio/events/orders`; `side` bid/ask relative to YES; `count` string ("10.00"),
  `price` string ("0.5600"); `time_in_force` required; optional `post_only`, `reduce_only`, `client_order_id`;
  response `order_id, fill_count, remaining_count, ts_ms, client_order_id, average_fill_price, average_fee_paid`.
  Legacy `/portfolio/orders` slated for deprecation. https://docs.kalshi.com/api-reference/orders/create-order-v2
- Fills model (SDK 3.13): `count_fp`, `yes_price_dollars`, `no_price_dollars`, `is_taker`, `fee_cost` (dollar string).
  https://cdn.jsdelivr.net/npm/kalshi-typescript@3.13.0/docs/Fill.md
- Market: `price_ranges` {start,end,step} strings; `fractional_trading_enabled` boolean; fixed-point migration
  (March 2026). https://docs.kalshi.com/python-sdk/models/PriceRange , https://docs.kalshi.com/getting_started/fixed_point_migration
- Balance: integer cents `balance`, `portfolio_value`, `updated_ts` (another page shows `balance_dollars`);
  both accepted, must agree. https://docs.kalshi.com/api-reference/portfolio/get-balance
- Historical: `GET /historical/cutoff` returns `market_settled_ts`, `trades_created_ts`, `orders_updated_ts`;
  `/historical/markets`, `/historical/trades`, `/historical/positions` exist.
  https://docs.kalshi.com/api-reference/historical/get-historical-cutoff-timestamps
- Hosts/auth: REST base `https://api.elections.kalshi.com/trade-api/v2`; WS handshake needs KALSHI-ACCESS-* headers;
  channel `orderbook_delta` yields `orderbook_snapshot` then `orderbook_delta` with `seq`.
  https://docs.kalshi.com/getting_started/quick_start_websockets
- Fee rounding page describes an order-level accumulator: https://docs.kalshi.com/getting_started/fee_rounding

### NOT verified (assumptions in code; all fail closed)
- Exact **order object** schema for list/get orders (`side`,`action`,`*_price_dollars`,`initial_count_fp`,
  `fill_count_fp`,`remaining_count_fp`, status enum). Snippets showed the older cents schema. Parser is strict.
- Whether a V2 list-orders endpoint exists; `ORDERS_LIST_PATH` uses legacy `/portfolio/orders` (third parties
  report 410). `get_order(client_order_id)` lists and filters client-side (unknown if server filter exists).
  If it 410s, ambiguity resolution FAILS (raises) rather than guessing: the order must stay UNKNOWN.
- `self_trade_prevention_type` enum value (`taker_at_cross` used), `cancel_order_on_pause`, DELETE response body.
- Positions payload (`position_fp`, `market_exposure_dollars`, `fees_paid_dollars`); `cost_basis` = exposure + fees
  (conservative, may double count). Fill `action`/`side` fields and `fee_cost`.
- WebSocket message field names (`yes_dollars_fp`, `no_dollars_fp`, `price_dollars`, `delta_fp`, `side`, `seq`
  per-subscription) and subscribe command shape.
- Series `fee_type` values (`quadratic`, `quadratic_with_maker_fees`; only third-party Allium docs) and
  `fee_multiplier` as JSON number. Unsupported fee types exclude the market.
- Market `status` string enum, `mve_*` field names, `strike_type`/`floor_strike`/`cap_strike` (only third-party Dune
  docs), quantity step for fractional markets (assumed 0.01, opt-in via `allow_fractional`, default whole contracts).
- Whether the `external-api.kalshi.com` host is canonical; both are treated as production.
- Rate-limit tiers (default 5 req/s from ExchangeConfig).
- Whether REST orderbook includes a server timestamp (it does not in the documented shape: book `ts` is request start).

### Open questions
1. Fee rounding unit: fee schedule PDF says round up to $0.01; Allium notes $0.0001 plus an accumulator. `fees.py`
   rounds each fill up to $0.01 (conservative). Confirm against https://kalshi.com/fee-schedule.
2. Empty book shape: `[]` vs `null`/missing. We require `[]` and reject `null`; relax only after seeing a real response.
3. Order response for an unfilled IOC/FOK: we map fill 0 / not resting to CANCELED; confirm vs. docs.
4. `trades_created_ts` maps to `/historical/trades` or `/historical/fills` (docs inconsistent); we call `/historical/trades`.

### Fail-closed choices
- Unknown host == production. Production: only GET to non-`/portfolio` paths pass `ProductionOrderGuard`;
  everything else needs a valid `LiveAuthorization` for that exact host, re-checked on every request. Env credentials
  never open the guard. `PublicMarketData` has no signer and an unauthorized guard.
- `LiveExecutionVenue` raises `LiveTradingDisabled` at construction and on every mutating call (and every /portfolio
  request) unless authorization is valid; the production credential env vars (`KALSHI_PROD_KEY_ID`,
  `KALSHI_PROD_PRIVATE_KEY_PATH`) are read only after authorization checks pass.
- POST/DELETE are never retried; timeout/5xx -> `AmbiguousSubmission` (caller queries `get_order` first); 4xx ->
  `OrderRejected`.
- Money fields must be strings; floats rejected; unknown `*_dollars/_fp/_cents` fields on markets/orders raise
  `SchemaError`; markets that fail normalization or lack series fee info are excluded and recorded in
  `PublicMarketData.skipped`.
- Excluded: non-binary, scalar, `mve_*`, non-open, missing `price_ranges`, unsupported `fee_type`, sell orders/fills.
- WS: any gap, bad delta, or missing seq invalidates the book (`to_orderbook()` -> None) until a fresh snapshot.
- Historical pages are written with exclusive-create plus sha256 manifest; candles flagged `proxy_only`.
