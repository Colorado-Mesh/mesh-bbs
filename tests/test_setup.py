from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from mesh_bbs.cli import parser
from mesh_bbs.config import load_config
from mesh_bbs.setup import run_setup


@pytest.mark.parametrize("custom_path", [False, True])
def test_setup_prints_runnable_web_start_steps_without_creating_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    custom_path: bool,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "configuration folder"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data folder"))
    answers = iter(["2", "Foothills Mesh", "foothills", "Community library", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    supplied = tmp_path / "other's folder" / "host config.toml" if custom_path else None

    target = run_setup(supplied)
    lines = capsys.readouterr().out.splitlines()
    parsed = []
    for label, expected in (
        ("Next: ", "init"),
        ("Create your contributor key: ", "web-access"),
        ("Start the host: ", "serve"),
    ):
        command = next(line.removeprefix(label) for line in lines if line.startswith(label))
        executable, *arguments = shlex.split(command)
        assert executable == "mesh-bbs"
        options = parser().parse_args(arguments)
        assert options.action == expected
        assert options.config == target
        parsed.append(options)
    assert parsed[1].access_action == "create"
    assert parsed[1].name == "alice" and not parsed[1].editor
    assert "Sign in: http://127.0.0.1:8080/connect" in lines
    assert "Setup has not created an access key or started the host." in lines
    config = load_config(target)
    assert config.region == "foothills"
    assert config.feeds == ()
    assert not config.data_dir.exists()
    assert not config.reticulum.enabled and not config.meshcore.enabled
    assert not config.meshtastic.enabled


def test_initial_setup_enters_connection_wizard_and_prints_final_port(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    answers = iter(
        [
            "2",
            "Foothills",
            "foothills",
            "Foothills BBS",
            "y",
            "2",
            "2",
            "192.0.2.1",
            "4403",
            "2",
            "0",
            "5",
            "8087",
            "none",
            "0",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    target = run_setup()
    config = load_config(target)
    assert config.region == "foothills" and config.meshtastic.enabled
    assert config.bind_port == 8087
    assert "Local web address: http://127.0.0.1:8087" in capsys.readouterr().out
    assert not (config.data_dir / "bbs.sqlite3").exists()
