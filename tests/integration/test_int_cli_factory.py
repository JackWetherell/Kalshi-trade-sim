"""CLI safety posture, runtime factory wiring, paper persistence, status and logging."""

from __future__ import annotations

import json
import logging
import os
import signal
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from kalshi_edge.cli import app
from kalshi_edge.config import AppConfig
from kalshi_edge.data.memory import InMemoryMarketData
from kalshi_edge.domain import Mode
from kalshi_edge.execution import PaperVenue
from kalshi_edge.monitoring.alerts import AlertSink
from kalshi_edge.monitoring.logs import JsonFormatter, redact, setup_logging
from kalshi_edge.monitoring.status import collect_status
from kalshi_edge.runtime.factory import RulesAllowlistProvider, build_estimator, build_runtime
from tests.support import FixedEstimator, book, market

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def write_cfg(tmp_path: Path, **over: object) -> Path:
    raw: dict[str, object] = {"mode": "paper", "paths": {"state_dir": str(tmp_path / "state")},
                              "risk": {"category_allowlist": ["weather"]},
                              "exchange": {"environment": "prod_public",
                                           "rules_verified_series": ["KXHIGHNY"]}}
    raw.update(over)
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


# ------------------------------------------------------------------------------ CLI
def test_kill_engages_persistently_and_reset_needs_a_tty(tmp_path: Path) -> None:
    cfg = write_cfg(tmp_path)
    r = runner.invoke(app, ["kill", "--config", str(cfg), "--reason", "unit test"])
    assert r.exit_code == 0 and "ENGAGED" in r.output and "NOT liquidated" in r.output
    ks = tmp_path / "state" / "KILL_SWITCH"
    assert json.loads(ks.read_text())["reason"] == "unit test"
    r2 = runner.invoke(app, ["kill", "--config", str(cfg), "--reset"], input="RESET\n")
    assert r2.exit_code == 2 and "interactive" in r2.output
    assert ks.exists()  # an agent/script cannot clear the kill switch non-interactively


def test_run_live_cannot_start_non_interactively(tmp_path: Path) -> None:
    cfg = write_cfg(tmp_path, mode="live", allow_live_trading=True,
                    exchange={"environment": "prod"})
    r = runner.invoke(app, ["run", "--mode", "live", "--config", str(cfg), "--steps", "1"],
                      input="ARM api.elections.kalshi.com AAAAAA\n5\n")
    assert r.exit_code == 3 and "NOT ARMED" in r.output
    assert not (tmp_path / "state" / "kalshi_edge.sqlite3").exists()  # nothing was built
    # lock must be released so the operator is not wedged
    assert runner.invoke(app, ["status", "--config", str(cfg)]).exit_code == 0


def test_run_live_with_config_flag_false_is_refused(tmp_path: Path) -> None:
    cfg = write_cfg(tmp_path, mode="live", allow_live_trading=False,
                    exchange={"environment": "prod"})
    r = runner.invoke(app, ["run", "--mode", "live", "--config", str(cfg), "--steps", "1"])
    assert r.exit_code == 3 and "NOT ARMED" in r.output


def test_run_mode_must_match_config(tmp_path: Path) -> None:
    cfg = write_cfg(tmp_path)
    r = runner.invoke(app, ["run", "--mode", "demo", "--config", str(cfg), "--steps", "1"])
    assert r.exit_code != 0


def test_paper_run_scrubs_production_credentials_and_makes_no_order_calls(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALSHI_PROD_KEY_ID", "prod-key-id-from-operator")
    monkeypatch.setenv("KALSHI_PROD_PRIVATE_KEY_PATH", "/nonexistent/prod.pem")
    old = signal.getsignal(signal.SIGTERM)
    try:
        r = runner.invoke(app, ["run", "--mode", "paper", "--config", str(write_cfg(tmp_path)),
                                "--steps", "1", "--interval", "0"])
    finally:
        signal.signal(signal.SIGTERM, old)
    assert r.exit_code == 0, r.output
    assert "KALSHI_PROD_KEY_ID" not in os.environ  # popped for non-live modes
    assert "prod-key-id" not in r.output
    # network is blocked by conftest; the loop must have halted/errored, never traded
    db = tmp_path / "state" / "kalshi_edge.sqlite3"
    assert db.exists()
    status = collect_status(AppConfig.model_validate(
        yaml.safe_load(write_cfg(tmp_path).read_text())))
    assert status["fills"] == 0 and status["open_orders"] == []


def test_second_runner_is_refused_by_instance_lock(tmp_path: Path) -> None:
    from kalshi_edge.runtime.lock import SingleInstanceLock
    cfg = write_cfg(tmp_path)
    with SingleInstanceLock(tmp_path / "state" / "trader.lock"):
        r = runner.invoke(app, ["run", "--mode", "paper", "--config", str(cfg), "--steps", "1"])
    assert r.exit_code == 2 and "refusing to start" in r.output


def test_doctor_reports_configs_and_prod_env_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALSHI_PROD_KEY_ID", "prod-key-id-from-operator")
    r = runner.invoke(app, ["doctor", "--config-dir", str(ROOT / "configs")])
    assert r.exit_code == 0, r.output
    assert "WARN production credential env vars" in r.output
    assert "prod-key-id-from-operator" not in r.output  # values never printed
    assert "allow_live_trading=TRUE" not in r.output
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "x.yaml").write_text("mode: paper\nbogus: 1\n")
    r2 = runner.invoke(app, ["doctor", "--config-dir", str(bad)])
    assert r2.exit_code == 1 and "FAIL" in r2.output


def test_status_without_state(tmp_path: Path) -> None:
    r = runner.invoke(app, ["status", "--config", str(write_cfg(tmp_path))])
    assert r.exit_code == 0 and "no state yet" in r.output


# ------------------------------------------------------------------------- factory
def paper_cfg(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(yaml.safe_load(write_cfg(tmp_path).read_text()))


def fresh_md() -> InMemoryMarketData:
    now = datetime.now(UTC)
    md = InMemoryMarketData()
    md.upsert_market(market(close_time=now + timedelta(hours=10), open_time=now))
    md.set_book(book(ts=now))
    return md


def test_default_estimator_is_none_so_strategy_c_never_trades(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    assert build_estimator(cfg) is None
    rt = build_runtime(cfg, market_data=fresh_md())
    rep = rt.loop.step()
    assert rep.submitted == 0 and rt.venue.get_fills() == []  # type: ignore[attr-defined]
    rt.store.close()


def test_rules_allowlist_fails_closed_for_unreviewed_series(tmp_path: Path) -> None:
    md = fresh_md()  # raw market claims rules_verified=True
    unreviewed = RulesAllowlistProvider(md, ())
    assert unreviewed.get_market(market().ticker).rules_verified is False  # type: ignore[union-attr]
    assert all(not m.rules_verified for m in unreviewed.list_markets())
    reviewed = RulesAllowlistProvider(md, ("KXHIGHNY",))
    assert reviewed.get_market(market().ticker).rules_verified is True  # type: ignore[union-attr]
    assert reviewed.get_market("nope") is None
    # end-to-end: an unreviewed series is rejected by the risk manager
    raw = write_cfg(tmp_path, exchange={"environment": "prod_public"})
    cfg = AppConfig.model_validate(yaml.safe_load(raw.read_text()))
    rt = build_runtime(cfg, market_data=md, estimator=FixedEstimator(D("0.95")))
    rep = rt.loop.step()
    assert rep.submitted == 0
    assert rep.rejections.get("market_rules_not_verified", 0) >= 1
    rt.store.close()


def test_paper_state_survives_restart_via_snapshot(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    est = FixedEstimator(D("0.95"))
    rt = build_runtime(cfg, market_data=fresh_md(), estimator=est)
    rep = rt.loop.step()
    assert rep.submitted == 1, rep.as_dict()
    rt.persist()
    v1: PaperVenue = rt.venue  # type: ignore[assignment]
    cash, pos = v1.cash, v1.get_account().positions
    n_fills, snap = len(v1.get_fills()), v1.snapshot()
    rt.store.close()

    rt2 = build_runtime(cfg, market_data=fresh_md(), estimator=est)  # "process restart"
    v2: PaperVenue = rt2.venue  # type: ignore[assignment]
    assert v2.cash == cash and v2.get_account().positions == pos
    assert len(v2.get_fills()) == n_fills
    assert v2.snapshot() == snap  # lossless round-trip
    assert rt2.loop.startup().ok  # restored venue reconciles cleanly with the store
    # duplicate client_order_ids from before the restart are still rejected by the venue
    cid = next(iter(v2._orders))
    assert v2.get_order(cid) is not None
    rt2.store.close()


def test_build_runtime_modes(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_runtime(AppConfig(mode=Mode.BACKTEST, paths={"state_dir": tmp_path}))  # type: ignore[arg-type]


# ---------------------------------------------------------------------- monitoring
def test_redact_masks_secret_like_keys_recursively() -> None:
    out = redact({"api_key": "x", "nested": {"Authorization": "Bearer abc", "ok": 1},
                  "list": [{"private_key": "pem"}, {"signature": "s", "n": 2}], "n": 5})
    assert out["api_key"] == "***REDACTED***"
    assert out["nested"] == {"Authorization": "***REDACTED***", "ok": 1}
    assert out["list"][0]["private_key"] == "***REDACTED***"
    assert out["list"][1] == {"signature": "***REDACTED***", "n": 2} and out["n"] == 5


def test_json_formatter_includes_mode_and_redacts() -> None:
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "hello %s", ("w",), None)
    rec.data = {"kind": "k", "secret": "s3"}  # type: ignore[attr-defined]
    payload = json.loads(JsonFormatter("paper").format(rec))
    assert payload["mode"] == "paper" and payload["msg"] == "hello w"
    assert payload["data"] == {"kind": "k", "secret": "***REDACTED***"}
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        rec2 = logging.LogRecord("x", logging.ERROR, __file__, 1, "bad", (), sys.exc_info())
    assert "boom" in json.loads(JsonFormatter("paper").format(rec2))["exc"]


def test_setup_logging_writes_structured_file(tmp_path: Path) -> None:
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        f = tmp_path / "logs" / "t.jsonl"
        setup_logging("paper", f)
        logging.getLogger("t").warning("hi", extra={"data": {"password": "p", "a": 1}})
        for h in root.handlers:
            h.flush()
        line = json.loads(f.read_text().splitlines()[-1])
        assert line["mode"] == "paper" and line["data"]["password"] == "***REDACTED***"
    finally:
        for h in list(root.handlers):
            h.close()
        root.handlers[:] = saved
        root.setLevel(level)


def test_alert_hook_failure_never_breaks_the_trader(tmp_path: Path) -> None:
    from kalshi_edge.execution import Store
    st = Store(tmp_path / "a.db")

    def bad(kind: str, data: dict[str, object]) -> None:
        raise RuntimeError("pager down")

    sink = AlertSink(st, hook=bad)
    sink.alert(datetime.now(UTC), "kill_switch", {"why": "x"})
    sink.alert(datetime.now(UTC), "fill", {})
    assert [k for k, _ in sink.sent] == ["kill_switch", "fill"]
    assert {r["kind"] for r in st.recent_events()} == {"alert:kill_switch", "alert:fill"}


def test_collect_status_reports_state(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    rt = build_runtime(cfg, market_data=fresh_md(), estimator=FixedEstimator(D("0.95")))
    rt.loop.step()
    rt.persist()
    rt.store.close()
    s = collect_status(cfg)
    assert s["mode"] == "paper" and s["live_trading_allowed_in_config"] is False
    assert s["fills"] >= 1 and s["paper_account"]["positions"] == 1
    assert s["kill_switch"]["engaged"] is False
    assert s["decisions"]["approved"] >= 1 and s["recent_events"]
