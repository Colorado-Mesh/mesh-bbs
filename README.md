# Mesh BBS

A standalone bulletin board for communities using Reticulum, MeshCore,
Meshtastic, and other constrained networks. Each host keeps a local copy of
shared boards so people can read newsletters and continue discussions through
outages.

This repository is under active development. The implementation plan and
acceptance criteria are in [docs/plan.md](docs/plan.md). Features are only marked
complete after their tests pass; hardware validation is tracked separately.

## What we are building

- Boards, threads, replies, long text posts, revisions, and removal records.
- Colorado Mesh newsletters with RSS/Atom import and an ordinary web feed.
- Permanent identifiers and durable deduplication across retries and hosts.
- Trusted-host replication over Reticulum, with disconnected operation.
- NomadNet browsing and LXMF command access for Reticulum users.
- MeshCore companion and Meshtastic direct-message interfaces with pagination.
- Small, rate-limited radio responses; full newsletters are retrieved on request.
- A transport interface that can later support operator-managed ham packet access.

The service runs independently of Mesh Client. MeshCore Room Servers are not
required. Public radio announcements are optional and disabled by default.

## Development

Python 3.12 or newer, SQLite, and uv. See the implementation plan for current
commands and milestones as they become available.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
