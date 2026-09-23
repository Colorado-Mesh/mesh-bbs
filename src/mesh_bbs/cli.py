"""Operator commands. Radio access starts only with the serve command."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import sqlite3
import sys
from pathlib import Path

from mesh_bbs import __version__
from mesh_bbs.commands import CommandService
from mesh_bbs.config import HostConfig, default_config_path, load_config
from mesh_bbs.events import MAX_BODY_BYTES
from mesh_bbs.store import Grant, Store


def open_store(config: HostConfig) -> Store:
    return Store(
        config.data_dir / "bbs.sqlite3",
        config.region,
        config.boards,
        grants={
            p.origin: Grant(p.public_key, frozenset(p.allowed_boards), p.can_moderate)
            for p in config.peers
        },
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Community bulletin boards across mesh networks")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--config", type=Path, help="Path to this host's TOML configuration")
    result.add_argument(
        "--region", default="colorado-mesh", help="Region whose saved config to use"
    )
    commands = result.add_subparsers(dest="action", required=True)
    commands.add_parser("setup", help="Choose Colorado Mesh or another community")
    commands.add_parser(
        "configure", help="Set up radios, Reticulum, feeds and web without editing TOML"
    )
    commands.add_parser("init", help="Create this host's database and print its public identity")
    commands.add_parser("doctor", help="Check the database and enabled dependencies")
    commands.add_parser("serve", help="Serve the web reader and explicitly enabled transports")
    command = commands.add_parser("command", help="Use the same text commands as radio users")
    command.add_argument("text")
    command.add_argument("--actor", default="local:operator")
    command.add_argument("--request-id")
    command.add_argument("--max-bytes", type=int, default=160)
    terminal = commands.add_parser("terminal", help="Serve one gateway-attested packet session")
    terminal.add_argument("--callsign", required=True)
    terminal.add_argument("--boards", required=True, help="Comma-separated public board allowlist")
    terminal.add_argument("--max-bytes", type=int, default=256)
    terminal.add_argument("--allow-posts", action="store_true")
    post = commands.add_parser("post", help="Publish a local text file")
    post.add_argument("board")
    post.add_argument("title")
    post.add_argument("--body-file", required=True, type=Path)
    post.add_argument(
        "--operation", required=True, help="Reuse this ID when retrying this publication"
    )
    post.add_argument("--parent", default="")
    edit = commands.add_parser("edit", help="Revise a post written by this host's local operator")
    edit.add_argument("post_id")
    edit.add_argument("--title")
    edit.add_argument("--body-file", required=True, type=Path)
    edit.add_argument("--operation", required=True)
    remove = commands.add_parser("remove", help="Record an operator removal of a post")
    remove.add_argument("post_id")
    remove.add_argument("--operation", required=True)
    backup = commands.add_parser("backup", help="Create a consistent private database backup")
    backup.add_argument("destination", type=Path)
    feeds = commands.add_parser("import-feeds", help="Fetch configured RSS/Atom sources once")
    feeds.add_argument("--force", action="store_true")
    web_access = commands.add_parser("web-access", help="Manage web contributor access keys")
    access_actions = web_access.add_subparsers(dest="access_action", required=True)
    create_access = access_actions.add_parser("create", help="Create a key, displayed only once")
    create_access.add_argument("name", help="Permanent contributor name; do not reuse for others")
    create_access.add_argument("--editor", action="store_true")
    revoke_access = access_actions.add_parser("revoke", help="Revoke a key and reserve its name")
    revoke_access.add_argument("name")
    access_actions.add_parser("list", help="List contributors without exposing keys")
    peers = commands.add_parser("peer", help="Pair explicitly trusted hosts in this community")
    peer_actions = peers.add_subparsers(dest="peer_action", required=True)
    export = peer_actions.add_parser("export", help="Export a signed public card, no private keys")
    export.add_argument("file", type=Path)
    add = peer_actions.add_parser("add", help="Verify and trust an operator's public peer card")
    add.add_argument("file", type=Path)
    add.add_argument(
        "--boards", default="*", help="Comma-separated boards; * includes future boards"
    )
    add.add_argument(
        "--trust-origin", help="Noninteractive approval: exact verified origin fingerprint"
    )
    peer_actions.add_parser("list", help="Show configured peers and last replication results")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.action == "setup":
            from mesh_bbs.setup import run_setup

            run_setup(args.config)
            return 0
        config_path = (args.config or default_config_path(args.region)).expanduser().absolute()
        if args.action == "configure":
            from mesh_bbs.configure import run_configure

            run_configure(config_path)
            return 0
        expected_config = config_path.read_bytes()
        config = load_config(config_path)
        if args.action == "serve":
            from mesh_bbs.runtime import serve

            asyncio.run(serve(config))
            return 0
        store = open_store(config)
        try:
            if args.action == "init":
                print(
                    json.dumps(
                        {
                            "name": config.name,
                            "region": config.region,
                            "origin": store.origin,
                            "public_key": store.public_key,
                            "database": str(config.data_dir / "bbs.sqlite3"),
                        },
                        indent=2,
                    )
                )
            elif args.action == "peer":
                from mesh_bbs.peer_setup import add_peer, card_fingerprint, export_card, read_card

                if args.peer_action == "export":
                    export_card(config, store, args.file)
                    print(f"Public card: {args.file}. Share it with your community's operator.")
                    print(f"Verify origin with them separately: {store.origin}")
                    print("Both hosts must import each other's cards and restart to sync.")
                elif args.peer_action == "add":
                    card = read_card(args.file)
                    if card["region"] != config.region:
                        raise ValueError("Peer belongs to another region")
                    print(f"Peer: {card['name']} | Region: {card['region']}")
                    print(f"Origin: {card['origin']} | Card fingerprint: {card_fingerprint(card)}")
                    boards = tuple(args.boards.split(","))
                    print(f"Trust scope: {', '.join(boards)}. No moderator privileges.")
                    if args.trust_origin is not None:
                        if args.trust_origin != card["origin"]:
                            raise ValueError("Approved fingerprint does not match this peer")
                    else:
                        from mesh_bbs.setup import _ask

                        if (
                            _ask(
                                "Verify the origin with its operator. Trust this peer? yes/no", "no"
                            )
                            != "yes"
                        ):
                            print("Not added.")
                            return 0
                    backup = add_peer(config, config_path, expected_config, store, card, boards)
                    print(f"Peer saved. Backup: {backup}" if backup else "Peer already configured.")
                    print("Exchange your card in return; restart both BBS services to apply trust.")
                else:
                    for peer in config.peers:
                        saved = store._meta("federation_status:" + (peer.reticulum_identity or ""))
                        print(
                            json.dumps(
                                {
                                    "origin": peer.origin,
                                    "reticulum_identity": peer.reticulum_identity,
                                    "boards": peer.allowed_boards,
                                    "last_sync": json.loads(saved) if saved else None,
                                }
                            )
                        )
                    if not config.peers:
                        print(
                            "No BBS peers configured. "
                            "Feed imports are not host-to-host replication."
                        )
            elif args.action == "command":
                response = CommandService(store, config.editors).handle(
                    args.actor,
                    args.text,
                    request_id=args.request_id,
                    max_bytes=args.max_bytes,
                )
                print(response)
                return int(response.startswith("Error:"))
            elif args.action == "web-access":
                from mesh_bbs.web_access import WebAccess

                access = WebAccess(store, config.editors)
                if args.access_action == "create":
                    token = access.create(args.name, editor=args.editor)
                    print(
                        f"Created web:{args.name}. Save this access key; it is shown only once. "
                        "Share it privately with that contributor.",
                        file=sys.stderr,
                    )
                    print(token)
                elif args.access_action == "revoke":
                    access.revoke(args.name)
                    print(f"Revoked web:{args.name}. The contributor name remains reserved.")
                else:
                    print(json.dumps(access.list_users(), indent=2))
            elif args.action == "terminal":
                from mesh_bbs.adapters.terminal import run_terminal

                run_terminal(
                    CommandService(store, config.editors),
                    sys.stdin.buffer,
                    sys.stdout.buffer,
                    callsign=args.callsign,
                    allowed_boards=tuple(board.strip() for board in args.boards.split(",")),
                    max_bytes=args.max_bytes,
                    allow_posts=args.allow_posts,
                )
            elif args.action in {"post", "edit"}:
                with args.body_file.open("rb") as body_file:
                    body = body_file.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise ValueError("Post exceeds the 64 KiB text limit")
                if args.action == "post":
                    post = store.publish(
                        "local:operator",
                        args.operation,
                        args.board,
                        args.title,
                        body.decode("utf-8"),
                        parent_id=args.parent,
                    )
                else:
                    original = store.get_post(args.post_id)
                    post = store.revise(
                        "local:operator",
                        args.operation,
                        original.post_id,
                        args.title if args.title is not None else original.title,
                        body.decode("utf-8"),
                    )
                print(f"Saved locally as {post.post_id}. Replication is pending.")
            elif args.action == "remove":
                post = store.remove(args.operation, args.post_id)
                print(f"Removed locally: {post.post_id}. Peers apply their moderator grants.")
            elif args.action == "backup":
                store.backup(args.destination)
                print(
                    f"Backup saved to {args.destination}. "
                    "Keep it private: it includes the host key."
                )
            elif args.action == "doctor":
                integrity = store.db.execute("PRAGMA quick_check").fetchone()[0]
                if integrity != "ok":
                    raise ValueError("SQLite integrity check failed")
                missing = [
                    name
                    for name, enabled in (
                        ("RNS", config.reticulum.enabled),
                        ("LXMF", config.reticulum.enabled),
                        ("meshcore", config.meshcore.enabled),
                        ("meshtastic", config.meshtastic.enabled),
                    )
                    if enabled and importlib.util.find_spec(name) is None
                ]
                if missing:
                    raise ValueError("Missing enabled protocol dependencies: " + ", ".join(missing))
                print(f"Database OK. Region: {config.region}. Enabled dependencies available.")
            elif args.action == "import-feeds":
                from mesh_bbs.newsletters import NewsletterImporter

                failed = False
                for feed in config.feeds:
                    outcome = NewsletterImporter(store, feed).poll_once(force=args.force)
                    print(f"{feed.source_id}: {outcome}")
                    failed |= outcome.status == "error"
                return int(failed)
        finally:
            store.close()
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
