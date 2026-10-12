"""Persistent, fail-closed kill switch.

The switch is a file on disk, separate from strategy config. If the file exists, or its
state cannot be determined (I/O error, permission error), trading is halted. Engaging is
always allowed programmatically; *resetting* requires an explicit operator action
(``kalshi-edge kill --reset`` in an interactive terminal).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class KillState:
    engaged: bool
    reason: str
    since: str | None


class KillSwitch:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def state(self) -> KillState:
        try:
            if not self.path.exists():
                parent = self.path.parent
                if not parent.exists():
                    # A missing state directory is normal on a fresh checkout.
                    return KillState(False, "", None)
                if not os.access(parent, os.R_OK | os.X_OK):
                    return KillState(True, "kill switch directory unreadable (fail closed)", None)
                return KillState(False, "", None)
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:  # fail closed
            return KillState(True, f"kill switch unreadable: {exc}", None)
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            data = {"reason": raw.strip()[:200]}
        return KillState(True, str(data.get("reason", "engaged")), data.get("since"))

    @property
    def engaged(self) -> bool:
        return self.state().engaged

    def engage(self, reason: str, *, now: datetime | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            return  # keep the original reason/time
        ts = (now or datetime.now(UTC)).isoformat()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"reason": reason, "since": ts}), encoding="utf-8")
        os.replace(tmp, self.path)

    def reset(self, *, operator_confirmed: bool) -> None:
        """Clear the switch. Callers must obtain interactive operator confirmation."""
        if not operator_confirmed:
            raise PermissionError("kill switch reset requires explicit operator confirmation")
        if self.path.exists():
            self.path.unlink()
