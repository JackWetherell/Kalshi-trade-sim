from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "kalshi"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def fx(name: str) -> Any:
    return json.loads((FIX / name).read_text())


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeAuth:
    def __init__(self, host: str, valid: bool = True) -> None:
        self.host = host
        self.valid = valid

    def is_valid(self, now: datetime) -> bool:
        return self.valid


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def pem_path(tmp_path: Path, rsa_key: rsa.RSAPrivateKey) -> Path:
    p = tmp_path / "key.pem"
    p.write_bytes(rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return p
