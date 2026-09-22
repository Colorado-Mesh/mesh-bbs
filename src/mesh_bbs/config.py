"""Validated operator configuration, kept separate for each community."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def validate_slug(value: str, label: str = "region") -> str:
    if not isinstance(value, str) or len(value) > 64 or not _SLUG.fullmatch(value):
        raise ValueError(f"{label} must be 1-64 lowercase letters/numbers separated by hyphens")
    return value


def _text(value: str, label: str, limit: int = 200) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be nonempty text, at most {limit} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters")


def _boolean(value: bool, label: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")


def _integer(value: int, label: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")


def _hex(value: str, label: str, length: int) -> None:
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise ValueError(f"{label} must be {length} lowercase hexadecimal characters")


def _xdg_path(variable: str, fallback: Path) -> Path:
    configured = os.environ.get(variable)
    return Path(configured) if configured and Path(configured).is_absolute() else fallback


def default_config_path(region: str = "colorado-mesh") -> Path:
    validate_slug(region)
    return (
        _xdg_path("XDG_CONFIG_HOME", Path.home() / ".config") / "mesh-bbs" / region / "config.toml"
    )


def default_data_dir(region: str = "colorado-mesh") -> Path:
    validate_slug(region)
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local/share") / "mesh-bbs" / region


@dataclass(frozen=True)
class FeedConfig:
    source_id: str
    url: str
    board: str = "news"
    poll_seconds: int = 900

    def __post_init__(self) -> None:
        validate_slug(self.source_id, "feed source_id")
        validate_slug(self.board, "feed board")
        _text(self.url, "feed url", 2048)
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("feed url must be an absolute HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("feed url must not contain credentials")
        _integer(self.poll_seconds, "feed poll_seconds", 60, 86400)


@dataclass(frozen=True)
class PeerConfig:
    origin: str
    public_key: str
    allowed_boards: tuple[str, ...] = ()
    reticulum_identity: str | None = None
    can_moderate: bool = False

    def __post_init__(self) -> None:
        _hex(self.origin, "peer origin", 64)
        _hex(self.public_key, "peer public_key", 64)
        if hashlib.sha256(bytes.fromhex(self.public_key)).hexdigest() != self.origin:
            raise ValueError("peer origin must be the SHA-256 of its raw public key")
        for board in self.allowed_boards:
            validate_slug(board, "peer allowed board")
        if self.reticulum_identity is not None:
            _hex(self.reticulum_identity, "peer reticulum_identity", 32)
        _boolean(self.can_moderate, "peer can_moderate")


@dataclass(frozen=True)
class ReticulumConfig:
    enabled: bool = False
    config_dir: Path | None = None

    def __post_init__(self) -> None:
        _boolean(self.enabled, "reticulum enabled")
        if self.enabled and self.config_dir is None:
            raise ValueError("enabled Reticulum requires an explicit config_dir")


@dataclass(frozen=True)
class RadioConfig:
    enabled: bool = False
    serial_port: str | None = None
    tcp_host: str | None = None
    tcp_port: int | None = None
    min_interval: float = 3.0
    airtime_budget_seconds: float = 120.0
    airtime_window_seconds: float = 3600.0
    packet_airtime_seconds: float = 10.0
    firmware_max_attempts: int = 4

    def __post_init__(self) -> None:
        _boolean(self.enabled, "radio enabled")
        if self.serial_port is not None:
            _text(self.serial_port, "radio serial_port", 1024)
        if self.tcp_host is not None:
            _text(self.tcp_host, "radio tcp_host", 255)
        if self.tcp_port is not None:
            _integer(self.tcp_port, "radio tcp_port", 1, 65535)
            if self.tcp_host is None:
                raise ValueError("radio tcp_port requires tcp_host")
        if self.serial_port is not None and self.tcp_host is not None:
            raise ValueError("choose either radio serial_port or tcp_host")
        if self.enabled and self.serial_port is None and self.tcp_host is None:
            raise ValueError("enabled radio requires serial_port or tcp_host")
        for name in (
            "min_interval",
            "airtime_budget_seconds",
            "airtime_window_seconds",
            "packet_airtime_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"radio {name} must be a finite number")
            if value <= 0:
                raise ValueError(f"radio {name} must be positive")
        if self.airtime_budget_seconds > self.airtime_window_seconds:
            raise ValueError("radio airtime budget cannot exceed its window")
        _integer(self.firmware_max_attempts, "radio firmware_max_attempts", 1, 10)
        if (
            self.packet_airtime_seconds * max(2, self.firmware_max_attempts)
            > self.airtime_budget_seconds
        ):
            raise ValueError("radio airtime budget must cover a complete response retry allowance")


@dataclass(frozen=True)
class HostConfig:
    name: str
    region: str
    data_dir: Path
    boards: tuple[str, ...] = ("general", "news")
    editors: tuple[str, ...] = ("local:operator",)
    feeds: tuple[FeedConfig, ...] = ()
    peers: tuple[PeerConfig, ...] = ()
    reticulum: ReticulumConfig = field(default_factory=ReticulumConfig)
    meshcore: RadioConfig = field(default_factory=RadioConfig)
    meshtastic: RadioConfig = field(default_factory=RadioConfig)
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    public_url: str | None = None

    def __post_init__(self) -> None:
        _text(self.name, "host name")
        validate_slug(self.region)
        if not isinstance(self.data_dir, Path) or not self.data_dir.is_absolute():
            raise ValueError("data_dir must be an absolute Path")
        if not self.boards:
            raise ValueError("at least one board is required")
        for board in self.boards:
            validate_slug(board, "board")
        if len(set(self.boards)) != len(self.boards):
            raise ValueError("duplicate boards are not allowed")
        for editor in self.editors:
            _text(editor, "editor")
            if editor.lower().startswith("meshtastic:"):
                raise ValueError(
                    "Meshtastic node addresses cannot authenticate newsletter editors; "
                    "use local, MeshCore, or Reticulum editorial access"
                )
        for feed in self.feeds:
            if feed.board not in self.boards:
                raise ValueError(f"feed board {feed.board!r} is not configured")
        if len({feed.source_id for feed in self.feeds}) != len(self.feeds):
            raise ValueError("duplicate feed source_id values are not allowed")
        if len({peer.origin for peer in self.peers}) != len(self.peers):
            raise ValueError("duplicate peer origins are not allowed")
        identities = [peer.reticulum_identity for peer in self.peers if peer.reticulum_identity]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate peer Reticulum identities are not allowed")
        for peer in self.peers:
            if not set(peer.allowed_boards).issubset(self.boards):
                raise ValueError("peer allowed_boards must name configured boards")
        _text(self.bind_host, "bind_host", 255)
        _integer(self.bind_port, "bind_port", 1, 65535)
        if self.public_url is not None:
            _text(self.public_url, "public_url", 2048)
            parsed = urlsplit(self.public_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or any(character.isspace() for character in self.public_url)
            ):
                raise ValueError(
                    "public_url must be an HTTP(S) origin without credentials or a path"
                )
            if parsed.port is not None:
                _integer(parsed.port, "public_url port", 1, 65535)


def _keys(table: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = table.keys() - allowed
    if unknown:
        raise ValueError(f"unknown {label} option(s): {', '.join(sorted(unknown))}")


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a TOML table")
    return value


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be an array of strings")
    return tuple(value)


def _tables(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array of tables")
    return [_table(item, label) for item in value]


def _path(value: Any, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty path")
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path: Path) -> HostConfig:
    path = path.expanduser().resolve()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    _keys(
        data,
        {
            "name",
            "region",
            "data_dir",
            "boards",
            "editors",
            "feeds",
            "peers",
            "reticulum",
            "meshcore",
            "meshtastic",
            "bind_host",
            "bind_port",
            "public_url",
        },
        "configuration",
    )
    try:
        region = validate_slug(data["region"])
        name = data["name"]
    except KeyError as error:
        raise ValueError(f"missing required configuration option: {error.args[0]}") from error
    feeds = []
    for values in _tables(data.get("feeds", []), "feeds"):
        _keys(values, {"source_id", "url", "board", "poll_seconds"}, "feed")
        try:
            feeds.append(FeedConfig(**values))
        except TypeError as error:
            raise ValueError(f"invalid feed configuration: {error}") from error
    peers = []
    for raw in _tables(data.get("peers", []), "peers"):
        values = dict(raw)
        _keys(
            values,
            {
                "origin",
                "public_key",
                "allowed_boards",
                "reticulum_identity",
                "can_moderate",
            },
            "peer",
        )
        values["allowed_boards"] = _strings(values.get("allowed_boards", []), "peer allowed_boards")
        try:
            peers.append(PeerConfig(**values))
        except TypeError as error:
            raise ValueError(f"invalid peer configuration: {error}") from error
    reticulum = dict(_table(data.get("reticulum", {}), "reticulum"))
    _keys(reticulum, {"enabled", "config_dir"}, "reticulum")
    if "config_dir" in reticulum:
        reticulum["config_dir"] = _path(
            reticulum["config_dir"], path.parent, "reticulum config_dir"
        )
    radios = {}
    for protocol in ("meshcore", "meshtastic"):
        values = _table(data.get(protocol, {}), protocol)
        _keys(
            values,
            {
                "enabled",
                "serial_port",
                "tcp_host",
                "tcp_port",
                "min_interval",
                "airtime_budget_seconds",
                "airtime_window_seconds",
                "packet_airtime_seconds",
                "firmware_max_attempts",
            },
            protocol,
        )
        radios[protocol] = RadioConfig(**values)
    return HostConfig(
        name=name,
        region=region,
        data_dir=_path(
            data.get("data_dir", str(default_data_dir(region))), path.parent, "data_dir"
        ),
        boards=_strings(data.get("boards", ["general", "news"]), "boards"),
        editors=_strings(data.get("editors", ["local:operator"]), "editors"),
        feeds=tuple(feeds),
        peers=tuple(peers),
        reticulum=ReticulumConfig(**reticulum),
        meshcore=radios["meshcore"],
        meshtastic=radios["meshtastic"],
        bind_host=data.get("bind_host", "127.0.0.1"),
        bind_port=data.get("bind_port", 8080),
        public_url=data.get("public_url"),
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def save_config(config: HostConfig, path: Path) -> None:
    """Create configuration without replacing any existing file or symlink."""
    lines = ["# Mesh BBS operator configuration. Radio connections require explicit opt-in."]
    for key in (
        "name",
        "region",
        "data_dir",
        "boards",
        "editors",
        "bind_host",
        "bind_port",
        "public_url",
    ):
        value = getattr(config, key)
        if value is not None:
            lines.append(f"{key} = {_toml_value(value)}")
    for section in ("reticulum", "meshcore", "meshtastic"):
        lines.extend(("", f"[{section}]"))
        for key, value in vars(getattr(config, section)).items():
            if value is not None:
                lines.append(f"{key} = {_toml_value(value)}")
    for section in ("feeds", "peers"):
        for entry in getattr(config, section):
            lines.extend(("", f"[[{section}]]"))
            for key, value in vars(entry).items():
                if value is not None:
                    lines.append(f"{key} = {_toml_value(value)}")
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
