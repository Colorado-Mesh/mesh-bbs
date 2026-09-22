# Radio recovery and protocol verification

The first pilot tests called simulated SDK objects. They proved application
contracts but did not exercise the real SDK handshake, framing, or callback
timing. Runtime startup also awaited each radio once: a missing radio stopped
web access and feed polling, while a later disconnect had no service-owned
recovery path.

This improvement keeps one owner for each radio connection. The service will
retry a missing or disconnected radio with bounded exponential backoff, keeping
the same persisted airtime budget between connections. Web reading and feed
polling remain available during a radio outage. Reticulum's process-wide runtime
continues to have its separate lifecycle.

## Acceptance

- [x] Missing radios do not prevent web access or newsletter polling.
- [x] A disconnected radio is closed before a replacement is created.
- [x] Repeated failures back off; shutdown interrupts the retry wait.
- [x] Reconnects preserve the persisted reply budget and application receipts.
- [x] `/healthz` describes HTTP liveness; `/readyz` reports degraded configured
      radio access without exposing device paths, peer addresses, or message text.
- [x] Real MeshCore SDK connects to a TCP companion-protocol emulator and
      completes publishing, idempotent retries, and reading.
- [x] Real Meshtastic SDK connects to a protobuf TCP radio emulator and
      completes the same command flow with bounded acknowledgement handling.
- [x] Core review findings, if reproduced, have focused regression tests.
Handoff requires full local checks and a passing current-commit CI run.
The [main branch runs](https://github.com/Colorado-Mesh/mesh-bbs/actions/workflows/ci.yml)
record that gate for each pushed commit.

The emulators implement host-to-radio wire protocols from the installed pinned
SDKs. They are test peers, not firmware CPU emulators. They do not establish
over-the-air delivery, RF airtime, regulatory compliance, or hardware coverage.
Tests use temporary databases and loopback ports exclusively.

## Verification

Unit tests cover retry timing, repeated failures, cleanup, readiness transitions,
and shutdown. Integration tests use real protocol libraries and byte streams to
catch differences that a mocked SDK method cannot reveal. Existing three-host
Reticulum integration, package installation, lint, typing, and distribution
checks remain required. Independent review targets lost replies, stable thread
references, dedupe, and trust boundaries.

## Review findings addressed

Radio startup failures previously discarded the client reference before cleanup
finished. A cleanup exception could then cause a replacement to open beside a
still-live old connection. Adapters now retain that reference until closure
succeeds, and the supervisor reports cleanup failures instead of retrying them.

Worker shutdown could also consume cancellation intended for the supervisor.
Cancellation now propagates, and readiness changes before connection closure
finishes. SDK cleanup that consumes cancellation cannot restart the radio.

A separate core audit reproduced a collision in the short random draft ID.
Creation retries a bounded number of collisions while preserving the existing
draft and its publication receipt.

The web listener also bypasses the standard HTTP server's reverse-DNS lookup:
an IP listener can start when name service is unavailable. Its regression test
fails with the default listener and passes with the numeric address retained.

The dependency floor moves to cryptography 50, with 50.0.1 locked, to include
the fix for [GHSA-g6cj-pr64-35w5](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5).
The affected PKCS#7 envelope decryption functions are not used in BBS, RNS, or
LXMF code; signed-event and real Reticulum integration tests verify the upgrade.
Because current cryptography wheels cover Apple Silicon only on macOS, Intel
installs check native compiler, Rust, and OpenSSL prerequisites before installing.
The CI matrix includes an Intel Mac source build and the real installer with
every protocol extra. Linux and Apple Silicon retain the same install command.
