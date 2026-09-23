"""Offline public peer cards; explicit operator trust is required on each host."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from mesh_bbs.config import HostConfig, PeerConfig, update_config
from mesh_bbs.events import BBSError, canonical
from mesh_bbs.store import Store

FIELDS = {"version", "name", "region", "origin", "public_key", "reticulum_identity"}


def export_card(config: HostConfig, store: Store, destination: Path) -> None:
    if not config.reticulum.enabled:
        raise BBSError("Enable Reticulum with mesh-bbs configure before exporting a sync peer")
    try:
        import RNS
    except ImportError as exc:
        raise BBSError("Install the Reticulum extra to export a peer card") from exc
    from mesh_bbs.adapters.reticulum import load_service_identity

    identity = load_service_identity(config.data_dir / "reticulum", RNS)
    card = dict(
        version=1,
        name=config.name,
        region=config.region,
        origin=store.origin,
        public_key=store.public_key,
        reticulum_identity=identity.hash.hex(),
    )
    card["signature"] = store.key.sign(canonical(card)).hex()
    with destination.expanduser().open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(card, indent=2) + "\n")


def read_card(path: Path) -> dict[str, Any]:
    if not path.expanduser().is_file():
        raise BBSError("Peer card must be a regular file")
    with path.expanduser().open("rb") as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise BBSError("Peer card is too large")
    card = json.loads(raw)
    if (
        not isinstance(card, dict)
        or set(card) != FIELDS | {"signature"}
        or type(card["version"]) is not int
        or card["version"] != 1
        or any(not isinstance(card[k], str) for k in FIELDS - {"version"} | {"signature"})
    ):
        raise BBSError("Invalid peer card")
    if (
        not card["name"].strip()
        or len(card["name"]) > 200
        or any(ord(c) < 32 or ord(c) == 127 for c in card["name"])
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", card["region"])
        or len(card["region"]) > 64
    ):
        raise BBSError("Invalid peer name or region")
    PeerConfig(card["origin"], card["public_key"], reticulum_identity=card["reticulum_identity"])
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(card["public_key"])).verify(
            bytes.fromhex(card["signature"]), canonical({key: card[key] for key in FIELDS})
        )
    except (ValueError, InvalidSignature) as exc:
        raise BBSError("Peer card signature does not match its contents") from exc
    return card


def add_peer(
    config: HostConfig,
    path: Path,
    expected: bytes,
    store: Store,
    card: dict[str, Any],
    boards: tuple[str, ...],
) -> Path | None:
    if card["region"] != config.region:
        raise BBSError("Peer belongs to another region; community sync stays inside one region")
    if card["origin"] == store.origin:
        raise BBSError("This is your own peer card")
    if not config.reticulum.enabled:
        raise BBSError("Enable Reticulum with mesh-bbs configure first")
    peer = PeerConfig(card["origin"], card["public_key"], boards, card["reticulum_identity"])
    for existing in config.peers:
        if existing.origin == peer.origin:
            if existing == peer:
                return None
            raise BBSError("This origin already has different settings; review the existing peer")
    return update_config(replace(config, peers=(*config.peers, peer)), path, expected)


def card_fingerprint(card: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(card)).hexdigest()
