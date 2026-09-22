"""Build and install the wheel away from the source tree, without a network."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_installed_wheel_can_initialize_publish_read_and_back_up(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    assert uv, "Package verification requires uv"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(tmp_path / "venv")
    env["UV_PYTHON_DOWNLOADS"] = "never"

    def run(arguments: list[str], *, cwd: Path = tmp_path) -> str:
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return completed.stdout

    dist = tmp_path / "dist"
    run([sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(dist)], cwd=root)
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1
    run(
        [
            uv,
            "sync",
            "--locked",
            "--offline",
            "--no-dev",
            "--no-install-project",
            "--python",
            sys.executable,
        ],
        cwd=root,
    )
    run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            str(tmp_path / "venv/bin/python"),
            str(wheels[0]),
        ]
    )
    executable = str(tmp_path / "venv/bin/mesh-bbs")
    assert run([executable, "--version"]).strip()
    config = tmp_path / "config.toml"
    config.write_text(
        'name = "Packaged host"\nregion = "package-test"\n'
        f"data_dir = {json.dumps(str(tmp_path / 'data'))}\n",
        encoding="utf-8",
    )
    command = [executable, "--config", str(config)]
    identity = json.loads(run([*command, "init"]))
    assert identity["region"] == "package-test"
    assert len(identity["origin"]) == 64
    body = tmp_path / "newsletter.txt"
    body.write_text("A newsletter from the installed wheel.\n", encoding="utf-8")
    post = [
        *command,
        "post",
        "news",
        "Package smoke",
        "--body-file",
        str(body),
        "--operation",
        "wheel-install-test",
    ]
    first = run(post)
    assert first == run(post)
    assert "installed wheel" in run([*command, "command", "news latest"])
    assert "Database OK" in run([*command, "doctor"])
    backup = tmp_path / "snapshot.sqlite3"
    run([*command, "backup", str(backup)])
    assert backup.is_file() and backup.stat().st_size > 0
