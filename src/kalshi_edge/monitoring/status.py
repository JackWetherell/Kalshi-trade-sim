"""Read-only status snapshot for the terminal dashboard (`kalshi-edge status`)."""

from __future__ import annotations

import json
from typing import Any

from kalshi_edge.config import AppConfig
from kalshi_edge.execution.store import Store
from kalshi_edge.risk.killswitch import KillSwitch


def collect_status(cfg: AppConfig) -> dict[str, Any]:
    ks = KillSwitch(cfg.paths.kill_switch_path).state()
    out: dict[str, Any] = {
        "mode": cfg.mode.value,
        "config_hash": cfg.config_hash(),
        "strategy": cfg.strategy.id.value,
        "live_trading_allowed_in_config": cfg.allow_live_trading,
        "kill_switch": {"engaged": ks.engaged, "reason": ks.reason, "since": ks.since},
        "state_dir": str(cfg.paths.state_dir),
    }
    if not cfg.paths.db_path.exists():
        out["store"] = "no state yet (trader has not run with this config)"
        return out
    store = Store(cfg.paths.db_path)
    try:
        out["risk_state"] = store.get("risk_state")
        out["decisions"] = store.decision_counts()
        out["open_orders"] = [o.client_order_id for o in store.open_orders()]
        out["unresolved_orders"] = [o.client_order_id for o in store.unresolved_orders()]
        out["fills"] = len(store.fills())
        out["settlements"] = len(store.settlements())
        paper = store.get("paper_venue_state")
        if isinstance(paper, dict):
            out["paper_account"] = {"cash": paper.get("cash"),
                                    "realized_pnl": paper.get("realized_pnl"),
                                    "positions": len(paper.get("positions", []))}
        out["recent_events"] = [
            {"ts": r["ts"], "kind": r["kind"], "payload": json.loads(r["payload"])}
            for r in store.recent_events(10)]
    finally:
        store.close()
    return out
