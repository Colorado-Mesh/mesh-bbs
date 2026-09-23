# Develop and verify a change

Use Python 3.12+ and uv. Read [AGENTS.md](../AGENTS.md) before changing the core,
permissions, adapters, or operational behavior. Development does not require a
physical radio or an installed Mesh Client profile.

## Prepare the environment

```sh
uv sync --locked --all-extras --group dev --group browser
uv run --no-sync playwright install chromium
```

The browser group is only for tests. Runtime web pages ship in the Python package
and need no frontend build. Intel macOS needs the [crypto build tools](install.md#intel-macs).

## Run checks

```sh
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
shellcheck install.sh
uv run --no-sync python -m hatchling build
```

Run the test groups in separate processes:

```sh
uv run --no-sync pytest -q -m 'not integration' --ignore=tests/browser
uv run --no-sync pytest -q tests/test_radio_adapters.py tests/test_reticulum_adapter.py tests/integration
uv run --no-sync pytest -q tests/browser
```

The core suite includes process and PTY tests. Keep it separate from the real
radio SDK tests, whose dispatcher threads may persist after a connection closes.
Browser tests use module-scoped synchronous Playwright fixtures.

For a core-only environment, omit `--all-extras` and `--group browser` during
sync. The core and packaged CLI must work without protocol libraries. Run the
relevant focused regression first, then the full affected groups. Preserve
normal commit hooks and check CI for the exact pushed commit.

## What the tests establish

| Tests | Evidence | Limits |
| --- | --- | --- |
| Core and wheel | Transactions, permissions, retries, restart, CLI installation, feeds, packet terminal | No RF delivery |
| Radio adapter contracts | Queue limits, parsing, scheduling, recovery | Simulated SDK responses |
| MeshCore/Meshtastic TCP emulators | Real SDK handshake, wire framing, callbacks, lost ACKs, DM commands | Test protocol peers, not firmware CPU emulators |
| Isolated Reticulum | Real links, three-host convergence, LXMF, NomadNet, interruption/restart | Temporary identities and loopback interfaces |
| Chromium | Real HTTP/SQLite publication, mobile layout, drafts, retries, sign-in | Not every browser or device |

Tests use temporary data and local interfaces. Never silently replace them with
a live configuration. Field pilots must separately establish coverage, modem
estimates, busy-channel behavior, and hardware recovery.

## CI and documentation

[CI](../.github/workflows/ci.yml) runs core and wheel checks on Ubuntu with
Python 3.12, 3.13, and 3.14, plus Python 3.12 on Apple Silicon and Intel macOS.
The Intel job also exercises the installer and native crypto build. Separate
jobs cover formatting/types/distributions, optional protocols, and Chromium.
Dependency resolution uses `uv.lock`; Actions use pinned commits. Superseded
branch runs cancel. Build artifacts are test outputs, not automatic releases.

When changing behavior, update the reader guide, operator instructions, and
sample configuration that describe it. Check relative links and execute CLI
examples against a temporary config. Avoid recording a test count as a permanent
quality claim; the tests and current-commit CI are the evidence.

GPL-3.0-or-later applies to the project. Keep the standard [LICENSE](../LICENSE)
text intact.
