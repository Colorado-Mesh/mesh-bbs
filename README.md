# Mesh BBS

A standalone bulletin board for communities using Reticulum, MeshCore,
Meshtastic, and other constrained networks. Each host keeps a local copy of
shared boards so people can read newsletters and continue discussions through
outages.

This is an early pilot implementation. Local tests cover recovery and real
Reticulum traffic over loopback; radio coverage and airtime behavior still need
an operator-run field test. The implementation plan and acceptance criteria are
in [docs/plan.md](docs/plan.md).

## What it does

- Boards, threads, replies, long text posts, revisions, and removal records.
- Colorado Mesh newsletters with RSS/Atom import and an ordinary web feed.
- Permanent identifiers and durable deduplication across retries and hosts.
- Trusted-host replication over Reticulum, with disconnected operation.
- NomadNet browsing and LXMF command access for Reticulum users.
- MeshCore companion and Meshtastic direct-message interfaces with pagination.
- Small, rate-limited radio responses; full newsletters are retrieved on request.
- Automatic radio recovery while local reading and newsletter imports stay available.
- A cleartext terminal interface for operator-managed packet-radio sessions.

The service runs independently of Mesh Client. MeshCore Room Servers are not
required. The radio adapters respond to direct messages; they do not broadcast
newsletters into public channels.

## Install

On Linux or macOS, run this as your normal user:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Colorado-Mesh/mesh-bbs/main/install.sh | sh
```

The wizard offers **Colorado Mesh** or **another community**, with separate
configuration, data, and identities for each region. It installs the protocol
libraries and leaves radio connections disabled until the operator selects
their devices. See [installation](docs/install.md) for pinned installs and
[operations](docs/operations.md) for peers, radios, and a background service.

Colorado Mesh includes its public blog feed. That feed currently contains
newsletter introductions and links to PDFs; it does not contain the complete
newsletter text. Text editions can also be published directly into the BBS.

## Use the board

DM the configured MeshCore companion, Meshtastic node, or LXMF destination.
The same commands are available locally with `mesh-bbs command 'COMMAND'`:

```text
boards
threads general
news latest
read POST_ID
more
thread POST_ID
reply POST_ID I can help Saturday.
publish DRAFT_ID
```

Replies first create a draft and show the parent ID. `publish DRAFT_ID` commits
it; repeating that command returns the existing post. Longer posts use `new`,
numbered `add` parts, and `preview`. `more` continues a saved revision even if a
new issue arrives. Removal stops serving the saved copy.

For an editor publishing a newsletter from a text file:

```sh
mesh-bbs post news 'September newsletter' \
  --body-file september.txt --operation september-2026
```

Reuse the operation ID when retrying the same publication. A successful response
means the host saved the post; it does not claim another host has replicated it.

The local web reader defaults to `http://127.0.0.1:8080`, with RSS at
`/feeds/news.xml`. Reticulum also serves NomadNet-compatible pages.

## Host layout

```text
MeshCore DMs -----+
Meshtastic DMs ---+--> commands --> SQLite + signed events
LXMF messages ---+                     |
Packet terminal -+                     |
NomadNet / web ------> read views       +--> trusted Reticulum peers
RSS / Atom ---------> newsletter import
```

Stock MeshCore companion and stock Meshtastic firmware are sufficient for the
implemented adapters. The BBS runs on the attached Pi or computer. MeshCore
contacts must be exchanged before using DMs. A region name selects a community
namespace; operators still explicitly exchange peer identities and board grants.

## Development

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
uv run python -m hatchling build
```

Core tests also run without optional radio libraries. The integration tests
use temporary Reticulum profiles and real radio SDKs connected to loopback TCP
protocol emulators. They never use an installed profile or a physical radio.
See [architecture](docs/architecture.md)
for trust, ordering, and protocol boundaries.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
