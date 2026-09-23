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
- A standalone web interface for reading, writing posts, and replying to threads.
- MeshCore companion and Meshtastic direct-message interfaces with pagination.
- Small, rate-limited radio responses; full newsletters are retrieved on request.
- Automatic radio recovery while local reading and newsletter imports stay available.
- A cleartext terminal interface for operator-managed packet-radio sessions.

The service runs independently of Mesh Client. MeshCore Room Servers are not
required. The radio adapters respond to direct messages; they do not broadcast
newsletters into public channels.

## Install

On Linux or Apple Silicon macOS, run this as your normal user. Intel Macs need
the [source-build prerequisites](docs/install.md#intel-macs) first:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Colorado-Mesh/mesh-bbs/main/install.sh | sh
```

The wizard offers **Colorado Mesh** or **another community**, with separate
configuration, data, and identities for each region. It installs the protocol
libraries and leaves radio connections disabled until the operator selects
their devices. See [installation](docs/install.md) for pinned installs and
[operations](docs/operations.md) for peers, radios, and a background service.

Colorado Mesh includes its public blog feed and the Markdown text sources for
published newsletters. Both sync every 15 minutes. News is read-only: human posts and replies belong on community boards. NomadNet shows the newest threads first, with
links to complete posts and their replies.

Optional MeshCore `#bbs` and Meshtastic channel notices announce new threads and
boards, with DM reading instructions. One designated host per protocol announces;
other hosts stay silent. See [configuration and duplicate prevention](docs/operations.md#channel-announcements).
On either configured radio announcement channel, send `help` for the BBS contact.
DM `help` for a numbered menu: **1 News, 2 Boards, 3 Write/resume**. Reply with a
number to choose; `next` gets more, `back` returns, and `menu` starts over.

For a complete operator walkthrough, see [community setup](docs/community-setup.md):
guided connections, other regions, and pairing trusted hosts for automatic sync.

## Use the board

After setup, initialize the host, create your web contributor key, and start it:

```sh
mesh-bbs --region colorado-mesh init
mesh-bbs --region colorado-mesh web-access create alice
mesh-bbs --region colorado-mesh serve
```

Replace `alice` with your contributor name and save the generated key privately.
For another community, use its region ID; for a custom configuration location,
replace `--region colorado-mesh` with `--config /path/to/config.toml` in all three
commands. The setup wizard prints commands with your actual config path.

Open `http://127.0.0.1:8080` to read boards. Visit `/connect` to sign in with the
key, then choose a board and write a post or reply to a thread. Choose **Create board** for a new topic. Everyone can write on community boards;
`news` only accepts automatic imports, even for editor accounts. Reading is public and needs no key. This web
interface runs in Mesh BBS itself, independently of Mesh Client.

The browser keeps the access key in the current tab's session storage and
unfinished drafts in local browser storage. Save the key somewhere private if
you need it later; do not put it in a URL or a public message. For access from
other computers, configure HTTPS and `public_url` as described in
[operations](docs/operations.md#web-contributors-and-public-access).

DM the configured MeshCore companion, Meshtastic node, or LXMF destination with
`help`. All three use the same menu. To post:

1. Choose **3**. Pick a board or **Create a board**.
2. Send a title, then your text in one or more messages.
3. Send `done` to review, then `publish`. Nothing is public until publication.

Drafts survive a host restart. `menu` then **3** resumes a draft; `cancel`
discards it. When reading a community post, send `reply` to write a response.
Long posts are paged on demand. The bot sends one response per request.

For experienced users, `post general Title | Text` publishes in one step.
`commands` shows the full reference. See the [command guide](docs/commands.md)
for retry handling, long posts, replies, and packet terminal sessions.

RSS is available at `/feeds/news.xml`. Reticulum also serves
NomadNet-compatible pages.

## Host layout

```text
MeshCore DMs -----+
Meshtastic DMs ---+--> commands -------------+
LXMF messages ---+                         |
Packet terminal -+                         v
Web contributors --> authenticated writes --> SQLite + signed events
RSS / Atom ---------> newsletter import --->   |       |
NomadNet / web ------> read views <------------+       +--> Reticulum peers
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

To include the browser tests, install the separate development group and its
Chromium build:

```sh
uv sync --locked --all-extras --group browser
uv run playwright install chromium
uv run pytest -q
```

CI runs the browser suite in its own job. These tools are not required to run
the BBS or install its web interface.

Core tests also run without optional radio libraries. The integration tests
use temporary Reticulum profiles and real radio SDKs connected to loopback TCP
protocol emulators. They never use an installed profile or a physical radio.
See [architecture](docs/architecture.md)
for trust, ordering, and protocol boundaries.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
