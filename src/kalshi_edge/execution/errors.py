"""Execution error types shared by all venues."""


class ExecutionError(Exception):
    pass


class AmbiguousSubmission(ExecutionError):
    """The submit outcome is unknown (timeout / dropped connection after send).

    The caller MUST query the venue by client_order_id before any retry, and must never
    generate a fresh client_order_id for the same decision.
    """


class OrderRejected(ExecutionError):
    """Definitive rejection by the venue (no order exists)."""


class LiveTradingDisabled(ExecutionError):
    """Raised by any attempt to use the production order path without valid arming."""
