"""Bounded anti-entropy exchanges between explicitly trusted community hosts.

Events retain their original signatures when forwarded. Transport trust permits
an exchange, while Store grants authorize each signing origin for its board.
Lexical inventory cursors reset after each complete sweep: a new event can sort
before yesterday's last event and must still be discovered on the next sweep.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mesh_bbs.events import HEX_ID, MAX_BOARDS, SLUG, BBSError, Event, canonical
from mesh_bbs.store import Store

VERSION = 1
MAX_RESPONSE_BYTES = 512 * 1024
MAX_REQUEST_BYTES = 32 * 1024
MAX_INVENTORY_IDS = 128
MAX_GET_IDS = 32


@dataclass
class PullResult:
    requests: int = 0
    transferred_bytes: int = 0
    accepted_events: int = 0
    completed_sweep: bool = False
    cursor: str = ""
    stop_reason: str = "complete"


class _BudgetExhausted(Exception):
    pass


def _ids(value: object, limit: int) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(not isinstance(item, str) or not HEX_ID.fullmatch(item) for item in value)
        or len(set(value)) != len(value)
    ):
        raise BBSError("Invalid federation event ID list")
    return value


def _boards(value: object) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_BOARDS
        or any(
            not isinstance(item, str) or (item != "*" and not SLUG.fullmatch(item))
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise BBSError("Invalid federation board list")
    return frozenset(value)


def _object_size(value: object, limit: int) -> int:
    if not isinstance(value, dict):
        raise BBSError("Federation messages must be objects")
    try:
        size = len(canonical(value))
    except (TypeError, ValueError, RecursionError) as error:
        raise BBSError("Invalid federation message encoding") from error
    if size >= limit:
        raise BBSError("Federation message exceeds the byte limit")
    return size


class FederationService:
    """Serve inventory/get requests and pull missing signed events.

    ``peer_boards`` keys are authenticated RNS identity hashes (32 hex digits),
    distinct from the event-origin hashes authorized in ``store.grants``.
    ``handle_request`` is safe for the Reticulum adapter's worker threads.
    ``pull_peer`` receives an async callable taking one request dictionary.
    Each host independently schedules pulls from its configured peers.
    """

    def __init__(self, store: Store, peer_boards: Mapping[str, frozenset[str]]) -> None:
        self.store = store
        self.peer_boards: dict[str, frozenset[str]] = {}
        for peer, boards in peer_boards.items():
            if not re.fullmatch(r"[0-9a-f]{32}", peer):
                raise BBSError("Federation peers require lowercase RNS identity hashes")
            if not isinstance(boards, frozenset):
                raise BBSError("Peer board permissions must be a frozenset")
            _boards(sorted(boards))
            self.peer_boards[peer] = boards if "*" in boards else boards & frozenset(store.boards)
        self._pull_locks = {peer: asyncio.Lock() for peer in self.peer_boards}

    def _allowed(self, peer: str) -> frozenset[str]:
        if peer not in self.peer_boards:
            raise BBSError("Unknown federation peer")
        return self.peer_boards[peer]

    def _shared_boards(self, allowed: frozenset[str], requested: frozenset[str]) -> frozenset[str]:
        local = frozenset(self.store.boards)
        return (local if "*" in allowed else allowed) & (local if "*" in requested else requested)

    def _envelope(self, mode: str, boards: frozenset[str], **data: Any) -> dict[str, Any]:
        return {
            "version": VERSION,
            "region": self.store.region,
            "mode": mode,
            "boards": sorted(boards),
            **data,
        }

    def _check_envelope(
        self, payload: object, mode: str, fields: set[str], *, response: bool = False
    ) -> dict[str, Any]:
        _object_size(payload, MAX_RESPONSE_BYTES if response else MAX_REQUEST_BYTES)
        assert isinstance(payload, dict)
        if set(payload) != {"version", "region", "mode", "boards"} | fields:
            raise BBSError("Invalid federation message fields")
        if type(payload["version"]) is not int or payload["version"] != VERSION:
            raise BBSError("Unsupported federation protocol version")
        if payload["region"] != self.store.region or payload["mode"] != mode:
            raise BBSError("Federation response belongs to another region or operation")
        _boards(payload["boards"])
        return payload

    def handle_request(self, peer_identity: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = self._allowed(peer_identity)  # Reject unknown peers before reading storage.
        if not isinstance(payload, dict):
            raise BBSError("Federation messages must be objects")
        mode = payload.get("mode")
        if mode == "inventory":
            request = self._check_envelope(payload, "inventory", {"after", "limit"})
            after, limit = request["after"], request["limit"]
            if not isinstance(after, str) or (after and not HEX_ID.fullmatch(after)):
                raise BBSError("Invalid federation inventory cursor")
            if type(limit) is not int or not 1 <= limit <= MAX_INVENTORY_IDS:
                raise BBSError("Inventory limit must be between 1 and 128")
            boards = self._shared_boards(allowed, _boards(request["boards"]))
            ids = self.store.inventory(boards, after, limit)
            response = self._envelope(
                "inventory",
                boards,
                ids=ids,
                next=ids[-1] if ids else after,
                complete=len(ids) < limit,
            )
        elif mode == "get":
            request = self._check_envelope(payload, "get", {"ids"})
            ids = _ids(request["ids"], MAX_GET_IDS)
            boards = self._shared_boards(allowed, _boards(request["boards"]))
            response = self._envelope("get", boards, events=[], remaining=list(ids), unavailable=[])
            for event_id in ids:
                found = self.store.export([event_id], boards)
                if not found:
                    response["remaining"].remove(event_id)
                    response["unavailable"].append(event_id)
                    continue
                candidate = {
                    **response,
                    "events": [*response["events"], found[0]],
                    "remaining": [item for item in response["remaining"] if item != event_id],
                }
                if len(canonical(candidate)) < MAX_RESPONSE_BYTES:
                    response = candidate
        else:
            raise BBSError("Unsupported federation operation")
        _object_size(response, MAX_RESPONSE_BYTES)
        return response

    def _cursor_key(self, peer: str) -> str:
        return "federation_cursor:" + peer

    def _read_cursor(self, peer: str, boards: frozenset[str]) -> str:
        with self.store.transaction():
            raw = self.store._meta(self._cursor_key(peer))
            if raw is None:
                return ""
            try:
                value = json.loads(raw)
                after = value["after"]
                if value["boards"] == sorted(boards) and isinstance(after, str):
                    if not after or HEX_ID.fullmatch(after):
                        return after
            except (TypeError, ValueError, KeyError):
                pass
            self._write_cursor(peer, boards, "")
            return ""

    def _write_cursor(self, peer: str, boards: frozenset[str], after: str) -> None:
        # Call only inside the same Store transaction as the accepted final batch.
        self.store._set_meta(
            self._cursor_key(peer), canonical({"boards": sorted(boards), "after": after}).decode()
        )

    def _inventory_response(
        self, payload: object, boards: frozenset[str], after: str, limit: int
    ) -> tuple[list[str], frozenset[str], str, bool]:
        response = self._check_envelope(
            payload, "inventory", {"ids", "next", "complete"}, response=True
        )
        effective = _boards(response["boards"])
        if "*" in effective or ("*" not in boards and not effective <= boards):
            raise BBSError("Peer advertised boards outside the requested permissions")
        ids = _ids(response["ids"], limit)
        if ids != sorted(ids) or any(event_id <= after for event_id in ids):
            raise BBSError("Peer inventory is not in increasing cursor order")
        if ids and not effective:
            raise BBSError("Peer advertised events without an allowed board")
        if response["next"] != (ids[-1] if ids else after):
            raise BBSError("Peer inventory cursor does not match its events")
        if type(response["complete"]) is not bool or response["complete"] != (len(ids) < limit):
            raise BBSError("Invalid inventory completion marker")
        return ids, effective, response["next"], response["complete"]

    def _get_response(
        self, payload: object, requested: list[str], boards: frozenset[str]
    ) -> list[Event]:
        response = self._check_envelope(
            payload, "get", {"events", "remaining", "unavailable"}, response=True
        )
        effective = _boards(response["boards"])
        if "*" in effective or ("*" not in boards and not effective <= boards):
            raise BBSError("Peer returned boards outside the requested permissions")
        values = response["events"]
        if not isinstance(values, list) or len(values) > len(requested):
            raise BBSError("Invalid federation event batch")
        events = [Event.from_dict(value) for value in values]
        returned = _ids([event.event_id for event in events], MAX_GET_IDS)
        remaining = _ids(response["remaining"], MAX_GET_IDS)
        unavailable = _ids(response["unavailable"], MAX_GET_IDS)
        combined = returned + remaining + unavailable
        if len(combined) != len(set(combined)) or set(combined) != set(requested):
            raise BBSError("Peer event batch does not account for every requested ID")
        if unavailable:
            raise BBSError("Peer cannot supply an event from its advertised inventory")
        if not events and remaining:
            raise BBSError("Peer event batch made no progress")
        for event in events:
            if event.board not in effective:
                raise BBSError("Peer returned an event from an unpermitted board")
        return events

    async def pull_peer(
        self,
        peer_identity: str,
        request: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        *,
        max_requests: int = 32,
        max_bytes: int = 8 * 1024 * 1024,
        timeout: float = 30.0,
        max_duration: float = 120.0,
        inventory_limit: int = MAX_INVENTORY_IDS,
    ) -> PullResult:
        """Pull within hard budgets, retaining valid batches across interruptions.

        The byte budget accounts for canonical serialized requests and responses.
        Reserve a maximum response before sending each request; unused reservation
        is released when its actual size is known. A budget below one maximum
        response cannot start a request. No cursor passes an unaccepted event.
        """
        boards = self._allowed(peer_identity)
        if type(max_requests) is not int or max_requests < 1:
            raise BBSError("Request budget must be a positive integer")
        if type(max_bytes) is not int or max_bytes < 1:
            raise BBSError("Byte budget must be a positive integer")
        if type(inventory_limit) is not int or not 1 <= inventory_limit <= MAX_INVENTORY_IDS:
            raise BBSError("Inventory limit must be between 1 and 128")
        if not all(math.isfinite(value) and value > 0 for value in (timeout, max_duration)):
            raise BBSError("Federation timeouts must be finite and positive")
        result = PullResult()

        async def exchange(payload: dict[str, Any]) -> dict[str, Any]:
            request_size = _object_size(payload, MAX_REQUEST_BYTES)
            if result.requests >= max_requests:
                result.stop_reason = "request_budget"
                raise _BudgetExhausted
            if result.transferred_bytes + request_size + MAX_RESPONSE_BYTES - 1 > max_bytes:
                result.stop_reason = "byte_budget"
                raise _BudgetExhausted
            result.requests += 1
            result.transferred_bytes += request_size
            async with asyncio.timeout(timeout):
                response = await request(payload)
            result.transferred_bytes += _object_size(response, MAX_RESPONSE_BYTES)
            return response

        try:
            async with asyncio.timeout(max_duration), self._pull_locks[peer_identity]:
                result.cursor = self._read_cursor(peer_identity, boards)
                while True:
                    response = await exchange(
                        self._envelope(
                            "inventory", boards, after=result.cursor, limit=inventory_limit
                        )
                    )
                    ids, effective, next_cursor, complete = self._inventory_response(
                        response, boards, result.cursor, inventory_limit
                    )
                    pending = self.store.missing(ids)
                    if not pending:
                        with self.store.transaction():
                            self._write_cursor(
                                peer_identity, boards, "" if complete else next_cursor
                            )
                    while pending:
                        requested = pending[:MAX_GET_IDS]
                        response = await exchange(self._envelope("get", effective, ids=requested))
                        events = self._get_response(response, requested, effective)
                        with self.store.transaction():
                            accepted = sum(self.store.accept(event) for event in events)
                            pending = self.store.missing(ids)
                            if not pending:
                                self._write_cursor(
                                    peer_identity, boards, "" if complete else next_cursor
                                )
                        result.accepted_events += accepted
                    result.cursor = "" if complete else next_cursor
                    if complete:
                        result.completed_sweep = True
                        return result
        except _BudgetExhausted:
            return result
        except TimeoutError:
            result.stop_reason = "timeout"
            return result
