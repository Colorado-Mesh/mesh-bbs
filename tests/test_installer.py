"""Exercise the real shell installer with fake tools and no network or installs."""

from __future__ import annotations

import os
import pty
import select
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


def executable(path: Path, text: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def fake_install(tmp_path: Path) -> tuple[dict[str, str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "installed bin"
    bin_dir.mkdir()
    executable(tools / "id", 'printf "%s\\n" "${TEST_UID:-501}"\n')
    executable(
        tools / "uname",
        'case "$1" in -m) printf "%s\\n" "${TEST_ARCH:-arm64}" ;; '
        '*) printf "%s\\n" "${TEST_SYSTEM:-Linux}" ;; esac\n',
    )
    executable(
        tools / "uv",
        """printf '%s\\n' "$@" > "$TEST_LOG"
printf '%s\\n' "${OPENSSL_DIR:-}" > "$TEST_OPENSSL_LOG"
if [ "${TEST_UV_FAIL:-0}" = 1 ]; then exit 17; fi
printf '#!/bin/sh\\nprintf "setup ran" > "$TEST_SETUP_LOG"\\n' > "$UV_TOOL_BIN_DIR/mesh-bbs"
chmod +x "$UV_TOOL_BIN_DIR/mesh-bbs"
""",
    )
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{tools}:/usr/bin:/bin",
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "TEST_LOG": str(tmp_path / "uv.log"),
            "TEST_SETUP_LOG": str(tmp_path / "setup.log"),
            "TEST_OPENSSL_LOG": str(tmp_path / "openssl.log"),
        }
    )
    for variable in (
        "MESH_BBS_REF",
        "MESH_BBS_EXTRAS",
        "TEST_UID",
        "TEST_SYSTEM",
        "TEST_UV_FAIL",
        "TEST_ARCH",
        "OPENSSL_DIR",
    ):
        env.pop(variable, None)
    return env, tmp_path


def install(env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(INSTALLER), *arguments],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        start_new_session=True,
    )


def test_installer_uses_isolated_python_and_does_not_run_setup_noninteractively(
    fake_install: tuple[dict[str, str], Path],
) -> None:
    env, temp = fake_install
    result = install(env, "--no-setup")
    assert result.returncode == 0, result.stderr
    assert (temp / "uv.log").read_text().splitlines() == [
        "tool",
        "install",
        "--managed-python",
        "--python",
        "3.12",
        "--reinstall",
        "mesh-bbs[reticulum,meshcore,meshtastic] @ "
        "https://github.com/Colorado-Mesh/mesh-bbs/archive/main.tar.gz",
    ]
    assert not (temp / "setup.log").exists()
    assert "setup" in result.stdout
    assert "Shell startup files have not been changed" in result.stdout


def test_pipe_without_controlling_terminal_prints_setup_command(
    fake_install: tuple[dict[str, str], Path],
) -> None:
    env, temp = fake_install
    result = install(env)
    assert result.returncode == 0, result.stderr
    assert not (temp / "setup.log").exists()
    assert "Run setup in a terminal" in result.stdout


def test_installer_accepts_pinned_ref_and_macos(fake_install: tuple[dict[str, str], Path]) -> None:
    env, temp = fake_install
    env["TEST_SYSTEM"] = "Darwin"
    env["MESH_BBS_REF"] = "from-environment"
    result = install(env, "--no-setup", "--ref", "abc123")
    assert result.returncode == 0, result.stderr
    assert "/archive/abc123.tar.gz" in (temp / "uv.log").read_text()


def intel_mac_tools(env: dict[str, str], temp: Path) -> Path:
    env.update(TEST_SYSTEM="Darwin", TEST_ARCH="x86_64")
    executable(temp / "tools/clang", "cat >/dev/null\n")
    executable(temp / "tools/cargo", "printf 'cargo 1.98.0\\n'\n")
    executable(temp / "tools/rustc", "printf 'rustc 1.98.0 (test)\\n'\n")
    prefix = temp / "openssl with spaces"
    (prefix / "include/openssl").mkdir(parents=True)
    (prefix / "include/openssl/ssl.h").touch()
    executable(temp / "tools/brew", f"printf '%s\\n' {shlex.quote(str(prefix))}\n")
    return prefix


@pytest.mark.parametrize("custom_openssl", [False, True])
def test_intel_macos_forwards_source_build_dependencies(fake_install, custom_openssl):
    env, temp = fake_install
    prefix = intel_mac_tools(env, temp)
    if custom_openssl:
        env["OPENSSL_DIR"] = str(prefix)
        executable(temp / "tools/brew", "exit 99\n")
    result = install(env, "--no-setup")
    assert result.returncode == 0, result.stderr
    assert (temp / "openssl.log").read_text().strip() == str(prefix)
    assert "compiling cryptography from source" in result.stdout


@pytest.mark.parametrize("broken", ["clang", "cargo", "rustc", "old-rust", "openssl"])
def test_intel_macos_missing_dependencies_fail_before_install(fake_install, broken):
    env, temp = fake_install
    prefix = intel_mac_tools(env, temp)
    if broken == "openssl":
        (prefix / "include/openssl/ssl.h").unlink()
    elif broken == "old-rust":
        executable(temp / "tools/rustc", "printf 'rustc 1.82.0 (test)\\n'\n")
    else:
        executable(temp / "tools" / broken, "exit 1\n")
    result = install(env, "--no-setup")
    assert result.returncode != 0
    assert "Intel Macs need" in result.stderr
    assert not (temp / "uv.log").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ("--ref", "../../other"),
        ("--ref",),
        ("--unknown",),
        ("--extras",),
        ("--extras", "unknown"),
        ("--extras", "meshcore,"),
        ("--extras", ",reticulum"),
        ("--extras", "reticulum,,meshtastic"),
    ],
)
def test_bad_installer_arguments_do_not_install(
    fake_install: tuple[dict[str, str], Path],
    arguments: tuple[str, ...],
) -> None:
    env, temp = fake_install
    assert install(env, *arguments).returncode != 0
    assert not (temp / "uv.log").exists()


@pytest.mark.parametrize("variable,value", [("TEST_UID", "0"), ("TEST_SYSTEM", "FreeBSD")])
def test_unsupported_invocation_does_not_install(
    fake_install: tuple[dict[str, str], Path],
    variable: str,
    value: str,
) -> None:
    env, temp = fake_install
    env[variable] = value
    assert install(env, "--no-setup").returncode != 0
    assert not (temp / "uv.log").exists()


def test_package_install_failure_is_preserved(fake_install: tuple[dict[str, str], Path]) -> None:
    env, temp = fake_install
    env["TEST_UV_FAIL"] = "1"
    assert install(env, "--no-setup").returncode == 17
    assert not (temp / "setup.log").exists()


@pytest.mark.parametrize(
    "extras,prefix",
    [
        ("none", "mesh-bbs @"),
        ("reticulum", "mesh-bbs[reticulum] @"),
        ("meshcore,meshtastic", "mesh-bbs[meshcore,meshtastic] @"),
    ],
)
def test_installer_allows_selected_protocol_dependencies(
    fake_install: tuple[dict[str, str], Path],
    extras: str,
    prefix: str,
) -> None:
    env, temp = fake_install
    env["MESH_BBS_EXTRAS"] = "invalid-environment-overridden-by-cli"
    result = install(env, "--no-setup", "--extras", extras)
    assert result.returncode == 0, result.stderr
    assert (temp / "uv.log").read_text().splitlines()[-1].startswith(prefix)


def fake_uv_download(env: dict[str, str], temp: Path) -> None:
    template = temp / "uv-template"
    (temp / "tools/uv").rename(template)
    env["TEST_UV_TEMPLATE"] = str(template)
    env["TEST_BOOTSTRAP_LOG"] = str(temp / "bootstrap.log")
    env["TMPDIR"] = str(temp)
    executable(
        temp / "tools/curl",
        """if [ "${TEST_CURL_FAIL:-0}" = 1 ]; then exit 22; fi
while [ "$#" -gt 0 ]; do
    if [ "$1" = '-o' ]; then shift; output=$1; break; fi
    shift
done
cat > "$output" <<'SCRIPT'
#!/bin/sh
set -eu
printf '%s\\n' "$UV_NO_MODIFY_PATH" > "$TEST_BOOTSTRAP_LOG"
cp "$TEST_UV_TEMPLATE" "$UV_INSTALL_DIR/uv"
SCRIPT
""",
    )


def test_bootstrapping_uv_disables_profile_edits_and_cleans_temp_files(
    fake_install: tuple[dict[str, str], Path],
) -> None:
    env, temp = fake_install
    fake_uv_download(env, temp)
    result = install(env, "--no-setup")
    assert result.returncode == 0, result.stderr
    assert (temp / "bootstrap.log").read_text().strip() == "1"
    assert list(temp.glob("mesh-bbs-install.*")) == []


def test_failed_uv_download_does_not_run_partial_script(
    fake_install: tuple[dict[str, str], Path],
) -> None:
    env, temp = fake_install
    fake_uv_download(env, temp)
    env["TEST_CURL_FAIL"] = "1"
    assert install(env, "--no-setup").returncode == 22
    assert not (temp / "bootstrap.log").exists()
    assert not (temp / "uv.log").exists()
    assert list(temp.glob("mesh-bbs-install.*")) == []


def test_printed_setup_command_quotes_user_paths(fake_install: tuple[dict[str, str], Path]) -> None:
    env, temp = fake_install
    special = temp / "host's tools"
    special.mkdir()
    env["UV_TOOL_BIN_DIR"] = str(special)
    result = install(env, "--no-setup")
    assert result.returncode == 0, result.stderr
    command = result.stdout.splitlines()[-1]
    assert shlex.split(command) == [str(special / "mesh-bbs"), "setup"]


@pytest.mark.skipif(os.name != "posix", reason="installer requires POSIX")
def test_piped_installer_launches_setup_on_controlling_terminal(
    fake_install: tuple[dict[str, str], Path],
) -> None:
    env, temp = fake_install
    # Protocol SDKs may leave dispatcher threads alive in the pytest process.
    result = subprocess.run(
        [sys.executable, str(Path(__file__))],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (temp / "setup.log").read_text() == "setup ran"


def _piped_install(env: dict[str, str]) -> None:
    child, terminal = pty.fork()
    if child == 0:
        # The installer itself is piped in, as in the documented one-liner.
        os.execvpe(
            "/bin/sh",
            ["/bin/sh", "-c", 'cat "$TEST_INSTALLER" | sh'],
            {**env, "TEST_INSTALLER": str(INSTALLER)},
        )
    deadline = time.monotonic() + 10
    status = None
    output = bytearray()
    try:
        while time.monotonic() < deadline:
            done, candidate = os.waitpid(child, os.WNOHANG)
            if done:
                status = candidate
                break
            readable, _, _ = select.select([terminal], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(terminal, 65536))
                except OSError:
                    pass  # PTYs may signal EOF with EIO after the child exits.
    finally:
        if status is None:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        os.close(terminal)
    assert status == 0, output.decode(errors="replace")


if __name__ == "__main__":
    _piped_install(dict(os.environ))
