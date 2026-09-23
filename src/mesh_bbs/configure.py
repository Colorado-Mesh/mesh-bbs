"""Guided connection setup. Saving never opens a radio or starts a service."""

from __future__ import annotations

import glob
import os
import re
import shlex
from dataclasses import replace
from pathlib import Path

from mesh_bbs.config import (
    FeedConfig,
    HostConfig,
    RadioConfig,
    ReticulumConfig,
    load_config,
    update_config,
)
from mesh_bbs.setup import _ask
from mesh_bbs.store import Store


def choice(prompt: str, options: set[str], default: str) -> str:
    while (answer := _ask(prompt, default).lower()) not in options:
        print("Choose " + ", ".join(sorted(options)))
    return answer


def number(prompt: str, default: int, low: int, high: int) -> int:
    while True:
        answer = _ask(prompt, str(default))
        try:
            value = int(answer)
            if low <= value <= high:
                return value
        except ValueError:
            pass
        print(f"Enter a number from {low} to {high}.")


def radio_config(config: HostConfig, protocol: str) -> RadioConfig:
    radio: RadioConfig = getattr(config, protocol)
    print(f"{protocol}: use a companion/standard client, not a room server or repeater.")
    print("0 Disabled; 1 USB serial; 2 TCP companion")
    default = "1" if radio.enabled and radio.serial_port else "2" if radio.enabled else "0"
    mode = choice("Connection", {"0", "1", "2"}, default)
    if mode == "0":
        return replace(radio, enabled=False)
    if mode == "1":
        ports = sorted(set(glob.glob("/dev/serial/by-id/*") + glob.glob("/dev/cu.*")))
        if ports:
            print("Available serial paths:\n" + "\n".join(ports))
        port = _ask("Companion serial path", radio.serial_port or "")
        radio = replace(radio, enabled=True, serial_port=port, tcp_host=None, tcp_port=None)
    else:
        host = _ask("Companion TCP host/IP", radio.tcp_host or "")
        port_number = number(
            "TCP port", radio.tcp_port or (4000 if protocol == "meshcore" else 4403), 1, 65535
        )
        radio = replace(radio, enabled=True, serial_port=None, tcp_host=host, tcp_port=port_number)
    print("This estimates reply airtime; it does not change the radio's RF settings.")
    if protocol == "meshcore":
        print("1 SF7 / 62.5 kHz / CR5 (one-second packet allowance); 2 Keep/custom estimate")
        preset = choice("Existing radio modem profile", {"1", "2"}, "2")
        estimate = (
            1.0
            if preset == "1"
            else float(
                _ask("Upper seconds per transmitted packet", str(radio.packet_airtime_seconds))
            )
        )
    else:
        print("1 LongFast (250 kHz); 2 MediumFast (250 kHz); 0 Other/keep estimate")
        preset = choice("Existing Meshtastic modem preset", {"0", "1", "2"}, "0")
        estimate = {"1": 3.0, "2": 1.0}.get(preset, 0.0)
        if not estimate:
            estimate = float(
                _ask("Upper seconds per transmitted packet", str(radio.packet_airtime_seconds))
            )
    radio = replace(radio, packet_airtime_seconds=estimate)
    attempts = 2 if protocol == "meshcore" else radio.firmware_max_attempts
    print(
        f"Allowance: about {int(radio.airtime_budget_seconds / (estimate * attempts))} replies "
        f"per {int(radio.airtime_window_seconds / 60)} minutes, before channel notices."
    )
    print("Only ONE host per local mesh should announce on the public BBS channel.")
    print(
        "0 No channel notices/help; 1 This host is the designated announcer; 2 Keep current owner"
    )
    announce = choice(
        "Channel announcements and help", {"0", "1", "2"}, "2" if radio.announcement_owner else "0"
    )
    if announce == "0":
        return replace(
            radio,
            announcement_owner=None,
            announcement_channel=None,
            announcement_channel_name=None,
        )
    if announce == "2":
        return radio
    store = Store(config.data_dir / "bbs.sqlite3", config.region, config.boards)
    try:
        owner = store.origin
    finally:
        store.close()
    channel = number(
        "Existing channel slot",
        radio.announcement_channel if radio.announcement_channel is not None else 1,
        0,
        39 if protocol == "meshcore" else 7,
    )
    name = _ask(
        "Existing channel name",
        radio.announcement_channel_name or ("#bbs" if protocol == "meshcore" else "BBS"),
    )
    print(
        "Configure matching channel keys in the radio app; "
        "Meshtastic readers need its channel URL/QR."
    )
    return replace(
        radio,
        announcement_owner=owner,
        announcement_channel=channel,
        announcement_channel_name=name,
    )


def reticulum_config(config: HostConfig) -> tuple[ReticulumConfig, tuple[Path, str] | None]:
    print("0 Disabled; 1 Existing Reticulum config directory; 2 Create a TCP gateway connection")
    mode = choice("Reticulum", {"0", "1", "2"}, "1" if config.reticulum.enabled else "0")
    if mode == "0":
        return replace(config.reticulum, enabled=False), None
    if mode == "1":
        directory = (
            Path(_ask("Reticulum directory", str(config.reticulum.config_dir or "")))
            .expanduser()
            .absolute()
        )
        if not (directory / "config").is_file():
            raise ValueError("That directory must contain an existing Reticulum config file")
        return ReticulumConfig(True, directory), None
    gateway = _ask("Reticulum TCP gateway supplied by your mesh", "rns.ratspeak.org")
    if not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", gateway):
        raise ValueError("Use a gateway hostname or IP, without a URL or spaces")
    port = number("Gateway TCP port", 4242, 1, 65535)
    directory = config.data_dir / "reticulum-config"
    target = directory / "config"
    if target.exists() or target.is_symlink():
        raise ValueError(f"{target} exists; choose the existing-config option to preserve it")
    text = (
        "[reticulum]\n  enable_transport = No\n  share_instance = No\n\n[interfaces]\n"
        "  [[BBS gateway]]\n    type = TCPClientInterface\n    enabled = Yes\n"
        f"    target_host = {gateway}\n    target_port = {port}\n"
    )
    return ReticulumConfig(True, directory), (target, text)


def run_configure(path: Path) -> None:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError("Configuration must be a regular file, not a symlink")
    expected = path.read_bytes()
    config = original = load_config(path)
    extra: tuple[Path, str] | None = None
    print("Choose what to configure. Nothing connects until you start or restart serve.")
    while True:
        print(
            "1 MeshCore; 2 Meshtastic; 3 Reticulum/NomadNet; "
            "4 Automatic news feed; 5 Web; 0 Save and finish"
        )
        option = choice("Setting", {"0", "1", "2", "3", "4", "5"}, "0")
        if option == "0":
            break
        try:
            if option in {"1", "2"}:
                protocol = "meshcore" if option == "1" else "meshtastic"
                radio = radio_config(config, protocol)
                config = (
                    replace(config, meshcore=radio)
                    if protocol == "meshcore"
                    else replace(config, meshtastic=radio)
                )
            elif option == "3":
                reticulum, pending = reticulum_config(config)
                config, extra = replace(config, reticulum=reticulum), pending
            elif option == "4":
                source = _ask(
                    "Stable feed ID (same ID on every importing peer)", f"{config.region}-news"
                )
                previous = next((f for f in config.feeds if f.source_id == source), None)
                url = _ask("RSS/Atom URL, or remove", previous.url if previous else "")
                others = tuple(f for f in config.feeds if f.source_id != source)
                feeds = (
                    others
                    if url == "remove"
                    else (
                        *others,
                        replace(previous, url=url) if previous else FeedConfig(source, url),
                    )
                )
                config = replace(config, feeds=feeds)
            elif option == "5":
                port = number("Local web port", config.bind_port, 1, 65535)
                url = _ask("Public HTTPS URL (none for local-only)", config.public_url or "none")
                if url != "none" and not url.startswith("https://"):
                    raise ValueError("Use an HTTPS public URL, or none for local-only")
                config = replace(config, bind_port=port, public_url=None if url == "none" else url)
                print(
                    "The web listener keeps its bind address; "
                    "use a TLS reverse proxy for remote access."
                )
        except (ValueError, OSError) as error:
            print(f"Not saved: {error}")
    if config == original:
        print("No configuration changes.")
        return
    if extra:
        target, text = extra
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            created = os.fstat(stream.fileno())
    try:
        backup = update_config(config, path, expected)
    except (ValueError, OSError):
        if extra:
            # Remove only the profile we created, never a concurrent replacement.
            current = target.lstat()
            if (current.st_ino, current.st_mtime_ns, current.st_size) == (
                created.st_ino,
                created.st_mtime_ns,
                created.st_size,
            ):
                target.unlink()
        raise
    print(f"Saved. Original configuration (including comments): {backup}")
    print("Restart your BBS service to apply the settings, or run:")
    print(f"mesh-bbs --config {shlex.quote(str(path))} serve")
    print(f"Local web: http://{config.bind_host}:{config.bind_port}")
    if config.reticulum.enabled and not config.peers:
        print(
            "Reticulum is enabled but no BBS peers are paired. "
            "Exchange cards using peer export / peer add."
        )
