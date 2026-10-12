"""Arming ceremony gates, ArmingToken validity, single-instance lock, replay clock."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support import T0

from kalshi_edge.config import AppConfig
from kalshi_edge.exchange.hosts import PROD_REST_HOST, LiveAuthorization
from kalshi_edge.runtime import arming
from kalshi_edge.runtime.arming import (
    FORBIDDEN_ENV_MARKERS,
    MAX_ARMING_TTL,
    ArmingRefused,
    ArmingToken,
    interactive_arm,
)
from kalshi_edge.runtime.clock import ReplayClock
from kalshi_edge.runtime.lock import InstanceLockedError, SingleInstanceLock

D = Decimal
SRC = Path(arming.__file__).resolve().parents[1]


def live_cfg(allow: bool = True) -> AppConfig:
    return AppConfig.model_validate({"mode": "live", "allow_live_trading": allow,
                                     "exchange": {"environment": "prod"}})


class FakeTty:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def human(monkeypatch: pytest.MonkeyPatch):
    """Returns a function that pretends we are a human at a terminal (clears automation
    markers, fakes a TTY). Must be called inside the test body: pytest re-sets
    PYTEST_CURRENT_TEST after fixture setup."""
    def go() -> None:
        for m in FORBIDDEN_ENV_MARKERS:
            monkeypatch.delenv(m, raising=False)
        monkeypatch.setattr(sys, "stdin", FakeTty(True))
        monkeypatch.setattr(sys, "stdout", FakeTty(True))
    return go


def typed_ok(risk: str = "5"):
    """Scripted input: echoes the printed challenge, then the risk amount."""
    seen: list[str] = []

    def inp(prompt: str) -> str:
        seen.append(prompt)
        m = re.search(r"'(ARM \S+ [0-9A-F]{6})'", prompt)
        return m.group(1) if m else risk

    return inp


def arm(cfg: AppConfig | None = None, **kw):
    return interactive_arm(cfg or live_cfg(), host=PROD_REST_HOST, max_total_risk=D("5"),
                           now=T0, print_fn=lambda s: None, **kw)


# ------------------------------------------------------------------ refusal paths
def test_refuses_under_pytest_without_any_patching() -> None:
    with pytest.raises(ArmingRefused):
        arm(input_fn=typed_ok())


@pytest.mark.parametrize("marker", FORBIDDEN_ENV_MARKERS)
def test_refuses_under_each_automation_marker(human, monkeypatch: pytest.MonkeyPatch,
                                              marker: str) -> None:
    human()
    monkeypatch.setenv(marker, "1")
    with pytest.raises(ArmingRefused, match="automated"):
        arm(input_fn=typed_ok())


def test_claudecode_and_ci_are_forbidden_markers() -> None:
    assert {"CI", "CLAUDECODE", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST"} <= set(
        FORBIDDEN_ENV_MARKERS)


@pytest.mark.parametrize(("stdin_tty", "stdout_tty"), [(False, True), (True, False),
                                                       (False, False)])
def test_refuses_when_not_a_tty(human, monkeypatch: pytest.MonkeyPatch, stdin_tty: bool,
                                stdout_tty: bool) -> None:
    human()
    monkeypatch.setattr(sys, "stdin", FakeTty(stdin_tty))
    monkeypatch.setattr(sys, "stdout", FakeTty(stdout_tty))
    with pytest.raises(ArmingRefused, match="TTY"):
        arm(input_fn=typed_ok())


def test_refuses_when_allow_live_trading_false(human) -> None:
    human()
    with pytest.raises(ArmingRefused, match="allow_live_trading"):
        arm(live_cfg(allow=False), input_fn=typed_ok())


@pytest.mark.parametrize("mode", ["paper", "demo", "backtest"])
def test_refuses_for_non_live_modes(human, mode: str) -> None:
    human()
    env = {"paper": "prod_public", "demo": "demo", "backtest": "none"}[mode]
    cfg = AppConfig.model_validate({"mode": mode, "exchange": {"environment": env}})
    with pytest.raises(ArmingRefused, match="only meaningful"):
        arm(cfg, input_fn=typed_ok())


def test_refuses_on_challenge_mismatch_and_never_asks_risk(human) -> None:
    human()
    prompts: list[str] = []

    def inp(p: str) -> str:
        prompts.append(p)
        return "ARM api.elections.kalshi.com 000000"

    with pytest.raises(ArmingRefused, match="challenge"):
        arm(input_fn=inp)
    assert len(prompts) == 1


@pytest.mark.parametrize("typed", ["", "yes", "ARM", "y", "arm host ABCDEF"])
def test_refuses_on_garbage_answers(human, typed: str) -> None:
    human()
    with pytest.raises(ArmingRefused):
        arm(input_fn=lambda p: typed)


@pytest.mark.parametrize("risk", ["4", "5.01", "abc", "", "$-5"])
def test_refuses_on_risk_confirmation_mismatch(human, risk: str) -> None:
    human()
    def inp(prompt: str) -> str:
        m = re.search(r"'(ARM \S+ [0-9A-F]{6})'", prompt)
        return m.group(1) if m else risk

    with pytest.raises(ArmingRefused, match="risk confirmation"):
        arm(input_fn=inp)


# ------------------------------------------------------------------- success + token
def test_successful_ceremony_issues_bounded_token(human) -> None:
    human()
    printed: list[str] = []
    tok = interactive_arm(live_cfg(), host=PROD_REST_HOST, max_total_risk=D("5"), now=T0,
                          input_fn=typed_ok("$5.00"), print_fn=printed.append,
                          ttl=timedelta(days=30))
    assert tok.host == PROD_REST_HOST and tok.pid == os.getpid()
    assert tok.expires_at - tok.issued_at == MAX_ARMING_TTL  # ttl clamped to 24h
    assert tok.is_valid(T0 + timedelta(hours=23, minutes=59))
    shown = "\n".join(printed)
    for needle in (PROD_REST_HOST, "MAX POTENTIAL LOSS", live_cfg().config_hash(), "1009"):
        assert needle in shown
    assert isinstance(tok, LiveAuthorization)


def test_challenge_is_fresh_each_time(human) -> None:
    human()
    prompts: list[str] = []

    def inp(prompt: str) -> str:
        prompts.append(prompt)
        m = re.search(r"'(ARM \S+ [0-9A-F]{6})'", prompt)
        return m.group(1) if m else "5"

    arm(input_fn=inp)
    arm(input_fn=inp)
    ch = [m.group(0) for p in prompts if (m := re.search(r"[0-9A-F]{6}'", p))]
    assert len(ch) == 2 and ch[0] != ch[1]  # collision odds 1 in 16.7M
    toks = {arm(input_fn=typed_ok()).nonce for _ in range(3)}
    assert len(toks) == 3


def token(**kw: object) -> ArmingToken:
    base: dict[str, object] = dict(host=PROD_REST_HOST, config_hash="h", issued_at=T0,
                                   expires_at=T0 + timedelta(hours=1), max_total_risk=D(5))
    base.update(kw)
    return ArmingToken(**base)  # type: ignore[arg-type]


def test_token_validity_window_is_half_open() -> None:
    t = token()
    assert not t.is_valid(T0 - timedelta(seconds=1))
    assert t.is_valid(T0)
    assert t.is_valid(T0 + timedelta(hours=1) - timedelta(microseconds=1))
    assert not t.is_valid(T0 + timedelta(hours=1))  # expired exactly at expiry
    assert not t.is_valid(T0 + timedelta(days=2))


def test_token_invalid_in_a_different_process() -> None:
    assert token().is_valid(T0)
    assert not token(pid=os.getpid() + 1).is_valid(T0)


def test_token_with_ttl_over_24h_is_never_valid() -> None:
    t = token(expires_at=T0 + MAX_ARMING_TTL + timedelta(seconds=1))
    assert not t.is_valid(T0 + timedelta(hours=1))
    assert token(expires_at=T0 + MAX_ARMING_TTL).is_valid(T0 + timedelta(hours=1))


def test_token_with_inverted_window_is_invalid() -> None:
    assert not token(expires_at=T0 - timedelta(hours=1)).is_valid(T0 - timedelta(minutes=1))


def test_token_is_frozen() -> None:
    with pytest.raises(AttributeError):
        token().expires_at = T0 + timedelta(days=9)  # type: ignore[misc]


def test_arming_token_is_never_persisted_or_referenced_by_storage(tmp_path: Path) -> None:
    """Restart expires arming: nothing durable can carry a token across processes."""
    offenders = []
    for py in SRC.rglob("*.py"):
        text = py.read_text()
        if py.name == "arming.py":
            continue
        if "ArmingToken" in text and py.parts[-2] in ("execution", "monitoring", "data", "risk"):
            offenders.append(str(py))
    assert offenders == []
    store_src = (SRC / "execution" / "store.py").read_text()
    assert "token" not in store_src.lower() and "arm" not in store_src.lower().replace("form", "")
    # json/pickle serialisation of the token is not part of the module either
    arming_src = (SRC / "runtime" / "arming.py").read_text()
    for banned in ("pickle", "json.dump", "write_text", "open(", "put("):
        assert banned not in arming_src


# ---------------------------------------------------------------------------- lock
def test_second_lock_holder_is_refused_and_release_allows_reacquire(tmp_path: Path) -> None:
    p = tmp_path / "state" / "trader.lock"
    a = SingleInstanceLock(p)
    a.acquire()
    assert p.read_text() == str(os.getpid())
    b = SingleInstanceLock(p)
    with pytest.raises(InstanceLockedError):
        b.acquire()
    a.release()
    b.acquire()  # now free
    b.release()
    b.release()  # idempotent


def test_lock_context_manager(tmp_path: Path) -> None:
    p = tmp_path / "l"
    with SingleInstanceLock(p), pytest.raises(InstanceLockedError), SingleInstanceLock(p):
        pass
    with SingleInstanceLock(p):
        pass


def test_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    p = tmp_path / "l"
    code = (
        "import sys\nfrom pathlib import Path\n"
        "from kalshi_edge.runtime.lock import SingleInstanceLock, InstanceLockedError\n"
        "try:\n    SingleInstanceLock(Path(sys.argv[1])).acquire()\n"
        "except InstanceLockedError:\n    sys.exit(7)\n")
    with SingleInstanceLock(p):
        r = subprocess.run([sys.executable, "-c", code, str(p)], capture_output=True, timeout=60,
                           check=False)
        assert r.returncode == 7, r.stderr
    r2 = subprocess.run([sys.executable, "-c", code, str(p)], capture_output=True, timeout=60,
                        check=False)
    assert r2.returncode == 0, r2.stderr


# --------------------------------------------------------------------------- clock
def test_replay_clock_never_moves_backwards() -> None:
    c = ReplayClock(T0)
    c.advance(timedelta(seconds=5))
    assert c.now() == T0 + timedelta(seconds=5)
    with pytest.raises(ValueError):
        c.set(T0)
    c.set(c.now())  # same instant ok
    with pytest.raises(ValueError):
        ReplayClock(T0.replace(tzinfo=None))
