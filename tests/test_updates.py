"""Updates must never interrupt a working host before the replacement is prepared."""

import json
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest

from mesh_bbs.config import HostConfig, load_config, save_config
from mesh_bbs.store import Store
from mesh_bbs.updates import (
    UpdateSupervisor,
    approved_head,
    read_state,
    service_lock,
    update_command,
    write_state,
)

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def host(tmp_path):
    config = HostConfig("Update test", "test", tmp_path / "data", auto_update=True)
    path = tmp_path / "config.toml"
    save_config(config, path)
    supervisor = UpdateSupervisor(path, config)
    supervisor.save(
        active_python=supervisor.python, original_python=supervisor.python, active_revision=OLD
    )
    return supervisor


@pytest.mark.parametrize(
    "status,conclusion,expected",
    [
        ("completed", "success", True),
        ("in_progress", "", False),
        ("completed", "failure", False),
        ("completed", "cancelled", False),
        ("completed", "skipped", False),
    ],
)
def test_requires_latest_exact_main_push_ci(monkeypatch, status, conclusion, expected):
    runs = [
        dict(
            id=10,
            head_sha=NEW,
            head_branch="main",
            event="push",
            status=status,
            conclusion=conclusion,
        ),
        dict(
            id=9,
            head_sha=NEW,
            head_branch="main",
            event="push",
            status="completed",
            conclusion="success",
        ),
        dict(
            id=11,
            head_sha=NEW,
            head_branch="main",
            event="pull_request",
            status="completed",
            conclusion="success",
        ),
        dict(
            id=12,
            head_sha=OLD,
            head_branch="main",
            event="push",
            status="completed",
            conclusion="success",
        ),
    ]
    monkeypatch.setattr(
        "mesh_bbs.updates.api_json",
        lambda path: {"sha": NEW} if path == "/commits/main" else {"workflow_runs": runs},
    )
    assert approved_head() == (NEW, expected)


def test_invalid_or_missing_ci_cannot_approve(monkeypatch):
    monkeypatch.setattr("mesh_bbs.updates.api_json", lambda _: {"sha": "../../outside"})
    with pytest.raises(ValueError):
        approved_head()
    monkeypatch.setattr(
        "mesh_bbs.updates.api_json",
        lambda path: {"sha": NEW} if path == "/commits/main" else {"workflow_runs": []},
    )
    assert approved_head() == (NEW, False)


@pytest.mark.parametrize("sha,approved", [(NEW, False), (OLD, True)])
def test_no_install_or_restart_for_unapproved_or_current_commit(host, monkeypatch, sha, approved):
    monkeypatch.setattr("mesh_bbs.updates.approved_head", lambda: (sha, approved))
    monkeypatch.setattr(host, "stage", lambda _: pytest.fail("must not stage"))
    host.check()
    assert host.state["active_revision"] == OLD


def test_failed_stage_keeps_service_and_reports_error(host, monkeypatch):
    monkeypatch.setattr("mesh_bbs.updates.approved_head", lambda: (NEW, True))
    monkeypatch.setattr(host, "stage", lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(host, "stop_child", lambda: pytest.fail("must keep running"))
    host.check()
    assert host.state["active_revision"] == OLD
    assert read_state(host.config)["error"] == "offline"


@pytest.mark.parametrize("changed", ["head", "ci", "config"])
def test_changes_during_stage_postpone_cutover(host, monkeypatch, changed):
    answers = iter([(NEW, True), (OLD if changed == "head" else NEW, changed != "ci")])
    monkeypatch.setattr("mesh_bbs.updates.approved_head", lambda: next(answers))

    def stage(_):
        if changed == "config":
            host.path.write_text(host.path.read_text() + "\n# operator edit\n")
        return host.python

    monkeypatch.setattr(host, "stage", stage)
    monkeypatch.setattr(host, "switch", lambda _: pytest.fail("must not switch"))
    host.check()
    assert host.state["active_revision"] == OLD
    assert "changed" in host.state["error"]


def test_prepared_update_records_recovery_before_switch(host, monkeypatch):
    monkeypatch.setattr("mesh_bbs.updates.approved_head", lambda: (NEW, True))
    monkeypatch.setattr(host, "stage", lambda _: host.python)

    def switch(python):
        saved = read_state(host.config)
        assert saved["pending"] and saved["previous_revision"] == OLD
        assert saved["active_revision"] == NEW and python == host.python

    monkeypatch.setattr(host, "switch", switch)
    host.check()
    assert host.state["error"] is None


def test_rollback_keeps_user_database_and_quarantines_commit(host, monkeypatch):
    with closing(Store(host.config.data_dir / "bbs.sqlite3", "test")) as store:
        post = store.publish("local:a", "op", "general", "Keep me", "Do not restore an old copy")
    database = host.config.data_dir / "bbs.sqlite3"
    before = database.read_bytes()
    host.save(active_revision=NEW, previous_revision=OLD, previous_python=host.python, pending=True)
    monkeypatch.setattr(host, "switch", lambda _: None)
    host.restore_previous("startup failed")
    assert database.read_bytes() == before
    assert host.state["failed_revision"] == NEW and host.state["active_revision"] == OLD
    assert not host.state["pending"]
    monkeypatch.setattr("mesh_bbs.updates.approved_head", lambda: (NEW, True))
    monkeypatch.setattr(host, "stage", lambda _: pytest.fail("do not retry a failed candidate"))
    host.check()
    with closing(Store(database, "test")) as store:
        assert store.get_post(post.post_id).body == "Do not restore an old copy"


def test_disable_preserves_active_version_and_stops_checks(host, monkeypatch):
    before = host.path.read_bytes()
    assert update_command("disable", host.path, host.config, before) == 0
    assert not load_config(host.path).auto_update
    assert read_state(host.config)["active_revision"] == OLD
    monkeypatch.setattr("mesh_bbs.updates.approved_head", lambda: pytest.fail("disabled"))
    host.check()


def test_preflight_uses_copy_without_opening_radios(host, monkeypatch):
    with closing(Store(host.config.data_dir / "bbs.sqlite3", "test")) as store:
        original = store.publish("local:a", "op", "general", "Original", "Still here")
    before = (host.config.data_dir / "bbs.sqlite3").read_bytes()

    def run(args):
        assert args[-1] == "doctor"
        config = load_config(Path(args[-2]))
        assert config.data_dir != host.config.data_dir and not config.auto_update
        with closing(Store(config.data_dir / "bbs.sqlite3", "test")) as candidate:
            candidate.remove("migration-test", original.post_id)

    monkeypatch.setattr(host, "run_tool", run)
    host.preflight(host.python)
    assert (host.config.data_dir / "bbs.sqlite3").read_bytes() == before
    assert not list(host.root.glob("preflight-*"))


def test_prune_retains_current_previous_and_new_candidate(host):
    host.save(active_revision=OLD, previous_revision="c" * 40)
    for sha in (OLD, NEW, "c" * 40, "d" * 40):
        folder = host.root / "releases" / sha
        folder.mkdir(parents=True)
        (folder / "marker").touch()
    host.prune(NEW)
    assert {p.name for p in (host.root / "releases").iterdir()} == {OLD, NEW, "c" * 40}


def test_service_lock_refuses_overlapping_instances(host):
    with service_lock(host.config), pytest.raises(RuntimeError, match="already owns"):
        with service_lock(host.config):
            pytest.fail("duplicate service")


def test_private_atomic_state_and_invalid_file(host):
    assert (host.root / "state.json").stat().st_mode & 0o777 == 0o600
    (host.root / "state.json").write_text("[]")
    with pytest.raises(ValueError, match="state"):
        read_state(host.config)


def test_real_supervisor_starts_and_stops_service_without_network_updates(tmp_path):
    # Disabled updates still supervise the selected installation; no GitHub/radio traffic.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = HostConfig("Lifecycle", "test", tmp_path / "data", bind_port=port)
    path = tmp_path / "config.toml"
    save_config(config, path)
    write_state(config, {"active_python": sys.executable, "original_python": sys.executable})
    process = subprocess.Popen(
        [sys.executable, "-m", "mesh_bbs.cli", "--config", str(path), "serve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=0.5
                ) as response:
                    assert json.load(response)["instance"]
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("no health response")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
        with service_lock(config):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_staging_installs_exact_commit_outside_original_environment(host, monkeypatch):
    calls = []
    monkeypatch.setattr(host, "run_tool", lambda args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(host, "preflight", lambda python: calls.append((python, {})))
    project = host.root / "releases" / NEW / "source" / ("mesh-bbs-" + NEW)
    monkeypatch.setattr(
        "mesh_bbs.updates.download_source",
        lambda sha, release: project if sha == NEW else pytest.fail("wrong commit"),
    )
    python = host.stage(NEW)
    args, options = calls[0]
    assert args[-1] == str(project)
    assert "--locked" in args and "--managed-python" in args and "--no-dev" in args
    assert options["env"]["UV_PROJECT_ENVIRONMENT"] == str(host.root / "releases" / NEW / "venv")
    assert python != host.python and str(host.root) in python
    assert calls[-1][0] == python


def test_bad_config_during_periodic_check_does_not_stop_service(host, monkeypatch):
    host.path.write_text("not valid toml")
    monkeypatch.setattr(host, "stop_child", lambda: pytest.fail("do not stop service"))
    host.check()
    assert host.state["error"] and host.state["active_revision"] == OLD


def test_supervised_child_exits_when_parent_pipe_disappears(tmp_path):
    import os

    config = HostConfig("Orphan test", "test", tmp_path / "data", bind_port=unused_port())
    path = tmp_path / "config.toml"
    save_config(config, path)
    process = subprocess.Popen(
        [sys.executable, "-m", "mesh_bbs.cli", "--config", str(path), "serve", "--no-auto-update"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(os.environ, MESH_BBS_SUPERVISED="1", MESH_BBS_INSTANCE="test-orphan"),
    )
    try:
        wait_for_host(process, config.bind_port)
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        with service_lock(config):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_host(process, port):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert process.poll() is None
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=0.5
            ) as response:
                if json.load(response).get("instance"):
                    return
        except OSError:
            pass
        time.sleep(0.1)
    pytest.fail("no health response")


def test_interrupted_candidate_recovers_previous_installation_on_service_restart(tmp_path):
    config = HostConfig("Recovery test", "test", tmp_path / "data", bind_port=unused_port())
    path = tmp_path / "config.toml"
    save_config(config, path)
    write_state(
        config,
        {
            "active_python": str(config.data_dir / "updates/releases" / NEW / "bin/python"),
            "active_revision": NEW,
            "pending": True,
            "original_python": sys.executable,
            "previous_python": sys.executable,
            "previous_revision": OLD,
        },
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "mesh_bbs.cli", "--config", str(path), "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_host(process, config.bind_port)
        state = read_state(config)
        assert state["active_revision"] == OLD and state["failed_revision"] == NEW
        assert not state["pending"] and "startup confirmation" in state["error"]
    finally:
        process.terminate()
        assert process.wait(timeout=10) == 0


def source_archive(extra_name=None, kind=None):
    import io
    import tarfile

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name in (f"mesh-bbs-{NEW}/uv.lock", f"mesh-bbs-{NEW}/pyproject.toml"):
            member = tarfile.TarInfo(name)
            member.size = 4
            archive.addfile(member, io.BytesIO(b"test"))
        if extra_name:
            member = tarfile.TarInfo(extra_name)
            member.type = kind or tarfile.REGTYPE
            member.linkname = "/tmp/outside"
            archive.addfile(member)
    return stream.getvalue()


@pytest.mark.parametrize(
    "path,kind",
    [
        (f"mesh-bbs-{NEW}/../../outside", None),
        ("/tmp/outside", None),
        (f"mesh-bbs-{NEW}/link", b"2"),
        (f"mesh-bbs-{NEW}/hardlink", b"1"),
        (f"mesh-bbs-{NEW}/device", b"3"),
        ("other-repo/uv.lock", None),
    ],
)
def test_source_extraction_rejects_paths_links_and_devices(tmp_path, path, kind):
    from mesh_bbs.updates import unpack_source

    with pytest.raises(ValueError, match="Unsafe"):
        unpack_source(source_archive(path, kind), NEW, tmp_path)
    assert not (tmp_path.parent / "outside").exists()


def test_source_extraction_requires_expected_revision_and_bounds_expansion(tmp_path, monkeypatch):
    from mesh_bbs.updates import unpack_source

    body = source_archive()
    project = unpack_source(body, NEW, tmp_path)
    assert project.name == "mesh-bbs-" + NEW and (project / "uv.lock").read_text() == "test"
    monkeypatch.setattr("mesh_bbs.updates.MAX_SOURCE_BYTES", 128)
    with pytest.raises(ValueError, match="Expanded"):
        unpack_source(body, NEW, tmp_path / "small")


def test_reset_requires_stopped_service_and_preserves_database(host):
    with closing(Store(host.config.data_dir / "bbs.sqlite3", "test")) as store:
        store.publish("a", "op", "general", "Keep", "Body")
    before = (host.config.data_dir / "bbs.sqlite3").read_bytes()
    with service_lock(host.config), pytest.raises(RuntimeError):
        update_command("reset", host.path, host.config, host.path.read_bytes())
    assert read_state(host.config)
    assert update_command("reset", host.path, host.config, host.path.read_bytes()) == 0
    assert not read_state(host.config)
    assert (host.config.data_dir / "bbs.sqlite3").read_bytes() == before


def test_update_snapshots_are_private_consistent_and_bounded(host):
    with closing(Store(host.config.data_dir / "bbs.sqlite3", "test")) as store:
        post = store.publish("local:a", "op", "general", "Keep", "Snapshot content")
        for index in range(3):
            host.backup(str(index) * 40, host.path.read_bytes())
    snapshots = list((host.root / "backups").iterdir())
    assert len(snapshots) == 2
    latest = Path(host.state["backup"])
    assert latest.stat().st_mode & 0o777 == 0o700
    assert (latest / "config.toml").read_bytes() == host.path.read_bytes()
    assert (latest / "bbs.sqlite3").stat().st_mode & 0o777 == 0o600
    with closing(Store(latest / "bbs.sqlite3", "test")) as snapshot:
        assert snapshot.get_post(post.post_id).body == "Snapshot content"


def test_state_disk_failure_keeps_running_service(host, monkeypatch):
    monkeypatch.setattr("mesh_bbs.updates.approved_head", lambda: (NEW, True))
    monkeypatch.setattr(host, "save", lambda **_: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(host, "stop_child", lambda: pytest.fail("keep running"))
    host.check()
    assert host.state["active_revision"] == OLD


def test_exec_failure_restarts_previous_child(host, monkeypatch):
    monkeypatch.setattr("mesh_bbs.updates.approved_head", lambda: (NEW, True))
    monkeypatch.setattr(host, "stage", lambda _: host.python)
    monkeypatch.setattr(host, "radio_ready", lambda: True)
    host.child = object()
    calls = []

    def switch(_):
        host.child = None
        raise OSError("candidate disappeared")

    monkeypatch.setattr(host, "switch", switch)
    monkeypatch.setattr(host, "start_child", lambda: calls.append("restart previous"))
    monkeypatch.setattr(host, "startup", lambda: True)
    host.check()
    assert calls == ["restart previous"]
    assert host.state["active_revision"] == OLD and not host.state.get("pending")


def test_changed_running_configuration_requires_operator_restart(host, monkeypatch):
    from dataclasses import replace

    from mesh_bbs.config import update_config

    update_config(replace(host.config, name="Changed"), host.path, host.path.read_bytes())
    monkeypatch.setattr(
        "mesh_bbs.updates.approved_head", lambda: pytest.fail("pending config edit")
    )
    host.check()
    assert "restart" in host.state["error"]
    assert host.state["active_revision"] == OLD


@pytest.mark.parametrize("fails_startup", [False, True])
def test_real_exec_cutover_and_failed_startup_keep_posts(tmp_path, fails_startup):
    import os
    import sysconfig

    config = HostConfig(
        "Cutover", "test", tmp_path / "data", bind_port=unused_port(), auto_update=True
    )
    path = tmp_path / "config.toml"
    save_config(config, path)
    with closing(Store(config.data_dir / "bbs.sqlite3", "test")) as store:
        post = store.publish("local:a", "keep", "general", "Before update", "Keep this post")
    # A separate interpreter prefix exercises exec/startup without downloading packages.
    release = config.data_dir / "updates/releases" / NEW / "venv"
    (release / "bin").mkdir(parents=True)
    python = release / "bin/python"
    python.symlink_to(sys.executable)
    (release / "pyvenv.cfg").write_bytes((Path(sys.prefix) / "pyvenv.cfg").read_bytes())
    library = release / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    library.mkdir(parents=True)
    (library / "site-packages").symlink_to(sysconfig.get_path("purelib"), target_is_directory=True)
    injection = tmp_path / "test-hooks"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os, sys\n"
        + (
            f"if os.environ.get('MESH_BBS_SUPERVISED') and '{NEW}' in sys.executable:\n"
            "    raise SystemExit(9)\n"
            if fails_startup
            else ""
        )
    )
    script = (
        "import sys\nfrom pathlib import Path\nimport mesh_bbs.updates as u\n"
        "from mesh_bbs.config import load_config\n"
        "p=Path(sys.argv[1]); c=load_config(p)\n"
        "u.write_state(c, {'active_python':sys.executable,'original_python':sys.executable,"
        f"'active_revision':'{OLD}'}})\n"
        "u.FIRST_CHECK_DELAY=0\n"
        f"u.approved_head=lambda: ('{NEW}',True)\n"
        "u.UpdateSupervisor.stage=lambda self,sha: sys.argv[2]\n"
        "raise SystemExit(u.supervise(p,c))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(injection))
    with (tmp_path / "process.log").open("wb") as logs:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(path), str(python)],
            env=env,
            stdout=logs,
            stderr=logs,
        )
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                assert process.poll() is None, (tmp_path / "process.log").read_text()
                state = read_state(config)
                finished = (
                    state.get("failed_revision") == NEW
                    if fails_startup
                    else state.get("active_revision") == NEW and state.get("last_update")
                )
                if finished and not state.get("pending"):
                    break
                time.sleep(0.1)
            else:
                pytest.fail((tmp_path / "process.log").read_text())
            wait_for_host(process, config.bind_port)
            assert read_state(config)["active_revision"] == (OLD if fails_startup else NEW)
            with closing(Store(config.data_dir / "bbs.sqlite3", "test")) as store:
                assert store.get_post(post.post_id).body == "Keep this post"
        finally:
            process.terminate()
            try:
                assert process.wait(timeout=10) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
