"""RSA-PSS request signing for the Kalshi Trade API.

Signature = base64(RSA-PSS(SHA256, MGF1(SHA256), salt_length=digest length)
over ``timestamp_ms + METHOD + path``), path including the ``/trade-api/v2`` prefix and
excluding the query string. Key material is never logged and never appears in ``repr``.
"""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


class AuthError(RuntimeError):
    pass


class RsaPssSigner:
    __slots__ = ("_key", "_key_id")

    def __init__(self, key_id: str, private_key: RSAPrivateKey) -> None:
        if not key_id:
            raise AuthError("empty key id")
        self._key_id = key_id
        self._key = private_key

    @classmethod
    def from_pem_path(cls, key_id: str, path: str | Path) -> RsaPssSigner:
        try:
            data = Path(path).read_bytes()
            key = serialization.load_pem_private_key(data, password=None)
        except Exception as exc:  # never include key text in the message
            raise AuthError(
                f"could not load private key from {path}: {type(exc).__name__}") from None
        if not isinstance(key, RSAPrivateKey):
            raise AuthError("private key is not RSA")
        return cls(key_id, key)

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        path_only = urlsplit(path).path
        message = f"{timestamp_ms}{method.upper()}{path_only}".encode()
        sig = self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("ascii")

    def headers(self, method: str, path: str, timestamp_ms: int) -> dict[str, str]:
        ts = str(timestamp_ms)
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
        }

    def __repr__(self) -> str:
        return "RsaPssSigner(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("RsaPssSigner cannot be pickled")
