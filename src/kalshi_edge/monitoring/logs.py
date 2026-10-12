"""Structured JSON logging. Every record carries the runtime mode."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SECRET_HINTS = ("private_key", "secret", "password", "authorization", "signature", "api_key")


def redact(obj: Any) -> Any:
    """Best-effort recursive redaction of secret-looking keys in structured payloads."""
    if isinstance(obj, dict):
        return {k: ("***REDACTED***" if any(h in str(k).lower() for h in _SECRET_HINTS)
                    else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact(v) for v in obj]
    return obj


class JsonFormatter(logging.Formatter):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "mode": self.mode,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload["data"] = redact(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(mode: str, log_file: Path | None = None, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    fmt = JsonFormatter(mode)
    h: logging.Handler = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    root.addHandler(h)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
