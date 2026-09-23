"""Durable spacing for explicitly enabled MeshCore flood advertisements."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path


class AdvertSchedule:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_attempt = 0.0
        if path.exists():
            value = json.loads(path.read_text())["last_attempt"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Invalid advertisement schedule")
            if not math.isfinite(value) or value < 0:
                raise ValueError("Invalid advertisement timestamp")
            self.last_attempt = float(value)

    def delay(self, now: float, interval: int) -> float:
        return max(0.0, self.last_attempt + interval - now)

    def mark_attempt(self, now: float) -> None:
        # Save before asking the radio: a lost reply or a crash must not cause
        # another flood advert immediately after reconnecting.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as out:
                temporary = out.name
                json.dump({"last_attempt": now}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
            self.last_attempt = now
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
