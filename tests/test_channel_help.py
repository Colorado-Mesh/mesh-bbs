from mesh_bbs.adapters.meshcore import channel_help_fingerprint
from mesh_bbs.channel_help import HELP_INTERVAL, HELP_REPLAY_WINDOW, ChannelHelpGate
from mesh_bbs.store import Store


def payload(**changes):
    return {
        "type": "CHAN",
        "txt_type": 0,
        "channel_idx": 1,
        "sender_timestamp": 1790186400,
        "text": "Reader: help",
        **changes,
    }


def test_only_exact_help_on_the_selected_channel_is_accepted():
    original = channel_help_fingerprint(payload(), 1)
    assert original
    assert channel_help_fingerprint(payload(), 1) == original
    for text in ("Reader: HELP", "Reader:  help  ", "help"):
        assert channel_help_fingerprint(payload(text=text), 1)
    for changes in (
        {"text": "Reader: can you help"},
        {"text": "Reader: help\x00"},
        {"text": "x" * 161},
        {"text": "Reader: post general Example | body"},
        {"text": "BBS: DM this node: boards, threads BOARD, read ID, more, help"},
        {"type": "PRIV"},
        {"txt_type": 2},
        {"channel_idx": 2},
        {"channel_idx": True},
        {"sender_timestamp": None},
        {"sender_timestamp": True},
        {"sender_timestamp": -1},
        {"sender_timestamp": 0x100000000},
    ):
        assert channel_help_fingerprint(payload(**changes), 1) is None


def test_help_deduplication_cooldown_and_bounded_storage_survive_restart(tmp_path):
    path = tmp_path / "bbs.db"
    store = Store(path, "test")
    gate = ChannelHelpGate(store, "meshcore")
    assert gate.claim("one", 1000)
    assert not gate.claim("two", 1000 + HELP_INTERVAL - 1)
    store.close()
    store = Store(path, "test")
    gate = ChannelHelpGate(store, "meshcore")
    assert not gate.available("one", 1000 + HELP_INTERVAL)
    assert not gate.claim("one", 1000 + HELP_INTERVAL)
    assert gate.claim("two", 1000 + HELP_INTERVAL)
    assert not gate.claim("backwards-clock", 999)
    # The five-minute admission rate plus expiry bounds storage to one day's attempts.
    for i in range(1000):
        assert gate.claim(str(i), 1000 + (i + 2) * HELP_INTERVAL)
    count = store.db.execute("SELECT count(*) FROM channel_help_attempts").fetchone()[0]
    assert count <= HELP_REPLAY_WINDOW // HELP_INTERVAL + 1
    store.close()
