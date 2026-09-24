"""Three real BBS hosts over isolated Reticulum links, including an LXMF reader."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from mesh_bbs.cli import open_store
from mesh_bbs.commands import CommandService
from mesh_bbs.config import FeedConfig, HostConfig, ReticulumConfig, load_config, save_config
from mesh_bbs.federation import FederationService
from mesh_bbs.feeds import FetchResult
from mesh_bbs.newsletters import NewsletterImporter
from mesh_bbs.peer_setup import add_peer, export_card, read_card
from mesh_bbs.views import Views

BOARDS = frozenset({"general", "news"})
LONG_BODY = "é" * 32768
FEED = b"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Colorado Mesh</title><item><guid isPermaLink="false">issue-9</guid>
<title>September newsletter</title><pubDate>Tue, 22 Sep 2026 12:00:00 GMT</pubDate>
<content:encoded>Bring a radio to Saturday's meetup.</content:encoded>
</item></channel></rss>"""


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


async def run_node(config: dict[str, Any]) -> None:
    import LXMF
    import RNS

    from mesh_bbs.adapters.reticulum import ReticulumAdapter

    root = Path(config["root"])
    host = load_config(root / "host.toml")
    store = open_store(host)
    commands = CommandService(store)
    federation = FederationService(
        store, {peer.reticulum_identity: frozenset(peer.allowed_boards) for peer in host.peers}
    )
    views = Views(store, "Colorado Mesh integration host")

    async def command(message: Any) -> str:
        assert message.protocol == "reticulum" and message.message_id
        return commands.handle(
            "reticulum:" + message.sender,
            message.text,
            request_id=message.message_id,
            max_bytes=4096,
        )

    adapter = ReticulumAdapter(
        config_dir=root / "rns",
        state_dir=root / "reticulum",
        name="Colorado Mesh integration host",
        command_handler=command,
        page_handler=views.page,
        sync_handler=federation.handle_request,
        trusted_peers=[peer["identity"] for peer in config["peers"].values()],
        delivery_timeout=8,
    )
    replies: asyncio.Queue[Any] = asyncio.Queue()
    reader = None
    reader_router = None
    started = False
    loop = asyncio.get_running_loop()

    async def destination(address: str, app: str, aspect: str) -> Any:
        address_bytes = bytes.fromhex(address)
        if not RNS.Transport.has_path(address_bytes):
            RNS.Transport.request_path(address_bytes)
        async with asyncio.timeout(8):
            while not RNS.Transport.has_path(address_bytes):
                await asyncio.sleep(0.01)
        identity = RNS.Identity.recall(address_bytes)
        assert identity is not None
        result = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, app, aspect)
        assert result.hash == address_bytes
        return result

    async def execute(request: dict[str, Any]) -> Any:
        nonlocal reader, reader_router, started
        action = request["action"]
        if action == "start":
            await adapter.start()
            started = True
            return {**adapter.addresses, "origin": store.origin, "key": store.public_key}
        if action == "seed":
            if config["name"] == "alpha":
                parent = store.publish("alice", "plans", "general", "Weekend plans", "Saturday")
                reply = store.publish(
                    "bob",
                    "reply",
                    "general",
                    "Re: Weekend plans",
                    "Count me in",
                    parent_id=parent.post_id,
                )
                return {"parent": parent.post_id, "reply": reply.post_id}
            if config["name"] == "beta":
                post = store.import_article("editor", "long", "news", "Long issue", LONG_BODY)
            else:
                post = store.publish("charlie", "offline", "general", "Offline", "Posted offline")
            feed = FeedConfig("colorado-newsletter", "https://example.org/feed", poll_seconds=60)
            importer = NewsletterImporter(
                store, feed, fetcher=lambda *_: FetchResult(200, FEED, '"issue-9"', None)
            )
            assert importer.poll_once(now=0).changed == 1
            assert importer.poll_once(now=60).changed == 0
            newsletter = next(p for p in store.list_posts("news") if p.source_id)
            return {"post": post.post_id, "newsletter": newsletter.post_id}
        if action == "pull":
            peer = config["peers"][request["peer"]]["identity"]
            result = await federation.pull_peer(
                peer,
                partial(adapter.request_peer, peer, timeout=8),
                timeout=8,
                max_duration=10,
                max_requests=request.get("max_requests", 32),
            )
            return asdict(result)
        if action == "snapshot":
            return {
                "events": store.inventory(BOARDS),
                "posts": [
                    json.loads(row[0])
                    for row in store.db.execute("SELECT payload FROM posts ORDER BY post_id")
                ],
                "origin": store.origin,
                "key": store.public_key,
            }
        if action == "reader":
            if reader_router is None:
                # A separate LXMF endpoint acts as a reader. All three BBS
                # delivery endpoints keep their actual command handlers.
                identity = RNS.Identity()
                reader_router = LXMF.LXMRouter(
                    identity=identity, storagepath=str(root / "reader"), autopeer=False
                )
                reader = reader_router.register_delivery_identity(identity)
                reader_router.register_delivery_callback(
                    lambda message: loop.call_soon_threadsafe(replies.put_nowait, message)
                )
                reader_router.announce(reader.hash)
            return reader.hash.hex()
        if action == "discover_reader":
            return (await destination(request["address"], "lxmf", "delivery")).hash.hex()
        if action == "message":
            assert reader_router is not None
            peer = await destination(request["address"], "lxmf", "delivery")
            message = LXMF.LXMessage(
                peer, reader, request["text"], desired_method=LXMF.LXMessage.DIRECT
            )
            await asyncio.to_thread(reader_router.handle_outbound, message)
            incoming = await asyncio.wait_for(replies.get(), 8)
            assert incoming.signature_validated
            assert incoming.source_hash == peer.hash
            return incoming.content.decode()
        if action == "page":
            peer = await destination(request["address"], "nomadnetwork", "node")
            link = RNS.Link(peer)
            try:
                async with asyncio.timeout(8):
                    while link.status != RNS.Link.ACTIVE:
                        assert link.status != RNS.Link.CLOSED
                        await asyncio.sleep(0.01)
                    response = loop.create_future()

                    def received(receipt: Any) -> None:
                        if not response.done():
                            response.set_result(receipt.response)

                    link.request(
                        request["path"],
                        data=request["variables"],
                        response_callback=lambda receipt: loop.call_soon_threadsafe(
                            received, receipt
                        ),
                        max_response_size=262144,
                    )
                    return (await response).decode()
            finally:
                link.teardown()
        raise AssertionError(f"Unknown test command: {action}")

    write_json(root / "ready.json", {"origin": store.origin, "key": store.public_key})
    try:
        while not (root / "stop").exists():
            mailbox = root / "request.json"
            if mailbox.exists():
                request = json.loads(mailbox.read_text())
                mailbox.unlink()
                write_json(root / "response.json", await execute(request))
            await asyncio.sleep(0.01)
    except BaseException:  # unslop-ignore: Report failures before RNS exits the process.
        traceback.print_exc()
        raise
    finally:
        if reader_router is not None:
            await asyncio.to_thread(reader_router.exit_handler)
        if started:
            await adapter.stop()
        store.close()


class Node:
    def __init__(self, root: Path, config: dict[str, Any]) -> None:
        self.root, self.config = root, config
        self.log = root / "process.log"
        self.process: subprocess.Popen | None = None

    def start(self) -> dict[str, Any]:
        for name in ("stop", "ready.json", "response.json", "request.json"):
            (self.root / name).unlink(missing_ok=True)
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
        with self.log.open("a") as output:
            self.process = subprocess.Popen(
                [sys.executable, "-u", __file__, json.dumps(self.config)],
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        return self.wait("ready.json")

    def wait(self, filename: str) -> Any:
        deadline = time.monotonic() + 12
        while not (self.root / filename).exists():
            assert self.process is not None
            if self.process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail(
                    f"{self.config['name']} waiting for {filename}:\n{self.log.read_text()}"
                )
            time.sleep(0.01)
        return json.loads((self.root / filename).read_text())

    def call(self, action: str, **values: Any) -> Any:
        (self.root / "response.json").unlink(missing_ok=True)
        write_json(self.root / "request.json", {"action": action, **values})
        return self.wait("response.json")

    def stop(self) -> None:
        if self.process is not None:
            (self.root / "stop").touch()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


@pytest.mark.integration
def test_three_host_bbs_over_reticulum(tmp_path: Path) -> None:
    pytest.importorskip("RNS")
    pytest.importorskip("LXMF")
    peers = {}
    roots = {name: tmp_path / name for name in ("alpha", "beta", "gamma")}
    ports = {}
    with ExitStack() as reservations:
        for name in roots:
            reservation = reservations.enter_context(socket.socket())
            reservation.bind(("127.0.0.1", 0))
            ports[name] = reservation.getsockname()[1]
    for name, root in roots.items():
        (root / "rns").mkdir(parents=True)
        host = HostConfig(
            name, "colorado-mesh", root, reticulum=ReticulumConfig(True, root / "rns")
        )
        save_config(host, root / "host.toml")
        store = open_store(host)
        export_card(host, store, root / "public-peer.json")
        card = read_card(root / "public-peer.json")
        peers[name] = {
            "identity": card["reticulum_identity"],
            "origin": card["origin"],
            "key": card["public_key"],
        }
        store.close()
        interfaces = (
            "  [[Loopback listener]]\n    enabled = Yes\n    type = TCPServerInterface\n"
            f"    listen_ip = 127.0.0.1\n    listen_port = {ports[name]}\n"
        )
        for peer_name in peers:
            if peer_name != name:
                interfaces += (
                    f"  [[Loopback to {peer_name}]]\n    enabled = Yes\n"
                    "    type = TCPClientInterface\n    target_host = 127.0.0.1\n"
                    f"    target_port = {ports[peer_name]}\n"
                )
        (root / "rns" / "config").write_text(
            "[reticulum]\nshare_instance = No\nenable_transport = Yes\n"
            f"[logging]\nloglevel = 1\n[interfaces]\n{interfaces}"
        )
    # Operators exchange signed cards and explicitly authorize the same community.
    for name, root in roots.items():
        path = root / "host.toml"
        for other_name, other_root in roots.items():
            if other_name == name:
                continue
            host = load_config(path)
            store = open_store(host)
            try:
                add_peer(
                    host,
                    path,
                    path.read_bytes(),
                    store,
                    read_card(other_root / "public-peer.json"),
                    ("*",),
                )
            finally:
                store.close()
    nodes = {
        name: Node(
            root,
            {
                "root": str(root),
                "name": name,
                "peers": {key: value for key, value in peers.items() if key != name},
            },
        )
        for name, root in roots.items()
    }
    alpha, beta, gamma = nodes.values()
    try:
        for node in nodes.values():
            node.start()
        seeds = {name: node.call("seed") for name, node in nodes.items()}
        before = [node.call("snapshot") for node in nodes.values()]
        assert [len(snapshot["events"]) for snapshot in before] == [2, 2, 2]
        assert seeds["beta"]["newsletter"] == seeds["gamma"]["newsletter"]
        addresses = {name: node.call("start") for name, node in nodes.items()}

        # An interrupted pull must retry the missing events instead of advancing
        # past their inventory. No posts cross the process boundary except RNS.
        interrupted = beta.call("pull", peer="alpha", max_requests=1)
        assert interrupted["stop_reason"] == "request_budget"
        assert interrupted["accepted_events"] == 0
        for receiver, peer in ((beta, "alpha"), (gamma, "beta"), (alpha, "gamma"), (beta, "gamma")):
            result = receiver.call("pull", peer=peer)
            assert result["completed_sweep"], (receiver.config["name"], peer, result)
        snapshots = [node.call("snapshot") for node in nodes.values()]
        assert snapshots[0]["events"] == snapshots[1]["events"] == snapshots[2]["events"]
        assert len(snapshots[0]["events"]) == 6
        assert snapshots[0]["posts"] == snapshots[1]["posts"] == snapshots[2]["posts"]
        posts = {post["post_id"]: post for post in snapshots[0]["posts"]}
        assert len(posts) == 5
        assert posts[seeds["beta"]["post"]]["body"] == LONG_BODY
        assert posts[seeds["alpha"]["reply"]]["parent_id"] == seeds["alpha"]["parent"]
        assert posts[seeds["alpha"]["reply"]]["thread_id"] == seeds["alpha"]["parent"]
        assert "Saturday's meetup" in posts[seeds["beta"]["newsletter"]]["body"]

        reader_address = gamma.call("reader")
        assert beta.call("discover_reader", address=reader_address) == reader_address

        def dm(text: str) -> str:
            return gamma.call("message", address=addresses["beta"]["lxmf"], text=text)

        created = dm("@draft-1 new general Radio meetup")
        assert created.startswith("Draft "), created
        draft = created.split()[1].rstrip(".")
        assert dm(f"@part-1 add {draft} 1 Signed LXMF post").startswith("Saved part 1.")
        saved = dm(f"@publish-1 publish {draft}")
        assert saved.startswith("Saved locally as "), saved
        assert dm("resend") == saved
        assert dm(f"@publish-1 publish {draft}") == saved
        post_id = saved.split()[3].rstrip(".")
        assert dm(f"read {post_id}") == f"{post_id} Radio meetup\nSigned LXMF post"

        for receiver, peer in ((alpha, "beta"), (gamma, "alpha")):
            assert receiver.call("pull", peer=peer)["completed_sweep"]
        final = [node.call("snapshot") for node in nodes.values()]
        assert final[0]["events"] == final[1]["events"] == final[2]["events"]
        assert len(final[0]["events"]) == 7
        assert final[0]["posts"] == final[1]["posts"] == final[2]["posts"]
        assert len(final[0]["posts"]) == 6
        # Replies visible in NomadNet's thread view are also reachable by DM,
        # including from old notices that used a hexadecimal read ID.
        assert "Saturday" in dm("read " + seeds["alpha"]["parent"])
        assert "Original: Weekend plans" in dm("replies")
        assert "Count me in" in dm("2")
        assert "Re: Weekend plans" in dm("back")
        # The same guided conversation creates a new board through real signed LXMF.
        menu = dm("help")
        assert "3 Write/resume" in menu
        assert dm("resend") == menu
        assert "general" in dm("3")
        assert "board name" in dm("2")
        assert "title" in dm("hiking")
        assert "text" in dm("Trail report")
        assert "saved" in dm("#!micron")
        assert "saved" in dm(">Conditions\n`!Trail is clear.`!")
        preview = dm("done")
        assert "publish |" in preview and "Trail is clear." in preview and "`!" not in preview
        assert "Posted to hiking" in dm("publish")
        for receiver, peer in ((alpha, "beta"), (gamma, "alpha")):
            assert receiver.call("pull", peer=peer)["completed_sweep"]
        shared = [node.call("snapshot")["posts"] for node in nodes.values()]
        assert shared[0] == shared[1] == shared[2]
        formatted = next(p for p in shared[0] if p["board"] == "hiking")
        assert formatted["body"] == "#!micron\n>Conditions\n`!Trail is clear.`!"
        reply = dm("read " + formatted["post_id"])
        assert "Trail is clear." in reply and "`!" not in reply
        formatted_page = gamma.call(
            "page",
            address=addresses["alpha"]["nomadnet"],
            path="/page/post.mu",
            variables={"var_id": formatted["post_id"]},
        )
        assert "`!Trail is clear." in formatted_page
        assert "<``\n`=\n Post ID:" in formatted_page
        final = [node.call("snapshot") for node in nodes.values()]
        beta.stop()
        assert beta.process is not None and beta.process.returncode == 0, beta.log.read_text()
        assert beta.start() == {"origin": peers["beta"]["origin"], "key": peers["beta"]["key"]}
        assert beta.call("start") == addresses["beta"]
        assert beta.call("snapshot") == final[1]
        repeated = beta.call("pull", peer="gamma")
        assert repeated["completed_sweep"] and repeated["accepted_events"] == 0

        page = gamma.call(
            "page",
            address=addresses["alpha"]["nomadnet"],
            path="/page/post.mu",
            variables={"var_id": seeds["beta"]["post"]},
        )
        assert LONG_BODY in page
        assert "Long issue" in page and seeds["beta"]["post"] in page
        thread = gamma.call(
            "page",
            address=addresses["alpha"]["nomadnet"],
            path="/page/thread.mu",
            variables={"var_id": seeds["alpha"]["parent"]},
        )
        assert "Weekend plans" in thread and "Count me in" in thread
        assert seeds["alpha"]["reply"] in thread

    finally:
        for node in nodes.values():
            node.stop()


if __name__ == "__main__":
    asyncio.run(run_node(json.loads(sys.argv[1])))
