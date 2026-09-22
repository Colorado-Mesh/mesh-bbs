import asyncio
import stat
import subprocess
import sys

import pytest

from mesh_bbs.adapters.base import IncomingMessage, QueuedRadioAdapter
from mesh_bbs.adapters.meshcore import MeshCoreAdapter
from mesh_bbs.adapters.meshtastic import MeshtasticAdapter
from mesh_bbs.airtime import AirtimeLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay
        await asyncio.sleep(0)


def limiter(tmp_path, clock, **kwargs):
    options = dict(budget_seconds=6, packet_airtime_seconds=2, window_seconds=60)
    options.update(kwargs)
    return AirtimeLimiter(
        tmp_path / "airtime.sqlite3", "test", clock=clock, sleep=clock.sleep, **options
    )


def reserve(budget, attempts=1):
    token, delay = budget.try_reserve(attempts)
    assert token is not None and delay == 0
    return token


def test_retry_allowance_stays_reserved_through_slow_handler_and_delivery(tmp_path):
    clock = Clock()
    budget = limiter(tmp_path, clock)
    try:
        token = reserve(budget, 2)
        clock.now += 180
        assert budget.try_reserve(2) == (None, 1)
        budget.finish(token)
        assert budget.try_reserve(2) == (None, 60)
        clock.now += 59
        assert budget.try_reserve(2) == (None, 1)
        clock.now += 1
        reserve(budget, 2)
    finally:
        budget.close()


def test_each_reply_expires_relative_to_its_delivery_completion(tmp_path):
    clock = Clock()
    budget = limiter(tmp_path, clock)
    try:
        budget.finish(reserve(budget, 2))
        clock.now += 20
        budget.finish(reserve(budget))
        assert budget.try_reserve(2) == (None, 40)
        clock.now += 40
        budget.finish(reserve(budget, 2))
        assert budget.try_reserve(1) == (None, 20)
    finally:
        budget.close()


@pytest.mark.asyncio
async def test_async_wait_uses_fake_clock_and_never_overdraws(tmp_path):
    clock = Clock()
    budget = limiter(tmp_path, clock)
    try:
        budget.finish(await budget.acquire(3))
        budget.finish(await budget.acquire(3))
        assert clock.sleeps == [60]
        assert budget.try_reserve(1) == (None, 60)
    finally:
        budget.close()


def test_restarting_does_not_reset_used_budget(tmp_path):
    clock = Clock()
    budget = limiter(tmp_path, clock)
    budget.finish(reserve(budget, 3))
    budget.close()
    clock.now += 20
    reopened = limiter(tmp_path, clock)
    try:
        assert reopened.try_reserve(1) == (None, 40)
        clock.now += 40
        reserve(reopened, 3)
    finally:
        reopened.close()


def test_unfinished_delivery_gets_full_cooldown_after_restart(tmp_path):
    clock = Clock()
    budget = limiter(tmp_path, clock)
    reserve(budget, 3)
    budget.close()
    clock.now += 180
    reopened = limiter(tmp_path, clock)
    try:
        assert reopened.try_reserve(1) == (None, 60)
    finally:
        reopened.close()


def test_crashed_process_releases_lock_and_keeps_reservation(tmp_path):
    script = """
import os
import sys
from pathlib import Path
from mesh_bbs.airtime import AirtimeLimiter
budget = AirtimeLimiter(Path(sys.argv[1]), "test", budget_seconds=6,
                       packet_airtime_seconds=2, window_seconds=60, clock=lambda: 1000)
token, _ = budget.try_reserve(3)
assert token is not None
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "airtime.sqlite3")],
        check=True,
        timeout=10,
    )
    clock = Clock()
    clock.now += 180
    budget = limiter(tmp_path, clock)
    try:
        assert budget.try_reserve(1) == (None, 60)
    finally:
        budget.close()


def test_clock_rollback_cannot_replenish_budget(tmp_path):
    clock = Clock()
    budget = limiter(tmp_path, clock)
    budget.finish(reserve(budget, 3))
    budget.close()
    clock.now -= 100
    reopened = limiter(tmp_path, clock)
    try:
        assert reopened.try_reserve(1) == (None, 160)
        clock.now += 120
        assert reopened.try_reserve(1) == (None, 40)
        clock.now += 40
        reserve(reopened, 3)
    finally:
        reopened.close()


def test_changed_limits_wait_out_previous_and_new_window(tmp_path):
    clock = Clock()
    budget = limiter(tmp_path, clock)
    budget.finish(reserve(budget, 3))
    budget.close()
    clock.now += 60
    reopened = limiter(tmp_path, clock, budget_seconds=12, window_seconds=90)
    try:
        assert reopened.try_reserve(1) == (None, 90)
        clock.now += 90
        reserve(reopened, 6)
    finally:
        reopened.close()


def test_equivalent_integer_and_float_limits_do_not_add_cooldown(tmp_path):
    clock = Clock()
    budget = limiter(tmp_path, clock)
    budget.close()
    reopened = limiter(
        tmp_path, clock, budget_seconds=6.0, window_seconds=60.0, packet_airtime_seconds=2.0
    )
    try:
        reserve(reopened, 3)
    finally:
        reopened.close()


def test_state_has_one_owner_and_private_files(tmp_path):
    clock = Clock()
    budget = limiter(tmp_path, clock)
    try:
        with pytest.raises(RuntimeError, match="Another process"):
            limiter(tmp_path, clock)
        for name in ("airtime.sqlite3", "airtime.sqlite3.lock"):
            assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    finally:
        budget.close()
    budget.close()
    with pytest.raises(ValueError, match="different radio"):
        AirtimeLimiter(tmp_path / "airtime.sqlite3", "other")
    reopened = limiter(tmp_path, clock)
    reopened.close()


@pytest.mark.parametrize("name", ["airtime.sqlite3", "airtime.sqlite3.lock"])
def test_state_symlinks_are_rejected_without_touching_target(tmp_path, name):
    target = tmp_path / "target"
    target.write_text("keep")
    target.chmod(0o644)
    (tmp_path / name).symlink_to(target)
    with pytest.raises(ValueError, match="not a symlink"):
        limiter(tmp_path, Clock())
    assert target.read_text() == "keep"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    "changes",
    [
        {"budget_seconds": 0},
        {"budget_seconds": float("nan")},
        {"budget_seconds": float("inf")},
        {"budget_seconds": True},
        {"window_seconds": 0},
        {"window_seconds": -1},
        {"packet_airtime_seconds": 0},
        {"packet_airtime_seconds": float("inf")},
        {"budget_seconds": 61},
        {"packet_airtime_seconds": 7},
    ],
)
def test_invalid_limits_fail_before_state_is_created(tmp_path, changes):
    with pytest.raises(ValueError):
        limiter(tmp_path, Clock(), **changes)
    assert not (tmp_path / "airtime.sqlite3").exists()


@pytest.mark.parametrize("attempts", [0, -1, 11, 1.5, True, 4])
def test_invalid_or_unaffordable_attempts_are_rejected(tmp_path, attempts):
    budget = limiter(tmp_path, Clock())
    try:
        with pytest.raises(ValueError):
            budget.try_reserve(attempts)
        reserve(budget, 3)
    finally:
        budget.close()


def test_finished_reservation_cannot_finish_a_later_reply(tmp_path):
    clock = Clock()
    budget = limiter(tmp_path, clock)
    try:
        old = reserve(budget, 3)
        budget.finish(old)
        clock.now += 60
        current = reserve(budget, 3)
        assert current != old
        with pytest.raises(ValueError, match="Unknown or already finished"):
            budget.finish(old)
        budget.finish(current)
    finally:
        budget.close()


class MemoryRadio(QueuedRadioAdapter):
    def __init__(self, handler, **kwargs):
        super().__init__(handler, max_bytes=160, min_interval=0, **kwargs)
        self.sent = []

    async def send_reply(self, message, text):
        self.sent.append((message.sender, text))


@pytest.mark.asyncio
async def test_budget_backpressures_commands_and_failed_command_consumes_reservation(tmp_path):
    clock = Clock()
    calls = []

    async def handler(message):
        calls.append(clock())
        if len(calls) == 1:
            raise RuntimeError("command failed")
        return "posted"

    budget = limiter(tmp_path, clock)
    radio = MemoryRadio(handler, airtime_limiter=budget, transmission_attempts=3)
    radio._start_worker()
    try:
        radio.enqueue(IncomingMessage("test", "alice", "publish first"))
        radio.enqueue(IncomingMessage("test", "bob", "publish second"))
        await radio.drain()
        assert calls == [1000, 1060]
        assert radio.failed == 1 and radio.acknowledged == 1
        assert radio.sent == [("bob", "posted")]
        assert budget.try_reserve(1) == (None, 60)
    finally:
        await radio._stop_worker()
        budget.close()


@pytest.mark.asyncio
async def test_shutdown_during_budget_wait_does_not_execute_command(tmp_path):
    clock = Clock()
    waiting = asyncio.Event()
    calls = []

    async def sleep(delay):
        waiting.set()
        await asyncio.Future()

    async def handler(message):
        calls.append(message)
        return "posted"

    budget = limiter(tmp_path, clock)
    budget._sleep = sleep
    budget.finish(reserve(budget, 3))
    radio = MemoryRadio(handler, airtime_limiter=budget)
    radio._start_worker()
    try:
        assert radio.enqueue(IncomingMessage("test", "alice", "publish"))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        await radio._stop_worker()
        assert not calls and not radio.sent
        assert budget.try_reserve(1) == (None, 60)
        assert not radio._sender_pending
    finally:
        await radio._stop_worker()
        budget.close()


@pytest.mark.asyncio
async def test_cancelling_a_delivery_keeps_reserved_retry_cost(tmp_path):
    clock = Clock()
    entered = asyncio.Event()

    async def handler(message):
        entered.set()
        await asyncio.Future()

    budget = limiter(tmp_path, clock)
    radio = MemoryRadio(handler, airtime_limiter=budget, transmission_attempts=3)
    radio._start_worker()
    try:
        radio.enqueue(IncomingMessage("test", "alice", "publish"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        clock.now += 120
        await radio._stop_worker()
        assert budget.try_reserve(1) == (None, 60)
    finally:
        await radio._stop_worker()
        budget.close()


@pytest.mark.asyncio
async def test_one_sender_cannot_fill_the_entire_request_queue():
    async def handler(message):
        return "ok"

    radio = MemoryRadio(handler, queue_size=8)
    radio._start_worker()
    try:
        alice = IncomingMessage("test", "alice", "help")
        bob = IncomingMessage("test", "bob", "help")
        assert all(radio.enqueue(alice) for _ in range(4))
        assert not any(radio.enqueue(alice) for _ in range(20))
        assert radio.enqueue(bob)
        await radio.drain()
        assert radio.rate_limited == 20
        assert radio.sent[-1] == ("bob", "ok")
    finally:
        await radio._stop_worker()


@pytest.mark.asyncio
async def test_sender_rate_limit_applies_after_previous_requests_finish():
    clock = Clock()

    async def handler(message):
        return "ok"

    radio = MemoryRadio(handler, queue_size=1, request_clock=clock)
    radio._start_worker()
    try:
        message = IncomingMessage("test", "alice", "help")
        for _ in range(12):
            assert radio.enqueue(message)
            await radio.drain()
        assert not radio.enqueue(message)
        assert radio.enqueue(IncomingMessage("test", "bob", "help"))
        await radio.drain()
        clock.now += 60
        assert radio.enqueue(message)
        await radio.drain()
        assert len(radio.sent) == 14
    finally:
        await radio._stop_worker()


@pytest.mark.asyncio
async def test_sender_tracking_cannot_grow_without_bound():
    clock = Clock()

    async def handler(message):
        return ""

    radio = MemoryRadio(handler, request_clock=clock)
    radio._start_worker()
    try:
        for index in range(1024):
            assert radio.enqueue(IncomingMessage("test", str(index), "help"))
            await radio.drain()
        newcomer = IncomingMessage("test", "new", "help")
        assert not radio.enqueue(newcomer)
        assert len(radio._sender_requests) == 1024
        clock.now += 60
        assert radio.enqueue(newcomer)
        await radio.drain()
        assert len(radio._sender_requests) == 1
    finally:
        await radio._stop_worker()


def test_protocol_adapters_reserve_their_entire_retry_allowance(tmp_path):
    async def handler(message):
        return "ok"

    budget = limiter(tmp_path, Clock(), budget_seconds=20)
    try:
        meshcore = MeshCoreAdapter(handler, serial_port="fake", airtime_limiter=budget)
        meshtastic = MeshtasticAdapter(handler, serial_port="fake", airtime_limiter=budget)
        assert meshcore.transmission_attempts == 2
        assert meshtastic.transmission_attempts == 4
    finally:
        budget.close()
    insufficient = limiter(tmp_path / "insufficient", Clock(), budget_seconds=2)
    try:
        with pytest.raises(ValueError, match="all transmission attempts"):
            MeshCoreAdapter(
                handler,
                serial_port="fake",
                airtime_limiter=insufficient,
                max_attempts=3,
            )
    finally:
        insufficient.close()
