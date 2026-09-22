from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from mesh_bbs.config import (
    FeedConfig,
    HostConfig,
    PeerConfig,
    RadioConfig,
    ReticulumConfig,
    default_config_path,
    default_data_dir,
    load_config,
    save_config,
    validate_slug,
)
from mesh_bbs.setup import run_setup


def test_config_roundtrip_with_all_transports_and_peer(tmp_path: Path) -> None:
    public_key = "ab" * 32
    config = HostConfig(
        name='Colorado "West" \\ host',
        region="colorado-mesh",
        data_dir=tmp_path / "data",
        feeds=(FeedConfig("colorado-news", "https://example.org/feed.xml"),),
        peers=(
            PeerConfig(
                origin=hashlib.sha256(bytes.fromhex(public_key)).hexdigest(),
                public_key=public_key,
                allowed_boards=("news",),
                reticulum_identity="cd" * 16,
            ),
        ),
        reticulum=ReticulumConfig(True, tmp_path / "rns"),
        meshcore=RadioConfig(True, serial_port="/dev/ttyUSB0"),
        meshtastic=RadioConfig(True, tcp_host="localhost", tcp_port=4403),
        public_url="https://bbs.example.org:8443",
    )
    path = tmp_path / "config/config.toml"
    save_config(config, path)
    assert load_config(path) == config
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_duplicate_reticulum_identity_cannot_replace_peer_board_scope(tmp_path: Path) -> None:
    peers = tuple(
        PeerConfig(
            origin=hashlib.sha256(bytes.fromhex(key)).hexdigest(),
            public_key=key,
            allowed_boards=(board,),
            reticulum_identity="cd" * 16,
        )
        for key, board in (("ab" * 32, "general"), ("ef" * 32, "news"))
    )
    with pytest.raises(ValueError, match="duplicate peer Reticulum identities"):
        HostConfig("Host", "test", tmp_path, peers=peers)


def test_config_writer_refuses_overwrite_and_dangling_symlink(tmp_path: Path) -> None:
    config = HostConfig("Host", "test", tmp_path)
    path = tmp_path / "config.toml"
    path.write_text("operator data", encoding="utf-8")
    with pytest.raises(FileExistsError):
        save_config(config, path)
    assert path.read_text() == "operator data"
    path.unlink()
    path.symlink_to(tmp_path / "missing.toml")
    with pytest.raises(FileExistsError):
        save_config(config, path)
    assert not (tmp_path / "missing.toml").exists()


def test_regions_have_separate_xdg_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert default_config_path() == tmp_path / "config/mesh-bbs/colorado-mesh/config.toml"
    assert default_config_path("other-mesh") == tmp_path / "config/mesh-bbs/other-mesh/config.toml"
    assert default_data_dir("other-mesh") == tmp_path / "data/mesh-bbs/other-mesh"


def test_relative_xdg_paths_are_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_config_path() == tmp_path / ".config/mesh-bbs/colorado-mesh/config.toml"


@pytest.mark.parametrize("region", ["", "../escape", "Colorado", "a/b", "-mesh", "a--b", "x" * 65])
def test_region_slug_rejects_paths_and_ambiguous_names(region: str) -> None:
    with pytest.raises(ValueError):
        validate_slug(region)


def test_minimal_config_disables_connections_and_resolves_relative_data(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('name = "Local"\nregion = "test"\ndata_dir = "data"\n')
    config = load_config(path)
    assert config.data_dir == tmp_path / "data"
    assert not config.reticulum.enabled
    assert not config.meshcore.enabled
    assert not config.meshtastic.enabled
    assert config.peers == config.feeds == ()
    assert config.bind_host == "127.0.0.1"
    assert config.public_url is None


@pytest.mark.parametrize(
    "addition",
    [
        'regionn = "typo"',
        'boards = "news"',
        "bind_port = true",
        "bind_port = 65536",
        'editors = ["meshtastic:00001234"]',
        'public_url = "file:///etc/passwd"',
        'public_url = "https://user:password@example.org"',
        'public_url = "https://example.org/feed.xml"',
        'public_url = "https://example.org:bad"',
        'public_url = "https://example.org:0"',
        'public_url = "https://example.org?q=x"',
        'public_url = "https://example.org/#part"',
        'public_url = "https://invalid host"',
        'feeds = "https://example.org"',
        "[reticulum]\nenabled = true",
        '[reticulum]\nenabled = "false"',
        "[meshcore]\nenabled = true",
        '[meshcore]\nserial_port = "/dev/ttyUSB0"\ntcp_host = "localhost"',
        '[meshcore]\nchannel = "mistyped-option"',
        '[[feeds]]\nsource_id = "newsletter"',
        '[[feeds]]\nsource_id = "news"\nurl = "file:///etc/passwd"',
        '[[feeds]]\nsource_id = "news"\nurl = "https://example.org/feed"\nboard = "missing"',
        '[[peers]]\norigin = "bad"\npublic_key = "bad"',
    ],
)
def test_malformed_or_unsafe_configuration_fails(tmp_path: Path, addition: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text('name = "Local"\nregion = "test"\n' + addition + "\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_peer_trust_requires_matching_key_and_empty_boards_grant_nothing() -> None:
    public_key = "00" * 32
    origin = hashlib.sha256(bytes.fromhex(public_key)).hexdigest()
    assert PeerConfig(origin, public_key).allowed_boards == ()
    with pytest.raises(ValueError, match="SHA-256"):
        PeerConfig("ff" * 32, public_key)


def test_setup_colorado_preset_does_not_enable_transports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    answers = iter(["", "My host"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    path = run_setup()
    config = load_config(path)
    assert config.name == "My host"
    assert config.region == "colorado-mesh"
    assert config.feeds == (
        FeedConfig("colorado-mesh-blog", "https://blog.coloradomesh.org/feed.xml"),
    )
    assert not config.reticulum.enabled
    assert not config.meshcore.enabled
    assert not config.meshtastic.enabled
    assert not config.data_dir.exists()
    assert "No public peers" in capsys.readouterr().out


def test_setup_custom_community_and_invalid_slug_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    answers = iter(["invalid", "2", "Front Range Friends", "../bad", "front-range", "West host"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    path = run_setup(tmp_path / "custom.toml")
    config = load_config(path)
    assert config.region == "front-range"
    assert config.feeds == ()
    assert config.data_dir == tmp_path / "data/mesh-bbs/front-range"


def test_setup_refuses_existing_config_and_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    path = tmp_path / "config.toml"
    path.write_text("existing")
    with pytest.raises(FileExistsError):
        run_setup(path)
    assert path.read_text() == "existing"
    path.unlink()
    path.symlink_to(tmp_path / "missing.toml")
    with pytest.raises(FileExistsError):
        run_setup(path)
    assert not (tmp_path / "missing.toml").exists()


def test_setup_without_terminal_explains_next_step(monkeypatch: pytest.MonkeyPatch) -> None:
    def closed_input(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed_input)
    with pytest.raises(ValueError, match="interactive terminal"):
        run_setup()
