"""Versioned, signed events shared by hosts in one community."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, fields
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_BODY_BYTES = 64 * 1024
HEX_ID = re.compile(r"[0-9a-f]{64}\Z")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class BBSError(ValueError):
    """An operation was rejected without changing persistent state."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def stable_id(*parts: str) -> str:
    return hashlib.sha256(canonical(parts)).hexdigest()


@dataclass(frozen=True)
class Event:
    version: int
    region: str
    origin: str
    public_key: str
    clock: int
    kind: str
    post_id: str
    board: str
    thread_id: str
    parent_id: str
    author: str
    title: str
    body: str
    created_at: str
    post_key: str = ""
    source_id: str = ""
    source_item: str = ""
    event_id: str = ""
    signature: str = ""

    def unsigned(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k not in {"event_id", "signature"}}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Event:
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
            raise BBSError("Invalid event fields")
        for key, item in value.items():
            expected = int if key in {"version", "clock"} else str
            if type(item) is not expected:
                raise BBSError(f"Invalid event field: {key}")
        return cls(**value)

    def validate(self, region: str) -> None:
        if self.version != 1 or self.region != region:
            raise BBSError("Event belongs to a different region or protocol version")
        if not SLUG.fullmatch(self.region) or not SLUG.fullmatch(self.board):
            raise BBSError("Invalid region or board")
        for value in (self.origin, self.public_key, self.post_id, self.thread_id, self.event_id):
            if not HEX_ID.fullmatch(value):
                raise BBSError("Invalid event identifier")
        if self.parent_id and not HEX_ID.fullmatch(self.parent_id):
            raise BBSError("Invalid parent identifier")
        if self.kind not in {"create", "revise", "remove"} or not 0 < self.clock < 2**63:
            raise BBSError("Invalid event operation")
        if self.clock > time.time_ns() // 1_000_000 + 300_000:
            raise BBSError("Event clock is more than five minutes ahead; check host clocks")
        if len(self.post_key) > 256:
            raise BBSError("Post operation ID exceeds the limit")
        if (
            self.kind == "create"
            and not self.source_id
            and (
                not self.post_key
                or self.post_id != stable_id(self.region, self.origin, self.author, self.post_key)
            )
        ):
            raise BBSError("Post identity does not belong to its origin and author")
        if not self.author or len(self.author.encode()) > 256:
            raise BBSError("Invalid author")
        if len(self.title.encode()) > 256 or len(self.body.encode()) > MAX_BODY_BYTES:
            raise BBSError("Post exceeds the text limit")
        if len(self.created_at) > 40 or len(self.source_id) > 128 or len(self.source_item) > 2048:
            raise BBSError("Event metadata exceeds the limit")
        if bool(self.source_id) != bool(self.source_item):
            raise BBSError("Incomplete feed identity")
        if not self.parent_id and self.thread_id != self.post_id:
            raise BBSError("A thread must refer to its own root")
        if self.parent_id and self.parent_id == self.post_id:
            raise BBSError("A post cannot reply to itself")
        if self.source_id and (
            self.parent_id
            or self.author != f"feed:{self.source_id}"
            or self.post_id != stable_id(self.region, "feed", self.source_id, self.source_item)
        ):
            raise BBSError("Invalid feed post identity")
        data = canonical(self.unsigned())
        key = bytes.fromhex(self.public_key)
        if hashlib.sha256(key).hexdigest() != self.origin:
            raise BBSError("Origin does not match the signing key")
        if hashlib.sha256(data).hexdigest() != self.event_id:
            raise BBSError("Event content does not match its identifier")
        try:
            Ed25519PublicKey.from_public_bytes(key).verify(bytes.fromhex(self.signature), data)
        except (InvalidSignature, ValueError) as exc:
            raise BBSError("Invalid event signature") from exc
