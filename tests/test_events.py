from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mesh_bbs.events import MAX_BODY_BYTES, BBSError, Event, canonical, stable_id


def signed_event(**changes: Any) -> Event:
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes_raw().hex()
    origin = hashlib.sha256(bytes.fromhex(public_key)).hexdigest()
    post_id = stable_id("colorado-mesh", origin, "meshcore:alice", "request-1")
    values = {
        "version": 1,
        "region": "colorado-mesh",
        "origin": origin,
        "public_key": public_key,
        "clock": 1,
        "kind": "create",
        "post_id": post_id,
        "board": "general",
        "thread_id": post_id,
        "parent_id": "",
        "author": "meshcore:alice",
        "title": "Network news",
        "body": "The new site is ready. 🏔️",
        "created_at": "2026-09-22T12:00:00+00:00",
        "post_key": "request-1",
    }
    values.update(changes)
    event = Event(**values)
    data = canonical(event.unsigned())
    return replace(event, event_id=hashlib.sha256(data).hexdigest(), signature=key.sign(data).hex())


def test_signed_event_roundtrip_preserves_unicode_and_fields() -> None:
    event = signed_event()
    copied = Event.from_dict(event.to_dict())
    copied.validate("colorado-mesh")
    assert copied == event
    assert copied.body.endswith("🏔️")


@pytest.mark.parametrize("change", ["extra", "missing", "bool-clock", "float-version", "body"])
def test_wire_event_requires_exact_fields_and_types(change: str) -> None:
    value = signed_event().to_dict()
    if change == "extra":
        value["unexpected"] = "ignored?"
    elif change == "missing":
        del value["signature"]
    elif change == "bool-clock":
        value["clock"] = True
    elif change == "float-version":
        value["version"] = 1.0
    else:
        value["body"] = ["not", "text"]
    with pytest.raises(BBSError):
        Event.from_dict(value)


@pytest.mark.parametrize("value", [None, [], "event", 123])
def test_wire_event_requires_object(value: object) -> None:
    with pytest.raises(BBSError):
        Event.from_dict(value)


def test_other_region_rejected_even_with_valid_signature() -> None:
    with pytest.raises(BBSError, match="region"):
        signed_event().validate("other-mesh")


def test_content_change_invalidates_event_id() -> None:
    with pytest.raises(BBSError, match="identifier"):
        replace(signed_event(), body="Injected bulletin").validate("colorado-mesh")


def test_recomputed_event_id_does_not_bypass_signature() -> None:
    event = replace(signed_event(), body="Injected bulletin")
    event = replace(event, event_id=hashlib.sha256(canonical(event.unsigned())).hexdigest())
    with pytest.raises(BBSError, match="signature"):
        event.validate("colorado-mesh")


@pytest.mark.parametrize("signature", ["", "zz", "00" * 63, "00" * 64])
def test_malformed_and_incorrect_signatures_rejected(signature: str) -> None:
    with pytest.raises(BBSError, match="signature"):
        replace(signed_event(), signature=signature).validate("colorado-mesh")


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"clock": 0},
        {"clock": -1},
        {"clock": 2**63},
        {"kind": "overwrite"},
        {"board": "../news"},
        {"author": ""},
        {"title": "🛰" * 65},
        {"body": "é" * (MAX_BODY_BYTES // 2 + 1)},
        {"parent_id": "broken"},
        {"post_key": ""},
        {"post_key": "a" * 257},
        {"source_id": "newsletter"},
        {"source_item": "issue-1"},
    ],
)
def test_structurally_invalid_events_rejected_despite_valid_signature(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(BBSError):
        signed_event(**changes).validate("colorado-mesh")


def test_utf8_body_limit_allows_exact_boundary() -> None:
    event = signed_event(body="é" * (MAX_BODY_BYTES // 2))
    event.validate("colorado-mesh")


def test_create_cannot_claim_another_post_id() -> None:
    victim = signed_event()
    forged = signed_event(post_id=victim.post_id, thread_id=victim.post_id)
    with pytest.raises(BBSError, match="identity"):
        forged.validate("colorado-mesh")


def test_feed_identity_is_independent_of_importing_host() -> None:
    post_id = stable_id("colorado-mesh", "feed", "newsletter", "issue-9")
    first = signed_event(
        author="feed:newsletter",
        source_id="newsletter",
        source_item="issue-9",
        post_id=post_id,
        thread_id=post_id,
    )
    second = signed_event(
        author="feed:newsletter",
        source_id="newsletter",
        source_item="issue-9",
        post_id=post_id,
        thread_id=post_id,
    )
    first.validate("colorado-mesh")
    second.validate("colorado-mesh")
    assert first.origin != second.origin
    assert first.post_id == second.post_id


def test_feed_identity_cannot_be_claimed_under_a_human_author() -> None:
    post_id = stable_id("colorado-mesh", "feed", "newsletter", "issue-9")
    with pytest.raises(BBSError, match="feed post identity"):
        signed_event(
            source_id="newsletter",
            source_item="issue-9",
            post_id=post_id,
            thread_id=post_id,
        ).validate("colorado-mesh")


def test_stable_ids_distinguish_parts_and_preserve_unicode() -> None:
    assert stable_id("ab", "c") != stable_id("a", "bc")
    assert stable_id("a", "a") != stable_id("a")
    assert stable_id("mountain 🏔️") == stable_id("mountain 🏔️")
