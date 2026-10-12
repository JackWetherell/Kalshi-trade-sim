from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from conftest import FakeAuth
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_edge.exchange.auth import AuthError, RsaPssSigner
from kalshi_edge.exchange.hosts import (
    DEMO_REST_BASE,
    DEMO_REST_HOST,
    PROD_REST_BASE,
    PROD_REST_HOST,
    ProductionOrderBlocked,
    ProductionOrderGuard,
)
from kalshi_edge.exchange.rest import KalshiRestClient
from kalshi_edge.execution.errors import LiveTradingDisabled


def test_signature_verifies_and_strips_query(pem_path: Path, rsa_key: rsa.RSAPrivateKey) -> None:
    signer = RsaPssSigner.from_pem_path("kid", pem_path)
    sig = signer.sign("1700000000000", "get", "/trade-api/v2/markets?limit=5")
    rsa_key.public_key().verify(
        base64.b64decode(sig), b"1700000000000GET/trade-api/v2/markets",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    h = signer.headers("GET", "/trade-api/v2/markets", 5)
    assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}


def test_repr_hides_key(pem_path: Path) -> None:
    signer = RsaPssSigner.from_pem_path("secret-kid", pem_path)
    assert "secret-kid" not in repr(signer) and "PRIVATE" not in repr(signer)
    assert "secret-kid" not in str(signer)


def test_bad_key_file(tmp_path: Path) -> None:
    p = tmp_path / "bad.pem"
    p.write_text("not a key")
    with pytest.raises(AuthError):
        RsaPssSigner.from_pem_path("k", p)


def _hit(guard: ProductionOrderGuard, method: str, base: str, path: str) -> int:
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    c = KalshiRestClient(base, guard=guard, transport=t, sleep=lambda s: None)
    return c.send(method, path).status_code


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT"])
def test_guard_blocks_prod_mutations(method: str) -> None:
    with pytest.raises(ProductionOrderBlocked):
        _hit(ProductionOrderGuard(None), method, PROD_REST_BASE, "/portfolio/events/orders")


def test_guard_blocks_prod_portfolio_get_and_is_live_error() -> None:
    with pytest.raises(LiveTradingDisabled):
        _hit(ProductionOrderGuard(None), "GET", PROD_REST_BASE, "/portfolio/balance")


def test_guard_allows_public_get_and_demo_post() -> None:
    assert _hit(ProductionOrderGuard(None), "GET", PROD_REST_BASE, "/markets") == 200
    demo_post = _hit(ProductionOrderGuard(None), "POST", DEMO_REST_BASE, "/portfolio/events/orders")
    assert demo_post == 200


def test_guard_with_env_credentials_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALSHI_DEMO_KEY_ID", "x")
    monkeypatch.setenv("KALSHI_PROD_KEY_ID", "x")
    with pytest.raises(ProductionOrderBlocked):
        _hit(ProductionOrderGuard(None), "POST", PROD_REST_BASE, "/portfolio/events/orders")


def test_guard_authorization_rules() -> None:
    ok = ProductionOrderGuard(FakeAuth(PROD_REST_HOST))
    assert _hit(ok, "POST", PROD_REST_BASE, "/portfolio/events/orders") == 200
    with pytest.raises(ProductionOrderBlocked):
        _hit(ProductionOrderGuard(FakeAuth(PROD_REST_HOST, valid=False)), "POST",
             PROD_REST_BASE, "/portfolio/events/orders")
    with pytest.raises(ProductionOrderBlocked):
        _hit(ProductionOrderGuard(FakeAuth(DEMO_REST_HOST)), "POST", PROD_REST_BASE,
             "/portfolio/events/orders")


def test_unknown_host_treated_as_production() -> None:
    with pytest.raises(ProductionOrderBlocked):
        _hit(ProductionOrderGuard(None), "POST", "https://example.invalid/trade-api/v2", "/x")
