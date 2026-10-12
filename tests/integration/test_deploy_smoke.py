"""Offline deployment smoke tests (pytest version of deploy/smoke/paper_smoke.sh).

No network and no credentials are needed. In a network-restricted environment every paper
step reports ``halted: market_discovery_failed`` (fail closed), which is the expected result.
All state goes to tmp dirs via config copies; nothing here can reach a production order endpoint.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kalshi_edge.runtime.lock import SingleInstanceLock

REPO = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "kalshi_edge.cli"]


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("KALSHI_PROD") and k not in ("KALSHI_EDGE_NONINTERACTIVE",)}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _cli(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*CLI, *args], cwd=REPO, env=_env(), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout, check=False)


def _tmp_config(src: str, tmp: Path, name: str) -> Path:
    text = (REPO / "configs" / src).read_text(encoding="utf-8")
    state = tmp / name
    out = text.replace(f"state_dir: var/{name}", f"state_dir: {state}")
    assert str(state) in out, f"state_dir not redirected in {src}"
    cfg = tmp / f"{name}.yaml"
    cfg.write_text(out, encoding="utf-8")
    return cfg


def _step_lines(stdout: str) -> list[dict[str, object]]:
    return [json.loads(ln) for ln in stdout.splitlines() if ln.startswith('{"mode"')]


@pytest.fixture(scope="module")
def paper_cfg(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _tmp_config("paper.yaml", tmp_path_factory.mktemp("deploy"), "paper")


@pytest.fixture(scope="module")
def paper_run(paper_cfg: Path) -> subprocess.CompletedProcess[str]:
    return _cli("run", "--mode", "paper", "--config", str(paper_cfg), "--steps", "2",
                "--interval", "0")


def test_paper_run_emits_valid_step_json(paper_run: subprocess.CompletedProcess[str]) -> None:
    assert paper_run.returncode == 0, paper_run.stderr
    steps = _step_lines(paper_run.stdout)
    assert len(steps) == 2
    for s in steps:
        assert s["mode"] == "paper"
        assert "error" not in s
        assert s["submitted"] == 0
        # offline: fail closed; online: whatever the strategy decides, never an error
        assert s["halted"] in (None, "market_discovery_failed")


def test_status_reports_healthy_paper_state(paper_run: subprocess.CompletedProcess[str],
                                            paper_cfg: Path) -> None:
    assert paper_run.returncode == 0
    r = _cli("status", "--config", str(paper_cfg))
    assert r.returncode == 0, r.stderr
    st = json.loads(r.stdout)
    assert st["mode"] == "paper"
    assert st["live_trading_allowed_in_config"] is False
    assert st["kill_switch"]["engaged"] is False
    assert st["unresolved_orders"] == []
    assert st["open_orders"] == []


def test_kill_engages_and_run_halts(paper_run: subprocess.CompletedProcess[str],
                                    paper_cfg: Path) -> None:
    assert paper_run.returncode == 0
    k = _cli("kill", "--config", str(paper_cfg), "--reason", "pytest smoke")
    assert k.returncode == 0, k.stderr
    assert "ENGAGED" in k.stdout
    st = json.loads(_cli("status", "--config", str(paper_cfg)).stdout)
    assert st["kill_switch"]["engaged"] is True
    assert st["kill_switch"]["reason"] == "pytest smoke"
    r = _cli("run", "--mode", "paper", "--config", str(paper_cfg), "--steps", "1",
             "--interval", "0")
    assert r.returncode == 0, r.stderr
    (step,) = _step_lines(r.stdout)
    assert str(step["halted"]).startswith("kill_switch")
    assert step["submitted"] == 0


def test_kill_reset_requires_a_tty(paper_cfg: Path) -> None:
    r = _cli("kill", "--config", str(paper_cfg), "--reset")
    assert r.returncode == 2
    assert "interactive terminal" in r.stdout


def test_second_instance_refused_by_lock(tmp_path: Path) -> None:
    cfg = _tmp_config("paper.yaml", tmp_path, "paper")
    with SingleInstanceLock(tmp_path / "paper" / "trader.lock"):
        r = _cli("run", "--mode", "paper", "--config", str(cfg), "--steps", "1",
                 "--interval", "0")
    assert r.returncode == 2
    assert "refusing to start" in r.stdout
    assert _step_lines(r.stdout) == []


def test_live_with_example_config_never_arms(tmp_path: Path) -> None:
    cfg = _tmp_config("live.example.yaml", tmp_path, "live")
    assert "allow_live_trading: false" in cfg.read_text(encoding="utf-8")
    r = _cli("run", "--mode", "live", "--config", str(cfg), "--steps", "1")
    assert r.returncode == 3
    assert "NOT ARMED" in r.stdout
    assert _step_lines(r.stdout) == []
    # the repo's own example file behaves the same way (state dir is gitignored var/live)
    r2 = _cli("run", "--mode", "live", "--config", "configs/live.example.yaml", "--steps", "1")
    assert r2.returncode != 0
    assert "NOT ARMED" in r2.stdout


def test_paper_mode_rejects_live_config(tmp_path: Path) -> None:
    cfg = _tmp_config("live.example.yaml", tmp_path, "live")
    r = _cli("run", "--mode", "paper", "--config", str(cfg), "--steps", "1")
    assert r.returncode != 0
    assert _step_lines(r.stdout) == []
    assert "does not match config mode" in (r.stdout + r.stderr)


def test_deploy_artifacts_have_no_live_unit() -> None:
    units = sorted((REPO / "deploy" / "systemd").glob("*.service"))
    assert units, "no systemd units found"
    for u in units:
        text = u.read_text(encoding="utf-8")
        exec_lines = [ln for ln in text.splitlines() if ln.startswith("ExecStart=")]
        assert all("--mode live" not in ln for ln in exec_lines), u.name
        assert "live" not in u.stem
    for name in ("paper", "demo", "recorder"):
        assert (REPO / "deploy" / "systemd" / f"kalshi-edge-{name}.service").exists()
    for name in ("paper", "demo"):
        text = (REPO / "deploy" / "systemd" / f"kalshi-edge-{name}.service").read_text()
        assert "UnsetEnvironment=KALSHI_PROD_KEY_ID KALSHI_PROD_PRIVATE_KEY_PATH" in text
