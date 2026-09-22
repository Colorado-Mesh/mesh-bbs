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
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.action == "setup":
            from mesh_bbs.setup import run_setup

            run_setup(args.config)
            return 0
        config = load_config(args.config or default_config_path(args.region))
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
            elif args.action == "command":
                response = CommandService(store, config.editors).handle(
                    args.actor,
                    args.text,
                    request_id=args.request_id,
                    max_bytes=args.max_bytes,
                )
                print(response)
                return int(response.startswith("Error:"))
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
