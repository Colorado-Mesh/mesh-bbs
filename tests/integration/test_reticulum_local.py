"""Real RNS/LXMF interoperability over temporary loopback-only networks, not RF."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

NODE_SCRIPT = r"""
import asyncio
import json
import sys
from pathlib import Path

import RNS
from mesh_bbs.adapters.reticulum import ReticulumAdapter, SYNC_PATH

config = json.loads(sys.argv[1])
root = Path(config["root"])

async def main():
    received = asyncio.Event()
    replies = []
    async def command(message):
        if config["role"] == "server":
            assert message.protocol == "reticulum"
            assert message.message_id
            return "reply:" + message.text
        replies.append(message.text)
        received.set()
        return ""

    def page(path, data):
        if path == "/page/index.mu":
            assert data == {}
            return b"#!c=0\n>Boards\ngeneral\nnews\n"
        assert path == "/page/board.mu"
        assert data == {"var_board": "general"}
        return ("#!c=0\n>General\n" + "Newsletter text.\n" * 400).encode()

    def sync(peer, data):
        assert peer == config["peer"]
        return {"received": data, "body": "é" * 32768}

    service = ReticulumAdapter(
        config_dir=root / "rns", state_dir=root / "state", name="Isolated BBS",
        command_handler=command, page_handler=page, sync_handler=sync,
        trusted_peers=[config["peer"]], delivery_timeout=15,
    )
    await service.start()
    try:
        (root / "ready.json").write_text(json.dumps(service.addresses))
        if config["role"] == "server":
            while not (root / "stop").exists():
                await asyncio.sleep(0.05)
            return

        result = await service.request_peer(config["peer"], {"cursor": 0}, timeout=15)
        assert result["received"] == {"cursor": 0}
        assert len(result["body"]) > RNS.Link.MDU
        assert len(result["body"].encode()) == 65536

        peer_addresses = json.loads(Path(config["peer_ready"]).read_text())
        async def destination(address, app, aspect):
            address = bytes.fromhex(address)
            if not RNS.Transport.has_path(address):
                RNS.Transport.request_path(address)
            async with asyncio.timeout(15):
                while not RNS.Transport.has_path(address):
                    await asyncio.sleep(0.05)
            identity = RNS.Identity.recall(address)
            assert identity.hash.hex() == config["peer"]
            return RNS.Destination(
                identity, RNS.Destination.OUT, RNS.Destination.SINGLE, app, aspect)

        pages = await destination(peer_addresses["nomadnet"], "nomadnetwork", "node")
        link = RNS.Link(pages)
        try:
            async with asyncio.timeout(15):
                while link.status != RNS.Link.ACTIVE:
                    await asyncio.sleep(0.05)
                loop = asyncio.get_running_loop()
                async def fetch_page(path, data):
                    response = loop.create_future()
                    def page_received(receipt):
                        loop.call_soon_threadsafe(response.set_result, receipt.response)
                    link.request(path, data=data, response_callback=page_received,
                                 max_response_size=262144)
                    return await response

                # Python NomadNet uses nil; Mesh Client's Rust LinkClient uses
                # an empty MessagePack binary for a page without variables.
                for data in (None, b"", {}):
                    assert await fetch_page("/page/index.mu", data) == (
                        b"#!c=0\n>Boards\ngeneral\nnews\n")
                    # Both clients send variables as a MessagePack map. Verify
                    # navigation still works after each empty-body request.
                    content = await fetch_page("/page/board.mu", {"var_board": "general"})
                    assert content.startswith(b"#!c=0\n>General\n")
                    assert len(content) > RNS.Link.MDU
        finally:
            link.teardown()

        # A valid encrypted link from an untrusted identity cannot use sync.
        sync_destination = await destination(peer_addresses["sync"], "meshbbs", "sync")
        untrusted_link = RNS.Link(sync_destination)
        try:
            async with asyncio.timeout(15):
                while untrusted_link.status != RNS.Link.ACTIVE:
                    await asyncio.sleep(0.05)
            untrusted_link.identify(RNS.Identity())
            accepted = asyncio.Event()
            loop = asyncio.get_running_loop()
            untrusted_link.request(
                SYNC_PATH, data={"cursor": 0}, timeout=0.5,
                response_callback=lambda _: loop.call_soon_threadsafe(accepted.set))
            try:
                await asyncio.wait_for(accepted.wait(), 1)
                raise AssertionError("untrusted identity was accepted")
            except TimeoutError:
                pass
        finally:
            untrusted_link.teardown()

        lxmf_destination = await destination(peer_addresses["lxmf"], "lxmf", "delivery")
        from types import SimpleNamespace
        await service._send_reply(SimpleNamespace(source=lxmf_destination), "boards")
        await asyncio.wait_for(received.wait(), 15)
        assert replies == ["reply:boards"]
        result = {"sync": True, "nomadnet": True, "lxmf": replies}
        (root / "result.json").write_text(json.dumps(result))
    finally:
        await service.stop()

asyncio.run(main())
"""


@pytest.mark.integration
def test_real_reticulum_lxmf_pages_and_trusted_sync(tmp_path: Path) -> None:
    rns = pytest.importorskip("RNS")
    pytest.importorskip("LXMF")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    roots = {role: tmp_path / role for role in ("server", "client")}
    identities = {role: rns.Identity() for role in roots}
    for role, root in roots.items():
        (root / "rns").mkdir(parents=True)
        (root / "state").mkdir()
        identities[role].to_file(str(root / "state" / "identity"))
        if role == "server":
            interface = f"""type = TCPServerInterface
    listen_ip = 127.0.0.1
    listen_port = {port}"""
        else:
            interface = f"""type = TCPClientInterface
    target_host = 127.0.0.1
    target_port = {port}"""
        (root / "rns" / "config").write_text(
            f"""[reticulum]
share_instance = No
enable_transport = Yes
[logging]
loglevel = 1
[interfaces]
  [[Isolated test]]
    enabled = Yes
    {interface}
"""
        )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    server_config = {
        "root": str(roots["server"]),
        "role": "server",
        "peer": identities["client"].hash.hex(),
    }
    with (tmp_path / "server.log").open("w+") as server_log:
        server = subprocess.Popen(
            [sys.executable, "-c", NODE_SCRIPT, json.dumps(server_config)],
            env=environment,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while not (roots["server"] / "ready.json").exists():
                if server.poll() is not None or time.monotonic() >= deadline:
                    server_log.seek(0)
                    pytest.fail(f"Isolated RNS server failed to start: {server_log.read()}")
                time.sleep(0.05)
            client_config = {
                "root": str(roots["client"]),
                "role": "client",
                "peer": identities["server"].hash.hex(),
                "peer_ready": str(roots["server"] / "ready.json"),
            }
            client = subprocess.run(
                [sys.executable, "-c", NODE_SCRIPT, json.dumps(client_config)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=75,
            )
            assert client.returncode == 0, client.stdout + client.stderr
            result = json.loads((roots["client"] / "result.json").read_text())
            assert result == {"sync": True, "nomadnet": True, "lxmf": ["reply:boards"]}
        finally:
            (roots["server"] / "stop").touch()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
