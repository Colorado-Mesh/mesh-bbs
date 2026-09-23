"""Interactive setup for a local, radio-disabled community host."""

from __future__ import annotations

import re
import shlex
import socket
from pathlib import Path

from mesh_bbs.config import (
    FeedConfig,
    HostConfig,
    default_config_path,
    default_data_dir,
    save_config,
    validate_slug,
)
from mesh_bbs.markdown_source import COLORADO_NEWSLETTER_BASE


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        return input(f"{prompt}{suffix}: ").strip() or default
    except EOFError as error:
        raise ValueError(
            "setup needs an interactive terminal; run mesh-bbs setup in a terminal"
        ) from error


def run_setup(config_path: Path | None = None) -> Path:
    print("Mesh BBS setup")
    print("1. Colorado Mesh")
    print("2. Another mesh or region")
    while (choice := _ask("Choose a community", "1")) not in {"1", "2"}:
        print("Enter 1 or 2.")
    if choice == "1":
        region, community = "colorado-mesh", "Colorado Mesh"
    else:
        community = _ask("Community name")
        while not community:
            community = _ask("Enter a community name")
        suggested = re.sub(r"[^a-z0-9]+", "-", community.lower()).strip("-")[:64].rstrip("-")
        while True:
            region = _ask("Stable region ID (use the same ID as your peers)", suggested)
            try:
                validate_slug(region)
                break
            except ValueError as error:
                print(error)
    target = (config_path or default_config_path(region)).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError(
            f"configuration already exists: {target}; edit it or choose another path"
        )
    host_name = _ask("Name for this host", f"{community} / {socket.gethostname()}")
    data_dir = default_data_dir(region)
    feeds = (
        (
            FeedConfig(
                "colorado-mesh-blog",
                "https://blog.coloradomesh.org/feed.xml",
                newsletter_markdown_base_url=COLORADO_NEWSLETTER_BASE,
            ),
        )
        if choice == "1"
        else ()
    )
    config = HostConfig(name=host_name, region=region, data_dir=data_dir, feeds=feeds)
    save_config(config, target)
    print(f"Saved configuration: {target}")
    print(f"Local data: {data_dir}")
    print("The region becomes fixed when this host's database is initialized.")
    print("No public peers are configured. Radio connections are disabled.")
    if feeds:
        print("Colorado Mesh blog import is configured: https://blog.coloradomesh.org/feed.xml")
        print("Blog posts and published newsletter Markdown sync every 15 minutes.")
        print("The text stays readable over the web, NomadNet, and paged radio replies.")
    else:
        print("Add your community's RSS/Atom source to the config to import newsletters.")
    quoted = shlex.quote(str(target))
    print(f"Next: mesh-bbs --config {quoted} init")
    print(f"Create your editor key: mesh-bbs --config {quoted} web-access create alice --editor")
    print("Replace alice with your contributor name. Save the generated key privately.")
    print(f"Start the host: mesh-bbs --config {quoted} serve")
    print(f"Local web address: http://{config.bind_host}:{config.bind_port}")
    print(f"Sign in: http://{config.bind_host}:{config.bind_port}/connect")
    print("Enter the access key on the sign-in page, then choose a board and write a post.")
    print("Setup has not created an access key or started the host.")
    return target
