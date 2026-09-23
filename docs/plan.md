# Project status and remaining work

Mesh BBS serves a community's boards through several protocols while preserving
post identity and reply relationships. Colorado Mesh newsletter delivery is the
first deployment use case. Other communities can install the same service,
choose their region, and explicitly pair trusted hosts.

This file records scope and acceptance criteria. Installation commands belong
in [installation](install.md); current behavior belongs in the linked guides.

## Implemented

| Area | Available behavior | Reference |
| --- | --- | --- |
| Storage | Signed events, stable IDs/parents, revisions, removals, atomic receipts, durable drafts | [Architecture](architecture.md) |
| Reading and writing | Shared DM menus, community boards, long posts, replies, explicit safe retries, local notice shortcuts | [Commands](commands.md) |
| News | RSS/Atom polling, published newsletter Markdown, full-text storage, correction dedupe, RSS export; import-only News | [Operations](operations.md#publish-newsletters) |
| Reticulum | Explicit trusted peers, interrupted-sync recovery, LXMF commands, NomadNet pages and scheduled announces | [Community setup](community-setup.md) |
| Radios | MeshCore companion and Meshtastic DMs, bounded queues/budgets, reconnects, designated channel notices/help | [Recovery design](reliability-plan.md) |
| Web | Public reading, contributor keys, boards, posts/replies, previews, local drafts and retry protection | [Web design](web-gui-plan.md) |
| Setup | One-line installer, region/connection wizard, public peer cards, example service, backup/recovery | [Installation](install.md) |
| Packet access | Cleartext terminal session with explicit boards and optional community posting | [Transport integration](transports.md) |

The implementation uses stock companion/client firmware. A region is a
namespace, not automatic federation membership. Hosts grant trust explicitly;
transport identities remain separate from a person's display names.

## Acceptance criteria to preserve

- Retries and restarts must not publish a second copy of the same operation.
  Identical independent submissions must remain distinct.
- Replies must retain their original parent IDs through missing, reordered,
  corrected, or removed content.
- Hosts must converge after repeated bounded exchanges without trusting an
  unknown origin merely because a known relay forwarded its event.
- Browsing must stay within link budgets; reading a large article must not
  enqueue the entire article over a public channel.
- Radio outages must leave web reading and feed polling usable, while keeping
  budgets and request receipts intact.
- Setup must work for another community without copying Colorado private state
  or requiring an assistant to edit config files.

## Evidence and limits

Core tests cover concurrent publication, process restart, permissions, feeds,
deduplication, paging, and packet policy. Package tests exercise the installed
CLI outside the source tree. Real SDK tests use loopback radio-protocol peers.
Three-host Reticulum tests cover disconnection, reordered events, large UTF-8
posts, retries, LXMF, and NomadNet with temporary identities. Chromium tests
exercise the real HTTP service and store.

See [development](development.md) for exact checks and the
[current CI results](https://github.com/Colorado-Mesh/mesh-bbs/actions/workflows/ci.yml).
These tests do not establish community-scale RF performance, every hardware
combination, or amateur-station suitability. A single deployed host importing
feeds does not demonstrate live multi-host federation.

## Follow-up work

Field pilots should measure delivery, airtime, lost responses, busy-channel
behavior, and disconnect recovery on actual MeshCore and Meshtastic hardware.
Operators should also rehearse restoring a backup and replacing an announcing
host without duplicating channel notices.

Native TNC/switch management, subscriptions, attachments, automatic PDF text
extraction, and Mesh Client integration remain outside the implemented service.
Add them only with explicit scope, bounded transport behavior, and tests of the
identity and retry contracts above.
