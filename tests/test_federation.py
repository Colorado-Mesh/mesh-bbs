from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from unittest.mock import Mock

import pytest

from mesh_bbs.events import MAX_BODY_BYTES, BBSError, Event, canonical
from mesh_bbs.federation import MAX_RESPONSE_BYTES, FederationService
from mesh_bbs.store import Grant, Store

PEERS = [f"{index:02x}" * 16 for index in range(1, 4)]
BOARDS = frozenset({"general", "news"})


@pytest.fixture
def hosts(tmp_path: Path):
    stores = [Store(tmp_path / f"host-{index}.db", "colorado") for index in range(3)]
    for store in stores:
        store.grants.update({other.origin: Grant(other.public_key, BOARDS) for other in stores})
    services = [
        FederationService(store, {peer: BOARDS for peer in PEERS if peer != PEERS[index]})
        for index, store in enumerate(stores)
    ]
    yield stores, services
    for store in stores:
        store.close()


def request(mode: str, **values: object) -> dict:
    return {"version": 1, "region": "colorado", "mode": mode, "boards": sorted(BOARDS), **values}


def transport(service: FederationService, caller: str, *, reverse_events: bool = False):
    async def exchange(payload: dict) -> dict:
        response = service.handle_request(caller, payload)
        if reverse_events and response["mode"] == "get":
            response["events"].reverse()
        return response

    return exchange


def event_ids(store: Store) -> list[str]:
    return store.inventory(BOARDS)


def test_unknown_peer_is_rejected_before_storage_reads(hosts) -> None:
    stores, services = hosts
    stores[0].inventory = Mock()
    stores[0].export = Mock()
    with pytest.raises(BBSError, match="Unknown federation peer"):
        services[0].handle_request("ff" * 16, request("inventory", after="", limit=128))
    stores[0].inventory.assert_not_called()
    stores[0].export.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"region": "another-region"},
        {"limit": 129},
        {"limit": True},
        {"after": "invalid"},
        {"boards": ["general", "general"]},
        {"unexpected": "field"},
        {"mode": "push"},
    ],
)
def test_invalid_inventory_request_is_rejected(hosts, changes: dict) -> None:
    _, services = hosts
    with pytest.raises(BBSError):
        services[0].handle_request(
            PEERS[1], {**request("inventory", after="", limit=128), **changes}
        )


def test_board_permissions_are_explicit_intersection(hosts) -> None:
    stores, _ = hosts
    public = stores[0].publish("alice", "one", "general", "Hello", "Public")
    private = stores[0].publish("alice", "two", "news", "News", "Restricted")
    service = FederationService(stores[0], {PEERS[1]: frozenset({"general"})})
    inventory = service.handle_request(PEERS[1], request("inventory", after="", limit=128))
    assert inventory["boards"] == ["general"]
    assert inventory["ids"] == [public.revision_id]
    response = service.handle_request(
        PEERS[1], request("get", ids=[private.revision_id, public.revision_id])
    )
    assert response["unavailable"] == [private.revision_id]
    assert [event["post_id"] for event in response["events"]] == [public.post_id]


def test_get_response_keeps_large_events_below_wire_limit(hosts) -> None:
    stores, services = hosts
    body = "é" * (MAX_BODY_BYTES // 2)
    posts = [
        stores[0].publish("editor", f"issue-{index}", "news", "Issue", body) for index in range(6)
    ]
    ids = [post.revision_id for post in posts]
    response = services[0].handle_request(PEERS[1], request("get", ids=ids))
    assert len(canonical(response)) < MAX_RESPONSE_BYTES
    assert 0 < len(response["events"]) < len(ids)
    assert response["unavailable"] == []
    accounted = [event["event_id"] for event in response["events"]] + response["remaining"]
    assert set(accounted) == set(ids)
    assert len(accounted) == len(ids)


async def test_three_offline_hosts_converge_with_replies_revisions_unicode_and_replay(
    hosts,
) -> None:
    stores, services = hosts
    body = "é" * (MAX_BODY_BYTES // 2)
    root = stores[0].publish("alice", "root", "general", "Weekend plans", "Meet Saturday")
    reply = stores[0].publish(
        "bob", "reply", "general", "Re: Weekend plans", "Count me in", parent_id=root.post_id
    )
    stores[0].revise("alice", "correction", root.post_id, "Weekend plans", "Meet Sunday")
    newsletter = stores[1].publish("editor", "september", "news", "September newsletter", body)
    independent = stores[2].publish(
        "charlie", "island-post", "general", "Offline", "Posted offline"
    )

    # A partition leaves each host with its own writes. Reconnect via B, not a master.
    assert stores[2].missing([newsletter.revision_id]) == [newsletter.revision_id]
    await services[1].pull_peer(PEERS[0], transport(services[0], PEERS[1], reverse_events=True))
    await services[2].pull_peer(PEERS[1], transport(services[1], PEERS[2], reverse_events=True))
    await services[0].pull_peer(PEERS[2], transport(services[2], PEERS[0], reverse_events=True))
    await services[1].pull_peer(PEERS[2], transport(services[2], PEERS[1]))

    inventories = [event_ids(store) for store in stores]
    assert inventories[0] == inventories[1] == inventories[2]
    for store in stores:
        assert store.get_post(root.post_id).body == "Meet Sunday"
        assert store.get_post(reply.post_id).parent_id == root.post_id
        assert store.get_post(reply.post_id).thread_id == root.post_id
        assert store.get_post(newsletter.post_id).body == body
        assert store.get_post(independent.post_id).body == "Posted offline"
    repeated = await services[2].pull_peer(PEERS[1], transport(services[1], PEERS[2]))
    assert repeated.completed_sweep
    assert repeated.accepted_events == 0
    assert event_ids(stores[2]) == inventories[2]


async def test_forwarded_origin_requires_its_own_grant(hosts) -> None:
    stores, services = hosts
    original = stores[0].publish("alice", "original", "general", "Hello", "Signed at A")
    await services[1].pull_peer(PEERS[0], transport(services[0], PEERS[1]))
    del stores[2].grants[stores[0].origin]
    with pytest.raises(BBSError, match="Origin is not trusted"):
        await services[2].pull_peer(PEERS[1], transport(services[1], PEERS[2]))
    assert stores[2].missing([original.revision_id]) == [original.revision_id]
    assert services[2]._read_cursor(PEERS[1], BOARDS) == ""


async def test_bad_event_rolls_back_entire_received_batch_and_cursor(hosts) -> None:
    stores, services = hosts
    for index in range(2):
        stores[0].publish("alice", str(index), "general", "Post", str(index))

    async def tampered(payload: dict) -> dict:
        response = services[0].handle_request(PEERS[1], payload)
        if response["mode"] == "get":
            response["events"][-1]["body"] = "tampered after signing"
        return response

    with pytest.raises(BBSError, match="content does not match"):
        await services[1].pull_peer(PEERS[0], tampered)
    assert event_ids(stores[1]) == []
    assert services[1]._read_cursor(PEERS[0], BOARDS) == ""


async def test_cursor_commit_failure_rolls_back_final_event_batch(hosts, monkeypatch) -> None:
    stores, services = hosts
    stores[0].publish("alice", "one", "general", "Post", "Text")

    def fail_cursor(*args: object) -> None:
        raise OSError("simulated persistence failure")

    monkeypatch.setattr(services[1], "_write_cursor", fail_cursor)
    with pytest.raises(OSError, match="persistence failure"):
        await services[1].pull_peer(PEERS[0], transport(services[0], PEERS[1]))
    assert event_ids(stores[1]) == []
    assert stores[1]._meta(services[1]._cursor_key(PEERS[0])) is None


async def test_partial_large_page_resumes_only_missing_events_without_skipping(hosts) -> None:
    stores, services = hosts
    body = "é" * (MAX_BODY_BYTES // 2)
    for index in range(6):
        stores[0].publish("editor", str(index), "news", "Issue", body)
    first = await services[1].pull_peer(PEERS[0], transport(services[0], PEERS[1]), max_requests=2)
    assert first.stop_reason == "request_budget"
    assert 0 < first.accepted_events < 6
    assert first.cursor == ""
    assert services[1]._read_cursor(PEERS[0], BOARDS) == ""
    already_present = set(event_ids(stores[1]))
    get_requests = []

    async def capture(payload: dict) -> dict:
        if payload["mode"] == "get":
            get_requests.extend(payload["ids"])
        return services[0].handle_request(PEERS[1], payload)

    resumed = await services[1].pull_peer(PEERS[0], capture)
    assert resumed.completed_sweep
    assert resumed.accepted_events + first.accepted_events == 6
    assert not already_present.intersection(get_requests)
    assert event_ids(stores[0]) == event_ids(stores[1])


async def test_full_sweep_reset_discovers_later_insertion_before_cursor(hosts) -> None:
    stores, services = hosts
    for index in range(3):
        stores[0].publish("alice", str(index), "general", "Post", str(index))
    ordered = event_ids(stores[0])
    late = Event.from_dict(stores[0].export([ordered[0]], BOARDS)[0])
    for value in stores[0].export(ordered[1:], BOARDS):
        stores[1].accept(Event.from_dict(value))

    partial = await services[2].pull_peer(
        PEERS[1], transport(services[1], PEERS[2]), inventory_limit=1, max_requests=2
    )
    assert partial.cursor == ordered[1]
    assert partial.stop_reason == "request_budget"
    stores[1].accept(late)  # This event sorts before the saved cursor.
    remainder = await services[2].pull_peer(PEERS[1], transport(services[1], PEERS[2]))
    assert remainder.completed_sweep
    assert remainder.cursor == ""
    assert stores[2].missing([late.event_id]) == [late.event_id]
    rescanned = await services[2].pull_peer(PEERS[1], transport(services[1], PEERS[2]))
    assert rescanned.completed_sweep
    assert rescanned.accepted_events == 1
    assert event_ids(stores[1]) == event_ids(stores[2])


async def test_cursor_survives_restart_and_changed_board_policy_resets_it(tmp_path: Path) -> None:
    source = Store(tmp_path / "source.db", "colorado")
    path = tmp_path / "target.db"
    target = Store(path, "colorado", grants={source.origin: Grant(source.public_key, BOARDS)})
    server = FederationService(source, {PEERS[1]: BOARDS})
    client = FederationService(target, {PEERS[0]: BOARDS})
    try:
        for index in range(3):
            source.publish("alice", str(index), "general", "Post", str(index))
        partial = await client.pull_peer(
            PEERS[0], transport(server, PEERS[1]), inventory_limit=1, max_requests=2
        )
        assert partial.cursor
        target.close()
        target = Store(path, "colorado", grants={source.origin: Grant(source.public_key, BOARDS)})
        restarted = FederationService(target, {PEERS[0]: BOARDS})
        assert restarted._read_cursor(PEERS[0], BOARDS) == partial.cursor
        resumed = await restarted.pull_peer(PEERS[0], transport(server, PEERS[1]))
        assert resumed.completed_sweep
        assert event_ids(target) == event_ids(source)
        with target.transaction():
            restarted._write_cursor(PEERS[0], BOARDS, "a" * 64)
        restricted = FederationService(target, {PEERS[0]: frozenset({"news"})})
        assert restricted._read_cursor(PEERS[0], frozenset({"news"})) == ""
    finally:
        target.close()
        source.close()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda response: response.update(version=2),
        lambda response: response.update(region="another-region"),
        lambda response: response.update(next="f" * 64),
        lambda response: response.update(ids=response["ids"] * 2),
        lambda response: response.update(boards=["private"]),
    ],
)
async def test_invalid_inventory_response_cannot_advance_cursor(hosts, mutation) -> None:
    stores, services = hosts
    stores[0].publish("alice", "one", "general", "Post", "Text")

    async def malformed(payload: dict) -> dict:
        response = services[0].handle_request(PEERS[1], payload)
        mutation(response)
        return response

    with pytest.raises(BBSError):
        await services[1].pull_peer(PEERS[0], malformed)
    assert event_ids(stores[1]) == []
    assert services[1]._read_cursor(PEERS[0], BOARDS) == ""


@pytest.mark.parametrize(
    "mutation", ["omit", "duplicate", "unavailable", "wrong_board", "wrong_id"]
)
async def test_bad_get_partition_or_board_is_rejected(hosts, mutation: str) -> None:
    stores, services = hosts
    stores[0].publish("alice", "one", "general", "Post", "Text")

    async def malformed(payload: dict) -> dict:
        response = copy.deepcopy(services[0].handle_request(PEERS[1], payload))
        if response["mode"] == "get":
            if mutation == "omit":
                response["events"] = []
            elif mutation == "duplicate":
                response["remaining"] = [response["events"][0]["event_id"]]
            elif mutation == "unavailable":
                response["unavailable"] = [response["events"][0]["event_id"]]
                response["events"] = []
            elif mutation == "wrong_board":
                response["boards"] = ["news"]
            else:
                response["events"][0]["event_id"] = "f" * 64
        return response

    with pytest.raises(BBSError):
        await services[1].pull_peer(PEERS[0], malformed)
    assert event_ids(stores[1]) == []


async def test_byte_budget_prevents_sending_when_response_cannot_fit(hosts) -> None:
    _, services = hosts
    calls = []

    async def capture(payload: dict) -> dict:
        calls.append(payload)
        return {}

    result = await services[1].pull_peer(PEERS[0], capture, max_bytes=MAX_RESPONSE_BYTES)
    assert result.stop_reason == "byte_budget"
    assert result.requests == 0
    assert result.transferred_bytes == 0
    assert calls == []


async def test_timeout_is_bounded_and_preserves_cursor(hosts) -> None:
    _, services = hosts

    async def stalled(payload: dict) -> dict:
        await asyncio.sleep(60)
        return {}

    result = await services[1].pull_peer(PEERS[0], stalled, timeout=0.01)
    assert result.stop_reason == "timeout"
    assert result.requests == 1
    assert not result.completed_sweep
    assert services[1]._read_cursor(PEERS[0], BOARDS) == ""
