import json
from dataclasses import replace

import pytest

from mesh_bbs.cli import main, open_store
from mesh_bbs.config import HostConfig, ReticulumConfig, load_config, save_config
from mesh_bbs.events import BBSError, canonical
from mesh_bbs.federation import FederationService
from mesh_bbs.peer_setup import add_peer, export_card, read_card


def make_host(tmp_path, name, region="test"):
    config = HostConfig(
        name, region, tmp_path / name, reticulum=ReticulumConfig(True, tmp_path / "rns")
    )
    path = tmp_path / f"{name}.toml"
    save_config(config, path)
    return config, path, open_store(config)


def card_file(tmp_path, config, store):
    data = dict(
        version=1,
        name=config.name,
        region=config.region,
        origin=store.origin,
        public_key=store.public_key,
        reticulum_identity=store.origin[:32],
    )
    data["signature"] = store.key.sign(canonical(data)).hex()
    path = tmp_path / f"{config.name}.json"
    path.write_text(json.dumps(data))
    return path


async def test_two_operator_card_exchange_syncs_new_boards_and_dedupes(tmp_path):
    a, ap, source = make_host(tmp_path, "alpha")
    b, bp, target = make_host(tmp_path, "beta")
    try:
        ac, bc = card_file(tmp_path, a, source), card_file(tmp_path, b, target)
        for config, path, store, card in ((a, ap, source, bc), (b, bp, target, ac)):
            add_peer(config, path, path.read_bytes(), store, read_card(card), ("*",))
        source.close()
        target.close()
        a, b = load_config(ap), load_config(bp)
        source, target = open_store(a), open_store(b)
        assert a.peers[0].allowed_boards == ("*",) and not a.peers[0].can_moderate
        source.create_board("meshcore:alice", "hiking")
        post = source.publish("meshtastic:bob", "one", "hiking", "Trip", "Saturday")
        server = FederationService(source, {a.peers[0].reticulum_identity: frozenset({"*"})})
        client = FederationService(target, {b.peers[0].reticulum_identity: frozenset({"*"})})

        async def request(payload):
            return server.handle_request(a.peers[0].reticulum_identity, payload)

        result = await client.pull_peer(b.peers[0].reticulum_identity, request)
        assert result.accepted_events == 2
        assert target.get_post(post.post_id) == post
        assert (await client.pull_peer(b.peers[0].reticulum_identity, request)).accepted_events == 0
        assert add_peer(b, bp, bp.read_bytes(), target, read_card(ac), ("*",)) is None
    finally:
        source.close()
        target.close()


def test_tampering_wrong_region_self_and_unapproved_cli_do_not_add_peer(tmp_path, monkeypatch):
    a, ap, source = make_host(tmp_path, "alpha")
    b, bp, target = make_host(tmp_path, "beta", "other")
    try:
        ac = card_file(tmp_path, a, source)
        card = read_card(ac)
        with pytest.raises(BBSError, match="another region"):
            add_peer(b, bp, bp.read_bytes(), target, card, ("*",))
        with pytest.raises(BBSError, match="own peer"):
            add_peer(a, ap, ap.read_bytes(), source, card, ("*",))
        changed = dict(card, reticulum_identity="ff" * 16)
        ac.write_text(json.dumps(changed))
        with pytest.raises(BBSError, match="signature"):
            read_card(ac)
        ac.write_text(json.dumps(card))
        assert main(["--config", str(bp), "peer", "add", str(ac), "--trust-origin", "wrong"]) == 1
        same_region = replace(b, region=a.region)
        from mesh_bbs.config import update_config

        update_config(same_region, bp, bp.read_bytes())
        # Use a fresh database for a different region, never re-home an initialized database.
        target.close()
        same_region = replace(same_region, data_dir=tmp_path / "fresh")
        update_config(same_region, bp, bp.read_bytes())
        monkeypatch.setattr("builtins.input", lambda _: "no")
        assert main(["--config", str(bp), "peer", "add", str(ac)]) == 0
        assert not load_config(bp).peers
    finally:
        source.close()
        target.close()


def test_offline_export_preserves_actual_reticulum_identity_and_contains_no_secrets(tmp_path):
    rns = pytest.importorskip("RNS")
    config, _, store = make_host(tmp_path, "alpha")
    try:
        card = tmp_path / "peer.json"
        export_card(config, store, card)
        original = (config.data_dir / "reticulum/identity").read_bytes()
        value = read_card(card)
        identity = rns.Identity(create_keys=False)
        assert identity.load_private_key(original)
        assert value["reticulum_identity"] == identity.hash.hex()
        assert original.hex() not in card.read_text()
        assert store.key.private_bytes_raw().hex() not in card.read_text()
        export_card(config, store, tmp_path / "again.json")
        assert (config.data_dir / "reticulum/identity").read_bytes() == original
        assert rns.Reticulum.get_instance() is None
    finally:
        store.close()
