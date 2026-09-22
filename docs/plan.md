# Implementation plan

## Goal

Build a dependable, extensible BBS for Colorado Mesh and other communities.
Newsletter delivery is the first end-to-end use case. Users must be able to
read and reply through different protocols while hosts synchronize the same
posts without duplicate issues or replies pointing at unrelated messages.

## Decisions

- Standalone Python service with SQLite per host; no Mesh Client dependency.
- Optional protocol adapters wrap one shared command service.
- Signed immutable events identify their originating host. The first federation
  is explicitly trusted, with board grants configured by each operator.
- Transport identities remain namespaced; display names never establish identity.
- Feed source IDs and item IDs identify imported articles across failover hosts.
- Radio browsing retrieves a bounded page at a time. Reading cursors pin a
  revision; drafts use numbered parts and idempotent publication.
- Public discovery channels do not carry complete newsletters.
- Ham packet support uses a separate operator-managed cleartext gateway;
  encrypted Reticulum federation is not implicitly authorized on amateur bands.

## Milestones and acceptance criteria

### 1. Repository and durable core

- [x] Reproducible Python environment, lint, typing, tests, package build, CI.
- [x] Boards, posts, stable parent references, revisions, tombstones.
- [x] Atomic acceptance and deduplication survive concurrent retries and restart.
- [x] Persistent drafts and byte-bounded reading cursors.
- [x] A CLI can initialize a host and demonstrate reading and publishing.

### 2. Newsletters

- [x] RSS and Atom parse from bounded input with safe text conversion.
- [x] Repeated imports create one issue; corrections retain its identity.
- [x] Summary-only feeds are labeled; no promise of unavailable full text.
- [x] Configured feed polling supports conditional requests and failure backoff.
- [x] Web browsing and RSS export expose the same saved public posts.
- [x] Official newsletter posting permissions differ from community replies.

### 3. Federation and Reticulum access

- [x] Trusted-peer inventory and missing-event transfer with signatures and limits.
- [x] Three disconnected hosts converge after reordered/repeated exchanges.
- [x] Missing parents and removal-before-create do not corrupt thread identity.
- [x] NomadNet browsing and LXMF commands use the shared content and permissions.
- [x] Real local Reticulum integration runs with isolated temporary profiles.

### 4. Radio adapters and operations

- [x] MeshCore companion DM adapter; no Room Server dependency.
- [x] Meshtastic DM adapter; ignore channel broadcasts by default.
- [x] Receive parsing, identity handling, payload limits, and retry behavior tested.
- [x] Bounded transmission queues and conservative scheduling.
- [x] Example configuration, service installation, backup and restore, health checks.
- [x] Protocol plugin documentation and a cleartext packet gateway interface.

### 5. Pilot readiness

- [x] Installation smoke test from the built package.
- [x] Security and architecture review findings addressed.
- [ ] Current-commit GitHub CI passes.
- [x] Known limits, hardware pilot instructions, and remaining work documented.

Actual MeshCore/Meshtastic RF coverage, traffic behavior under a busy community
mesh, and licensed ham station operation require an operator-run field pilot.
Local tests must never be described as field validation.

## Research

- https://github.com/markqvist/Reticulum
- https://github.com/markqvist/LXMF
- https://github.com/markqvist/NomadNet
- https://github.com/meshcore-dev/meshcore_py
- https://github.com/meshcore-dev/MeshCore/blob/main/docs/companion_protocol.md
- https://meshtastic.org/docs/development/python/library/
- https://www.rssboard.org/rss-specification
- https://www.rfc-editor.org/rfc/rfc4287
- https://docs.python.org/3/library/sqlite3.html

## Validation evidence

The tests exercise concurrent publication retries, process restart, out-of-order
replication, removal and revision permissions, byte-limited reading, feed polling,
terminal board policy, and persistent reply budgets. Wheel tests install outside
the source tree and exercise initialization, newsletter publication, reading,
and backup.

The full Reticulum integration starts three independent hosts with temporary
identities and real loopback links. It checks independently created posts,
64 KiB UTF-8 content, stable reply parents, duplicate newsletter import,
interrupted synchronization, signed LXMF commands and retries, restart, and
NomadNet views. Radio adapters use simulated devices; no RF testing is claimed.

Independent code review covered transaction boundaries, identity and board
permissions, parsing, shutdown, and bounded work. Its findings have regression
tests. Repository CI results are available in the Actions tab.
