"""Small, persistent replay and flood guard for public channel instructions."""

from __future__ import annotations

from mesh_bbs.store import Store

HELP_INTERVAL = 300
HELP_REPLAY_WINDOW = 86400


class ChannelHelpGate:
    def __init__(self, store: Store, protocol: str) -> None:
        self.store, self.protocol = store, protocol
        with store.transaction():
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS channel_help_attempts ("
                "protocol TEXT NOT NULL, fingerprint TEXT NOT NULL, attempted REAL NOT NULL, "
                "PRIMARY KEY(protocol,fingerprint))"
            )

    def available(self, fingerprint: str, now: float) -> bool:
        last = self.store.db.execute(
            "SELECT max(attempted) FROM channel_help_attempts WHERE protocol=?",
            (self.protocol,),
        ).fetchone()[0]
        if last is not None and now < last + HELP_INTERVAL:
            return False
        return not self.store.db.execute(
            "SELECT 1 FROM channel_help_attempts "
            "WHERE protocol=? AND fingerprint=? AND attempted>=?",
            (self.protocol, fingerprint, now - HELP_REPLAY_WINDOW),
        ).fetchone()

    def claim(self, fingerprint: str, now: float) -> bool:
        """Record before sending; uncertain delivery must not replay after reconnect."""
        with self.store.transaction():
            self.store.db.execute(
                "DELETE FROM channel_help_attempts WHERE protocol=? AND attempted<?",
                (self.protocol, now - HELP_REPLAY_WINDOW),
            )
            if not self.available(fingerprint, now):
                return False
            return bool(
                self.store.db.execute(
                    "INSERT OR IGNORE INTO channel_help_attempts VALUES (?,?,?)",
                    (self.protocol, fingerprint, now),
                ).rowcount
            )
