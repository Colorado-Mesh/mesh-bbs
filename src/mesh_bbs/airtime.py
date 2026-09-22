"""Persisted reservations for a radio's configured reply airtime allowance.

The packet duration is an operator-supplied upper estimate, including framing.
This accounts for BBS replies and their retry allowance, not firmware beacons,
acknowledgements, other applications, or transmissions by forwarding radios.
Firmware may continue queued transmissions after a host-side timeout; this is
an admission budget, not measured RF airtime or a regulatory duty-cycle check.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import stat
import time
from collections.abc import Awaitable, Callable
from pathlib import Path


def _private_file(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Airtime state must be a regular file, not a symlink") from None
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class AirtimeLimiter:
    """Reserve worst-case reply cost before accepting a command's mutations.

    A reservation stays charged throughout command processing and delivery,
    then for a complete rolling window after ``finish``. Failed deliveries
    consume the same allowance. Reopening unfinished work starts a fresh
    cooldown because the last transmission time is unknown after a crash.
    One limiter owns a state file at a time; callers must close it on shutdown.
    """

    def __init__(
        self,
        state_path: Path,
        radio_id: str,
        *,
        budget_seconds: float = 120.0,
        window_seconds: float = 3600.0,
        packet_airtime_seconds: float = 10.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        for name, value in (
            ("budget_seconds", budget_seconds),
            ("window_seconds", window_seconds),
            ("packet_airtime_seconds", packet_airtime_seconds),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if budget_seconds > window_seconds:
            raise ValueError("airtime budget cannot exceed its window")
        if packet_airtime_seconds > budget_seconds:
            raise ValueError("airtime budget must allow at least one packet")
        if not radio_id or len(radio_id) > 128:
            raise ValueError("radio_id must be between 1 and 128 characters")
        try:
            import fcntl
        except ImportError:
            raise RuntimeError("Persisted radio airtime limits require Linux or macOS") from None

        self.budget_seconds = float(budget_seconds)
        self.window_seconds = float(window_seconds)
        self.packet_airtime_seconds = float(packet_airtime_seconds)
        self._clock, self._sleep = clock, sleep
        self._closed = False
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_file = _private_file(state_path.with_name(state_path.name + ".lock"))
        try:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another process owns this radio's airtime state") from None
            os.close(_private_file(state_path))
            self._db = sqlite3.connect(state_path, isolation_level=None, timeout=10)
            try:
                self._db.execute("PRAGMA synchronous=FULL")
                self._db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS reservations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cost REAL NOT NULL,
                        expires REAL
                    );
                    """
                )
                old_radio = self._get("radio_id")
                if old_radio is not None and old_radio != radio_id:
                    raise ValueError("Airtime state belongs to a different radio")
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    self._set("radio_id", radio_id)
                    now, _ = self._now()
                    self._db.execute(
                        "UPDATE reservations SET expires=? WHERE expires IS NULL",
                        (now + window_seconds,),
                    )
                    limits = json.dumps(
                        [self.budget_seconds, self.window_seconds, self.packet_airtime_seconds]
                    )
                    old_limits = self._get("limits")
                    if old_limits is not None and old_limits != limits:
                        old_window = float(json.loads(old_limits)[1])
                        self._set("blocked_until", str(now + max(old_window, window_seconds)))
                    self._set("limits", limits)
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            except BaseException:
                self._db.close()
                raise
        except BaseException:
            os.close(self._lock_file)
            raise

    def _get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def _set(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def _now(self) -> tuple[float, float]:
        wall = self._clock()
        if not math.isfinite(wall):
            raise ValueError("Airtime clock must be finite")
        previous = self._get("last_time")
        now = max(wall, float(previous)) if previous is not None else wall
        self._set("last_time", str(now))
        return now, wall

    def validate_attempts(self, attempts: int) -> float:
        if type(attempts) is not int or not 1 <= attempts <= 10:
            raise ValueError("airtime attempts must be an integer between 1 and 10")
        cost = attempts * self.packet_airtime_seconds
        if cost > self.budget_seconds:
            raise ValueError("airtime budget must allow all transmission attempts for one reply")
        return cost

    def try_reserve(self, attempts: int) -> tuple[int | None, float]:
        """Return a reservation ID, or the delay before checking capacity again."""
        cost = self.validate_attempts(attempts)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            now, wall = self._now()
            self._db.execute("DELETE FROM reservations WHERE expires<=?", (now,))
            blocked = float(self._get("blocked_until") or "0")
            if blocked > now:
                result: tuple[int | None, float] = (None, blocked - wall)
            else:
                rows = self._db.execute("SELECT cost,expires FROM reservations").fetchall()
                used = math.fsum(float(row[0]) for row in rows)
                if used + cost <= self.budget_seconds:
                    cursor = self._db.execute(
                        "INSERT INTO reservations(cost,expires) VALUES (?,NULL)", (cost,)
                    )
                    assert cursor.lastrowid is not None
                    result = (cursor.lastrowid, 0.0)
                else:
                    expirations = [float(row[1]) for row in rows if row[1] is not None]
                    delay = min(expirations) - wall if expirations else 1.0
                    result = (None, max(0.001, delay))
            self._db.execute("COMMIT")
            return result
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    async def acquire(self, attempts: int) -> int:
        while True:
            reservation, delay = self.try_reserve(attempts)
            if reservation is not None:
                return reservation
            await self._sleep(delay)

    def finish(self, reservation: int) -> None:
        """Keep full cost for one window after the adapter's delivery attempt ends."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            now, _ = self._now()
            cursor = self._db.execute(
                "UPDATE reservations SET expires=? WHERE id=? AND expires IS NULL",
                (now + self.window_seconds, reservation),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown or already finished airtime reservation")
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._db.close()
            finally:
                os.close(self._lock_file)
