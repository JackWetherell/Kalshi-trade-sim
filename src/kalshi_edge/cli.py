"""kalshi-edge command line.

There is deliberately no non-interactive way to start live trading: ``run --mode live``
requires an operator-written live config AND a fresh interactive TTY arming ceremony.
"""

from __future__ import annotations

import json
import os
import platform
import signal
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from kalshi_edge import __version__
from kalshi_edge.config import AppConfig, load_config
from kalshi_edge.domain import Mode, StrategyId
from kalshi_edge.runtime.arming import ArmingToken

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Kalshi binary-market research / backtest / paper / demo trader. "
                       "Live trading is disabled by default.")
data_app = typer.Typer(no_args_is_help=True, help="Market data sync and recording.")
app.add_typer(data_app, name="data")

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="YAML config file")]
PROD_SECRET_ENV = ("KALSHI_PROD_KEY_ID", "KALSHI_PROD_PRIVATE_KEY_PATH")


def _load(path: Path, mode: Mode | None = None) -> AppConfig:
    cfg = load_config(path)
    if mode is not None and cfg.mode is not mode:
        raise typer.BadParameter(f"--mode {mode.value} does not match config mode "
                                 f"{cfg.mode.value} in {path}")
    return cfg


def _echo_json(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------- doctor
@app.command()
def doctor(config_dir: Annotated[Path, typer.Option(help="configs directory")] = Path("configs"),
           network: Annotated[bool, typer.Option(help="probe public Kalshi endpoints")] = False
           ) -> None:
    """Check environment, configs, state and safety posture (never prints secrets)."""
    ok = True
    typer.echo(f"kalshi-edge {__version__} | python {platform.python_version()} | "
               f"{platform.system()} {platform.release()}")
    for p in sorted(config_dir.glob("*.yaml")):
        try:
            c = load_config(p)
            armed = "allow_live_trading=TRUE" if c.allow_live_trading else "live disabled"
            typer.echo(f"ok   {p}: mode={c.mode.value} strategy={c.strategy.id.value} "
                       f"hash={c.config_hash()} ({armed})")
        except Exception as exc:
            ok = False
            typer.echo(f"FAIL {p}: {exc}")
    present = [k for k in PROD_SECRET_ENV if os.environ.get(k)]
    if present:
        typer.echo(f"WARN production credential env vars are set ({', '.join(present)}); "
                   "they are ignored by backtest/paper/demo. Unset them in non-live shells.")
    else:
        typer.echo("ok   no production credential env vars present")
    demo = [k for k in ("KALSHI_DEMO_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY_PATH")
            if os.environ.get(k)]
    typer.echo(f"info demo credential env vars set: {len(demo)}/2 (values not shown)")
    if network:
        import httpx

        from kalshi_edge.exchange.hosts import DEMO_REST_BASE, PROD_REST_BASE
        for base in (PROD_REST_BASE, DEMO_REST_BASE):
            try:
                r = httpx.get(f"{base}/exchange/status", timeout=10)
                typer.echo(f"ok   GET {base}/exchange/status -> {r.status_code}")
            except Exception as exc:
                ok = False
                typer.echo(f"FAIL GET {base}/exchange/status: {exc.__class__.__name__}: {exc}")
    typer.echo("doctor: OK" if ok else "doctor: problems found")
    raise typer.Exit(0 if ok else 1)


# ------------------------------------------------------------------------------ data
@data_app.command("sync")
def data_sync(config: ConfigOpt = Path("configs/research.yaml"),
              series: Annotated[list[str] | None, typer.Option(help="series tickers")] = None,
              out: Annotated[Path | None, typer.Option(help="output dir")] = None) -> None:
    """Fetch the historical cutoff and settled historical markets (raw pages + manifest)."""
    from kalshi_edge.exchange.historical import RawPageStore, fetch_historical_markets, get_cutoff
    from kalshi_edge.exchange.rest import PublicMarketData

    cfg = _load(config)
    md = PublicMarketData(cfg.exchange)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    store = RawPageStore(out or cfg.paths.data_dir / "historical" / stamp)
    cutoff = get_cutoff(md.client, store)
    typer.echo(f"historical cutoff: {cutoff}")
    pages = 0
    for s in (series or list(cfg.exchange.series_allowlist)):
        pages += fetch_historical_markets(md.client, store, {"series_ticker": s})
    typer.echo(f"wrote {pages} market pages to {store.dir}")


@data_app.command("record")
def data_record(config: ConfigOpt = Path("configs/research.yaml"),
                interval: Annotated[float, typer.Option(help="seconds between rounds")] = 60.0,
                rounds: Annotated[int, typer.Option(help="0 = run until stopped")] = 0) -> None:
    """Prospectively record public market metadata + order books (gzip JSONL)."""
    from kalshi_edge.data.recorder import SnapshotRecorder
    from kalshi_edge.exchange.rest import PublicMarketData

    cfg = _load(config)
    md = PublicMarketData(cfg.exchange)
    rec = SnapshotRecorder(md.client, cfg.paths.data_dir,
                           series_allowlist=cfg.exchange.series_allowlist,
                           page_limit=cfg.exchange.markets_page_limit)
    n = 0
    while rounds == 0 or n < rounds:
        typer.echo(json.dumps({"ts": datetime.now(UTC).isoformat(), **rec.record_once()}))
        n += 1
        if rounds == 0 or n < rounds:
            time.sleep(interval)


# -------------------------------------------------------------------------- backtest
@app.command()
def backtest(config: ConfigOpt = Path("configs/backtest.yaml"),
             strategy: Annotated[StrategyId | None, typer.Option(help="override strategy")] = None,
             scenario: Annotated[str, typer.Option(help="synthetic scenario")] =
             "favorite_underpriced",
             label: Annotated[str, typer.Option()] = "",
             out_root: Annotated[Path | None, typer.Option(help="reports root")] = None) -> None:
    """Run one deterministic event-time backtest and write a report directory."""
    from kalshi_edge.backtest import run_backtest

    cfg = _load(config, Mode.BACKTEST)
    if strategy is not None:
        cfg = AppConfig.model_validate({**cfg.model_dump(mode="json"),
                                        "strategy": {**cfg.strategy.model_dump(mode="json"),
                                                     "id": strategy.value}})
    root = out_root or cfg.paths.reports_dir
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}_{label or cfg.strategy.id.value}_{scenario}"
    result = run_backtest(cfg, out_dir=root / name, label=label or cfg.strategy.id.value,
                          scenario=scenario)
    typer.echo(f"report: {result.out_dir}")
    keys = ("dataset_label", "final_bankroll", "net_pnl", "roi_on_capital", "n_trades",
            "max_drawdown_pct", "win_rate", "bootstrap_pnl_ci")
    _echo_json({k: result.metrics.get(k) for k in keys if k in result.metrics}
               or result.metrics)


@app.command()
def report(latest: Annotated[bool, typer.Option("--latest", help="most recent run")] = True,
           run_dir: Annotated[Path | None, typer.Option(help="specific run dir")] = None,
           reports_root: Annotated[Path, typer.Option()] = Path("reports")) -> None:
    """Print the summary of a backtest report."""
    from kalshi_edge.backtest import latest_report_dir, render_summary

    target = run_dir or (latest_report_dir(reports_root) if latest else None)
    if target is None:
        typer.echo("no reports found")
        raise typer.Exit(1)
    typer.echo(render_summary(target))


# ------------------------------------------------------------------------------- run
@app.command()
def run(mode: Annotated[Mode, typer.Option(help="paper | demo | live")],
        config: ConfigOpt,
        steps: Annotated[int, typer.Option(help="0 = run until stopped")] = 0,
        interval: Annotated[float | None, typer.Option(help="seconds between steps")] = None
        ) -> None:
    """Run the trading loop in paper (default), demo, or — only after interactive human
    arming — live mode."""
    from kalshi_edge.monitoring.logs import setup_logging
    from kalshi_edge.runtime.factory import build_runtime
    from kalshi_edge.runtime.lock import InstanceLockedError, SingleInstanceLock

    if mode is Mode.BACKTEST:
        raise typer.BadParameter("use `kalshi-edge backtest`")
    cfg = _load(config, mode)
    setup_logging(cfg.mode.value, cfg.paths.state_dir / "trader.log.jsonl")
    if mode is not Mode.LIVE:
        for k in PROD_SECRET_ENV:  # never let paper/demo inherit production credentials
            os.environ.pop(k, None)
    lock = SingleInstanceLock(cfg.paths.lock_path)
    try:
        lock.acquire()
    except InstanceLockedError as exc:
        typer.echo(f"refusing to start: {exc}")
        raise typer.Exit(2) from exc
    token = None
    if mode is Mode.LIVE:
        token = _interactive_live_arming(cfg)
    try:
        rt = build_runtime(cfg, authorization=token)
        rep = rt.loop.startup()
        typer.echo(f"[{cfg.mode.value}] startup reconcile ok={rep.ok} issues={rep.issues[:5]}")
        stop = {"flag": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
        n = 0
        pause = interval if interval is not None else cfg.paper_scan_interval_seconds
        while not stop["flag"] and (steps == 0 or n < steps):
            try:
                step = rt.loop.step()
                typer.echo(json.dumps({"mode": cfg.mode.value, **step.as_dict()}))
            except Exception as exc:  # loop errors never mean "keep trading blindly"
                typer.echo(json.dumps({"mode": cfg.mode.value, "error": repr(exc)[:300]}))
            finally:
                rt.persist()
            n += 1
            if steps == 0 or n < steps:
                time.sleep(pause)
    except KeyboardInterrupt:
        typer.echo("stopped by operator")
    finally:
        lock.release()


def _interactive_live_arming(cfg: AppConfig) -> ArmingToken:
    from kalshi_edge.exchange.hosts import PROD_REST_HOST
    from kalshi_edge.runtime.arming import ArmingRefused, interactive_arm

    max_total = min(cfg.risk.live_canary_total_risk,
                    cfg.strategy_capital * cfg.risk.aggregate_open_loss_frac)
    try:
        return interactive_arm(cfg, host=PROD_REST_HOST, max_total_risk=Decimal(max_total),
                               now=datetime.now(UTC))
    except ArmingRefused as exc:
        typer.echo(f"NOT ARMED: {exc}")
        raise typer.Exit(3) from exc


# ---------------------------------------------------------------------------- status
@app.command()
def status(config: ConfigOpt = Path("configs/paper.yaml")) -> None:
    """Read-only status: kill switch, risk state, orders, recent events."""
    from kalshi_edge.monitoring.status import collect_status

    cfg = _load(config)
    _echo_json(collect_status(cfg))


# ------------------------------------------------------------------------------ kill
@app.command()
def kill(config: ConfigOpt = Path("configs/paper.yaml"),
         reason: Annotated[str, typer.Option(help="why")] = "operator stop",
         reset: Annotated[bool, typer.Option(help="clear the kill switch (interactive)")] = False
         ) -> None:
    """Engage (default) or interactively reset the persistent kill switch."""
    from kalshi_edge.risk.killswitch import KillSwitch

    cfg = _load(config)
    ks = KillSwitch(cfg.paths.kill_switch_path)
    if not reset:
        ks.engage(reason)
        typer.echo(f"kill switch ENGAGED at {ks.path}: {ks.state().reason}")
        typer.echo("The running trader halts new orders and cancels resting entries on its "
                   "next step. Positions are NOT liquidated.")
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        typer.echo("reset requires an interactive terminal")
        raise typer.Exit(2)
    st = ks.state()
    typer.echo(f"kill switch: engaged={st.engaged} reason={st.reason!r} since={st.since}")
    typed = typer.prompt("Type RESET to clear it")
    if typed.strip() != "RESET":
        typer.echo("not reset")
        raise typer.Exit(1)
    ks.reset(operator_confirmed=True)
    typer.echo("kill switch cleared")
    _maybe_clear_latches(cfg)


def _maybe_clear_latches(cfg: AppConfig) -> None:
    """Latched weekly/drawdown stops need a separate, explicit human review to clear."""
    from kalshi_edge.execution.store import Store
    from kalshi_edge.risk.state import RiskState

    if not cfg.paths.db_path.exists():
        return
    store = Store(cfg.paths.db_path)
    try:
        raw = store.get("risk_state")
        if not raw:
            return
        st = RiskState.from_json(raw)
        if not (st.weekly_halt or st.drawdown_kill or st.daily_halt):
            return
        typer.echo(f"latched stops: weekly={st.weekly_halt} drawdown={st.drawdown_kill} "
                   f"daily={st.daily_halt}; equity={st.current_equity} peak={st.peak_equity}")
        typer.echo("Clearing re-bases day/week/peak equity to the current equity.")
        if typer.prompt("Type CLEAR-LATCHES after reviewing the losses").strip() != \
                "CLEAR-LATCHES":
            typer.echo("latches left in place; the trader will stay halted")
            return
        st.weekly_halt = st.drawdown_kill = st.daily_halt = False
        st.peak_equity = st.start_of_day_equity = st.start_of_week_equity = st.current_equity
        store.put("risk_state", st.to_json())
        store.log_event(datetime.now(UTC), "latches_cleared_by_operator", st.to_json())
        typer.echo("latches cleared")
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    app()
