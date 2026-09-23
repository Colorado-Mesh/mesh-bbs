from dataclasses import replace

import pytest

from mesh_bbs.config import HostConfig, ReticulumConfig, load_config, save_config, update_config
from mesh_bbs.configure import number, run_configure


def test_wizard_sets_radio_feed_and_reticulum_without_connecting(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    config = HostConfig("Foothills BBS", "foothills", tmp_path / "data")
    save_config(config, path)
    original = path.read_bytes()
    answers = iter(
        [
            "2",
            "2",
            "192.0.2.1",
            "4403",
            "2",
            "0",  # Meshtastic TCP MediumFast, no notices
            "3",
            "2",
            "gateway.example.org",
            "4242",  # Reticulum TCP profile
            "4",
            "foothills-news",
            "https://example.org/feed.xml",
            "0",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    run_configure(path)
    loaded = load_config(path)
    assert loaded.meshtastic.enabled and loaded.meshtastic.packet_airtime_seconds == 1
    assert loaded.meshtastic.tcp_port == 4403 and not loaded.meshcore.enabled
    assert loaded.reticulum.enabled
    assert "gateway.example.org" in (loaded.reticulum.config_dir / "config").read_text()
    assert loaded.feeds[0].source_id == "foothills-news"
    assert list(tmp_path.glob("config.toml.backup-*"))[0].read_bytes() == original
    assert not (config.data_dir / "bbs.sqlite3").exists()
    assert not (config.data_dir / "reticulum/identity").exists()


def test_update_preserves_old_file_and_refuses_stale_data_or_symlink(tmp_path):
    path = tmp_path / "config.toml"
    config = HostConfig("A", "test", tmp_path / "data")
    save_config(config, path)
    before = path.read_bytes()
    backup = update_config(replace(config, name="B"), path, before)
    assert backup.read_bytes() == before
    assert path.stat().st_mode & 0o777 == backup.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="changed"):
        update_config(config, path, before)
    alias = tmp_path / "alias.toml"
    alias.symlink_to(path)
    with pytest.raises(ValueError):
        update_config(config, alias, path.read_bytes())
    assert load_config(path).name == "B"


def test_existing_reticulum_profile_and_unrelated_settings_are_preserved(tmp_path, monkeypatch):
    directory = tmp_path / "rns"
    directory.mkdir()
    (directory / "config").write_text("[reticulum]\n[interfaces]\n")
    config = HostConfig("A", "test", tmp_path / "data", reticulum=ReticulumConfig(True, directory))
    path = tmp_path / "config.toml"
    save_config(config, path)
    answers = iter(["5", "8085", "https://bbs.example.org", "0"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    run_configure(path)
    loaded = load_config(path)
    assert loaded.reticulum == config.reticulum
    assert loaded.bind_host == "127.0.0.1" and loaded.bind_port == 8085
    assert (directory / "config").read_text() == "[reticulum]\n[interfaces]\n"


def test_wizard_no_change_or_invalid_choice_does_not_rewrite(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    save_config(HostConfig("A", "test", tmp_path / "data"), path)
    before = path.read_bytes()
    answers = iter(["nonsense", "5", "8080", "http://unsafe.example.org", "0"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    run_configure(path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.backup-*"))


def test_numeric_prompt_exits_when_terminal_input_ends(monkeypatch):
    def closed_input(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed_input)
    with pytest.raises(ValueError, match="interactive terminal"):
        number("Port", 8080, 1, 65535)


def test_failed_config_save_removes_only_new_reticulum_profile(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    config = HostConfig("A", "test", tmp_path / "data")
    save_config(config, path)
    original = path.read_bytes()
    answers = iter(["3", "2", "gateway.example.org", "4242", "0"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    def concurrent_edit(*_):
        path.write_bytes(original + b"# Edited elsewhere\n")
        raise ValueError("Configuration changed; reload it before saving")

    monkeypatch.setattr("mesh_bbs.configure.update_config", concurrent_edit)
    with pytest.raises(ValueError, match="changed"):
        run_configure(path)
    assert path.read_bytes() == original + b"# Edited elsewhere\n"
    assert not (config.data_dir / "reticulum-config/config").exists()
