from __future__ import annotations

from typing import Any

import pytest

from kalshi_edge.config import AppConfig, load_config


@pytest.fixture(scope="session")
def small_cfg_factory() -> Any:
    def make(strategy: str = "naive-80", **bt: Any) -> AppConfig:
        base = {"n_events": 150, "days": 40, "decision_interval_minutes": 360}
        base.update(bt)
        return load_config("configs/backtest.yaml",
                           overrides={"backtest": base, "strategy": {"id": strategy}})
    return make
