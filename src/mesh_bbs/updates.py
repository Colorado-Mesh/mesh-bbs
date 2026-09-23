"""Opt-in supervision of side-by-side, CI-approved application installations."""

from __future__ import annotations

import fcntl
import gzip
import importlib.metadata
import io
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from mesh_bbs.config import HostConfig, load_config, save_config, update_config

logger = logging.getLogger(__name__)
REPOSITORY = "Colorado-Mesh/mesh-bbs"
API = f"https://api.github.com/repos/{REPOSITORY}"
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
START_TIMEOUT = 60
FIRST_CHECK_DELAY = 30
STOP_TIMEOUT = 30


@contextmanager
def service_lock(config: HostConfig) -> Iterator[None]:
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        config.data_dir / "service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("A BBS service already owns this data directory") from error
        yield
    finally:
        os.close(descriptor)


def api_json(path: str) -> Any:
    request = urllib.request.Request(
        API + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "mesh-bbs-updater"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if not response.url.startswith(API + "/"):
            raise ValueError("Unexpected update API redirect")
        body = response.read(MAX_JSON_BYTES + 1)
    if len(body) > MAX_JSON_BYTES:
        raise ValueError("Update API response exceeds limit")
    return json.loads(body)


def approved_head() -> tuple[str, bool]:
    head = api_json("/commits/main")
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("Update API returned an invalid commit")
    response = api_json(f"/actions/workflows/ci.yml/runs?head_sha={sha}&event=push&per_page=20")
    runs = response.get("workflow_runs") if isinstance(response, dict) else None
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise ValueError("Invalid CI response")
    matching = [
        run
        for run in runs
        if type(run.get("id")) is int
        and type(run.get("run_attempt", 1)) is int
        and run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
    ]
    latest: dict[str, Any] = max(
        matching, key=lambda run: (run["id"], run.get("run_attempt", 1)), default={}
    )
    return sha, latest.get("status") == "completed" and latest.get("conclusion") == "success"


def installed_revision() -> str | None:
    try:
        direct = importlib.metadata.distribution("mesh-bbs").read_text("direct_url.json")
        url = json.loads(direct or "{}").get("url", "")
        match = re.fullmatch(
            rf"https://github.com/{re.escape(REPOSITORY)}/archive/([0-9a-f]{{40}})\.tar\.gz",
            url,
        )
        return match[1] if match else None
    except (ValueError, importlib.metadata.PackageNotFoundError):
        return None


def unpack_source(body: bytes, sha: str, destination: Path) -> Path:
    """Extract regular repository files only, with bounded size and path depth."""
    if not SHA.fullmatch(sha) or len(body) > 16 * 1024 * 1024:
        raise ValueError("Invalid source archive")
    root_name = f"mesh-bbs-{sha}"
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
        expanded = compressed.read(MAX_SOURCE_BYTES + 1)
    if len(expanded) > MAX_SOURCE_BYTES:
        raise ValueError("Expanded source archive exceeds limit")
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        total = 0
        for count, member in enumerate(archive, start=1):
            parts = Path(member.name).parts
            total += member.size
            if (
                count > 5000
                or total > 64 * 1024 * 1024
                or not 0 <= member.size <= 8 * 1024 * 1024
                or not parts
                or parts[0] != root_name
                or ".." in parts
                or len(parts) > 20
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("Unsafe or oversized source archive")
            path = destination.joinpath(*parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                assert stream is not None
                with stream, path.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=64 * 1024)
    project = destination / root_name
    if not (project / "uv.lock").is_file() or not (project / "pyproject.toml").is_file():
        raise ValueError("Source archive lacks project or lockfile")
    return project


def download_source(sha: str, release: Path) -> Path:
    if not SHA.fullmatch(sha):
        raise ValueError("Invalid source revision")
    url = f"https://github.com/{REPOSITORY}/archive/{sha}.tar.gz"
    with urllib.request.urlopen(url, timeout=30) as response:
        if not response.url.startswith(
            (f"https://github.com/{REPOSITORY}/", f"https://codeload.github.com/{REPOSITORY}/")
        ):
            raise ValueError("Unexpected source archive redirect")
        body = response.read(16 * 1024 * 1024 + 1)
    source = release / "source"
    if source.is_symlink():
        raise ValueError("Source directory must not be a symlink")
    if source.exists():
        shutil.rmtree(source)
    source.mkdir(mode=0o700)
    return unpack_source(body, sha, source)


def state_path(config: HostConfig) -> Path:
    return config.data_dir / "updates" / "state.json"


def read_state(config: HostConfig) -> dict[str, Any]:
    path = state_path(config)
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("Invalid updater state file")
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError("Invalid updater state")
    return result


def write_state(config: HostConfig, state: dict[str, Any]) -> None:
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def update_command(action: str, path: Path, config: HostConfig, expected: bytes) -> int:
    if action == "reset":
        directory = state_path(config).parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            directory / "supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "Stop the BBS service before resetting update selection"
                ) from error
            with service_lock(config):
                state_path(config).unlink(missing_ok=True)
        finally:
            os.close(descriptor)
        print("Cached version selection cleared. Start the service to use the installed version.")
        return 0
    if action in {"enable", "disable"}:
        enabled = action == "enable"
        if config.auto_update != enabled:
            backup = update_config(replace(config, auto_update=enabled), path, expected)
            print(f"Configuration saved; private backup: {backup}")
        print(
            "Automatic updates "
            + (
                "enabled. Restart the service to start checking."
                if enabled
                else "disabled. A running updater stops checking automatically."
            )
        )
        return 0
    state = read_state(config)
    report = {
        "enabled": config.auto_update,
        "interval_seconds": config.update_interval_seconds,
        "installed_revision": installed_revision(),
        **state,
    }
    if action == "check":
        sha, approved = approved_head()
        report.update(main_revision=sha, ci_passed=approved)
    print(json.dumps(report, indent=2))
    return 0


class UpdateSupervisor:
    def __init__(self, path: Path, config: HostConfig) -> None:
        self.path = path
        self.config = config
        self.root = config.data_dir / "updates"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state = read_state(config)
        self.python = str(Path(sys.executable).absolute())
        self.child: subprocess.Popen[bytes] | None = None
        self.stopping = False
        self.tools: subprocess.Popen[bytes] | None = None
        self.old_handlers: dict[int, Any] = {}
        self.instance = secrets.token_hex(16)
        self.ready = False

    def save(self, **changes: Any) -> None:
        self.state.update(changes)
        write_state(self.config, self.state)

    def command(self, python: str, *, supervised: bool) -> list[str]:
        return [
            python,
            "-m",
            "mesh_bbs.cli",
            "--config",
            str(self.path),
            "serve",
            *([] if supervised else ["--no-auto-update"]),
        ]

    def valid_python(self, value: Any) -> str:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Updater interpreter must be an absolute path")
        path = Path(value)
        original = self.state.get("original_python", self.python)
        releases = self.root / "releases"
        if value != original and not path.is_relative_to(releases):
            raise ValueError("Updater interpreter is outside its managed installations")
        if ".." in path.parts or not path.is_file():
            raise ValueError("Updater interpreter is unavailable")
        return value

    def switch(self, python: str) -> None:
        python = self.valid_python(python)
        self.stop_child()
        if self.stopping:
            return
        # The lock closes on exec; the replacement reacquires before spawning a reader.
        os.execv(python, self.command(python, supervised=True))

    def on_signal(self, signum: int, _frame: Any) -> None:
        if signum == getattr(signal, "SIGUSR1", None):
            if (
                self.ready
                and self.config.reticulum.enabled
                and self.child
                and self.child.poll() is None
            ):
                self.child.send_signal(signum)
        else:
            self.stopping = True

    def stop_child(self) -> None:
        self.ready = False
        if self.child is None:
            return
        child, self.child = self.child, None
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        if child.stdin:
            child.stdin.close()

    def start_child(self) -> None:
        env = dict(os.environ, MESH_BBS_SUPERVISED="1", MESH_BBS_INSTANCE=self.instance)
        self.child = subprocess.Popen(
            self.command(self.python, supervised=False), stdin=subprocess.PIPE, env=env
        )

    def run_tool(self, args: list[str], *, env: dict[str, str] | None = None) -> None:
        self.tools = subprocess.Popen(args, env=env, start_new_session=True)
        deadline = time.monotonic() + 900
        try:
            while self.tools.poll() is None:
                if self.stopping or time.monotonic() >= deadline:
                    raise RuntimeError("Update preparation stopped or timed out")
                time.sleep(0.1)
            if self.tools.returncode:
                raise RuntimeError(f"Update preparation failed (exit {self.tools.returncode})")
        finally:
            if self.tools.poll() is None:
                os.killpg(self.tools.pid, signal.SIGTERM)
                try:
                    self.tools.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(self.tools.pid, signal.SIGKILL)
                    self.tools.wait(timeout=10)
            self.tools = None

    def stage(self, sha: str) -> str:
        if not SHA.fullmatch(sha):
            raise ValueError("Invalid update revision")
        uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
        release = self.root / "releases" / sha
        self.prune(sha)
        release.mkdir(parents=True, exist_ok=True, mode=0o700)
        if release.is_symlink():
            raise ValueError("Release directory must not be a symlink")
        project = download_source(sha, release)
        env = dict(
            os.environ,
            UV_PROJECT_ENVIRONMENT=str(release / "venv"),
            UV_NO_PROGRESS="1",
        )
        # Service managers often omit the paths used by the interactive installer.
        env["PATH"] = os.pathsep.join(
            [
                env.get("PATH", os.defpath),
                str(Path.home() / ".cargo/bin"),
                str(Path.home() / ".local/bin"),
                "/usr/local/bin",
                "/opt/homebrew/bin",
            ]
        )
        if platform.system() == "Darwin" and platform.machine() == "x86_64":
            openssl = Path("/usr/local/opt/openssl@3")
            if "OPENSSL_DIR" not in env and (openssl / "include/openssl/ssl.h").is_file():
                env["OPENSSL_DIR"] = str(openssl)
        self.run_tool(
            [
                uv,
                "sync",
                "--locked",
                "--no-dev",
                "--all-extras",
                "--no-editable",
                "--managed-python",
                "--python",
                "3.12",
                "--project",
                str(project),
            ],
            env=env,
        )
        python = str(release / "venv" / "bin" / "python")
        self.run_tool([python, "-m", "mesh_bbs.cli", "--help"])
        self.preflight(python)
        return python

    def prune(self, staging: str) -> None:
        keep = {staging, self.state.get("active_revision"), self.state.get("previous_revision")}
        directory = self.root / "releases"
        if directory.exists():
            for path in directory.iterdir():
                if SHA.fullmatch(path.name) and path.name not in keep and not path.is_symlink():
                    shutil.rmtree(path)

    def preflight(self, python: str) -> None:
        # Open a snapshot with the candidate; no server, network interface or real identity use.
        with tempfile.TemporaryDirectory(prefix="preflight-", dir=self.root) as temporary:
            target = Path(temporary)
            database = self.config.data_dir / "bbs.sqlite3"
            if database.exists():
                with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(target / "bbs.sqlite3")) as destination:
                        source.backup(destination)
            candidate_config = target / "config.toml"
            save_config(replace(self.config, data_dir=target, auto_update=False), candidate_config)
            self.run_tool(
                [python, "-m", "mesh_bbs.cli", "--config", str(candidate_config), "doctor"]
            )

    def backup(self, sha: str, configuration: bytes) -> None:
        directory = self.root / "backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".preparing-", dir=directory))
        try:
            database = self.config.data_dir / "bbs.sqlite3"
            if database.exists():
                with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(temporary / "bbs.sqlite3")) as snapshot:
                        source.backup(snapshot)
                (temporary / "bbs.sqlite3").chmod(0o600)
            (temporary / "config.toml").write_bytes(configuration)
            (temporary / "config.toml").chmod(0o600)
            target = directory / f"{sha}-{time.time_ns()}"
            temporary.rename(target)
            self.save(backup=str(target))
            snapshots = sorted(
                (
                    p
                    for p in directory.iterdir()
                    if re.fullmatch(r"[0-9a-f]{40}-[0-9]+", p.name) and not p.is_symlink()
                ),
                key=lambda p: p.stat().st_mtime_ns,
                reverse=True,
            )
            for old in snapshots[2:]:
                shutil.rmtree(old)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def healthy(self) -> bool:
        host = self.config.bind_host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1" if host == "0.0.0.0" else "::1"
        if ":" in host:
            host = f"[{host}]"
        try:
            with urllib.request.urlopen(
                f"http://{host}:{self.config.bind_port}/healthz", timeout=2
            ) as response:
                data = json.loads(response.read(4096))
                return (
                    response.status == 200
                    and isinstance(data, dict)
                    and data.get("instance") == self.instance
                    and (
                        not (self.state.get("pending") and self.state.get("require_radio_ready"))
                        or self.radio_ready()
                    )
                )
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def radio_ready(self) -> bool:
        host = self.config.bind_host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1" if host == "0.0.0.0" else "::1"
        if ":" in host:
            host = f"[{host}]"
        try:
            with urllib.request.urlopen(
                f"http://{host}:{self.config.bind_port}/readyz", timeout=2
            ) as response:
                return response.status == 200
        except OSError:
            return False

    def startup(self) -> bool:
        deadline = time.monotonic() + START_TIMEOUT
        healthy_since: float | None = None
        while not self.stopping and time.monotonic() < deadline:
            if self.child is None or self.child.poll() is not None:
                return False
            if self.healthy():
                healthy_since = healthy_since or time.monotonic()
                if time.monotonic() - healthy_since >= 5:
                    return True
            else:
                healthy_since = None
            time.sleep(0.25)
        return False

    def restore_previous(self, reason: str) -> None:
        previous = self.state.get("previous_python")
        if not previous:
            raise RuntimeError(reason)
        self.save(
            active_python=previous,
            active_revision=self.state.get("previous_revision"),
            pending=False,
            failed_revision=self.state.get("active_revision"),
            error=reason,
        )
        logger.error("%s; returning to previous application; user data is retained", reason)
        self.switch(previous)

    def check(self) -> None:
        previous_state = dict(self.state)
        had_child = self.child is not None
        try:
            config_bytes = self.path.read_bytes()
            configured = load_config(self.path)
            if not configured.auto_update:
                return
            if (
                replace(
                    configured,
                    auto_update=self.config.auto_update,
                    update_interval_seconds=self.config.update_interval_seconds,
                )
                != self.config
            ):
                self.save(
                    error="Configuration differs from the running host; restart before updating"
                )
                return
            sha, approved = approved_head()
            self.save(
                last_check=time.time(),
                target_revision=sha,
                ci_passed=approved,
                error=self.state.get("error") if sha == self.state.get("failed_revision") else None,
            )
            if not approved or sha in {
                self.state.get("active_revision"),
                self.state.get("failed_revision"),
            }:
                return
            logger.info("Preparing CI-approved BBS update %s while current service runs", sha)
            python = self.stage(sha)
            if self.stopping:
                return
            latest, still_approved = approved_head()
            if latest != sha or not still_approved or self.path.read_bytes() != config_bytes:
                self.save(
                    error="Head, CI, or configuration changed during preparation; retry later"
                )
                return
            self.valid_python(python)
            self.backup(sha, config_bytes)
            if self.path.read_bytes() != config_bytes or self.stopping:
                return
            self.save(
                previous_python=self.python,
                previous_revision=self.state.get("active_revision"),
                active_python=python,
                active_revision=sha,
                pending=True,
                require_radio_ready=self.child is not None and self.radio_ready(),
                error=None,
            )
            self.switch(python)
        except (
            OSError,
            EOFError,
            ValueError,
            KeyError,
            RuntimeError,
            sqlite3.Error,
            tarfile.TarError,
        ) as error:
            self.state = previous_state
            try:
                self.save(error=str(error)[:1000], last_check=time.time())
            except OSError:
                logger.exception("Could not record updater error; keeping the current application")
            logger.error("BBS update postponed: %s", error)
            if had_child and self.child is None and not self.stopping:
                # exec failed after the old child closed. Restart that same application.
                self.start_child()
                if not self.startup():
                    raise RuntimeError("Previous BBS application could not restart") from error
                self.ready = True

    def run(self) -> int:
        self.save(original_python=self.state.get("original_python", self.python))
        active = self.state.get("active_python", self.python)
        if self.state.get("pending") and active != self.python:
            # A candidate failed before its new supervisor could confirm startup.
            self.restore_previous("Candidate exited before startup confirmation")
        elif active != self.python:
            self.switch(active)
        self.save(
            active_python=self.python,
            active_revision=self.state.get("active_revision") or installed_revision(),
        )
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
            self.old_handlers[sig] = signal.signal(sig, self.on_signal)
        try:
            self.start_child()
            if not self.startup():
                if self.stopping:
                    return 0
                if self.state.get("pending"):
                    self.restore_previous("Updated service did not become healthy")
                raise RuntimeError("BBS service did not become healthy")
            if self.state.get("pending"):
                self.save(pending=False, last_update=time.time(), error=None)
                logger.info("BBS update healthy at %s", self.state.get("active_revision"))
            self.ready = True
            next_check = time.monotonic() + FIRST_CHECK_DELAY
            while not self.stopping:
                assert self.child is not None
                result = self.child.poll()
                if result is not None:
                    return result or 1
                if time.monotonic() >= next_check:
                    self.check()
                    next_check = time.monotonic() + self.config.update_interval_seconds
                time.sleep(0.25)
            return 0
        finally:
            self.stop_child()
            for old_sig, handler in self.old_handlers.items():
                signal.signal(old_sig, handler)


def supervise(path: Path, config: HostConfig) -> int:
    supervisor = UpdateSupervisor(path, config)
    lock_path = supervisor.root / "supervisor.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("An update supervisor is already running for this host") from error
        return supervisor.run()
    finally:
        os.close(descriptor)
