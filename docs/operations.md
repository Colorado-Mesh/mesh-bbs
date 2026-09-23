# Run a community host

Start with [installation and community setup](install.md). The commands below
assume the Colorado Mesh default paths and an executable on your PATH. Substitute
your region's config path, or use `--region YOUR-REGION` after saving its config.
Use `~/.local/bin/mesh-bbs` when the installer directory is not on your PATH.

```sh
mesh-bbs --region colorado-mesh init
mesh-bbs --region colorado-mesh web-access create alice
mesh-bbs --region colorado-mesh serve
```

`init` prints the host's public signing identity. `doctor` checks the local
database and that enabled adapter libraries are installed; it does not prove
that radios, peers, or a newsletter source are reachable. `serve` starts the
web interface, configured feed polling, and explicitly enabled transports.
Press Ctrl-C to stop a foreground host.

Replace `alice` with your contributor name and save the generated key. Open
`http://127.0.0.1:8080/connect` to sign in, then choose a board and write a post.
The default board view is at `http://127.0.0.1:8080`; `/feeds/news.xml` exports
the saved newsletter board as RSS. Readers need no access key. Boards remain
public to anyone reaching the web listener.

## Web contributors and public access

Issue one key per contributor so authorship and revocation stay separate:

```sh
mesh-bbs --region colorado-mesh web-access create sam
mesh-bbs --region colorado-mesh web-access create editor-lee --editor
mesh-bbs --region colorado-mesh web-access list
mesh-bbs --region colorado-mesh web-access revoke sam
```

Names use lowercase letters, numbers, and hyphens. The key is printed once;
share it privately with that contributor. `list` reports names, editor roles,
and revocation status without exposing keys. Names remain reserved after
revocation and cannot be reassigned. If someone loses a key, revoke it and
issue a new contributor name. A contributor is recorded as `web:NAME`, separate
from their identity on any radio protocol.

Contributors can create community boards and post or reply in them. News is
reserved for automatic imports; even editor accounts cannot post or reply there. Browser forms are available
at `/new/BOARD` and `/reply/POST_ID`. The browser saves unfinished drafts in
local storage and keeps the access key in the current tab's session storage.
Keep the key in private storage for future sessions. Drafts can remain on a
shared computer after sign-out; clear them before leaving that computer.
Do not paste access keys into URLs, public posts, screenshots, or issue reports.

For public posting, provide HTTPS through an operator-managed reverse proxy
and set the top-level config value to its exact external origin:

```toml
public_url = "https://bbs.example.org"
```

Keep the BBS listener on loopback when the proxy runs on the same computer.
Allow the proxy to forward requests to that listener, and preserve the
`Authorization` and `Origin` headers. `public_url` controls origin checks and
published links; it does not configure DNS, certificates, or the proxy itself.
Restart the service after editing it. The interface accepts writes over HTTPS
or local loopback HTTP; exposing plain HTTP on a LAN does not enable posting.
Key creation and revocation take effect from the database without a restart.

The forms retain a publication operation ID when retrying a failed response,
so a lost confirmation does not create another post. Publication means the
local host saved the text. Web posts enter the same signed event store and
Reticulum replication as posts submitted through radio commands.

## Connection recovery and monitoring

A missing or disconnected MeshCore or Meshtastic radio no longer stops local
reading, newsletter polling, or the other configured transports. Each radio
reconnects independently, closing the previous connection before creating a
replacement. Retry waits grow from one second to at most one minute; a connection
that stays up for 30 seconds resets the wait. Meshtastic's SDK can also recover
some TCP interruptions within its existing connection instance.

Reading cursors, publication receipts, and the persisted airtime budget remain
in place across reconnects. An in-memory request awaiting a reply may be dropped
when its radio disconnects. Retry it with the same operation ID or draft ID to
recover the saved result. Reconnecting does not create a fresh airtime allowance.

Use these endpoints for different monitoring decisions:

```sh
curl -fsS http://127.0.0.1:8080/healthz
curl -sS http://127.0.0.1:8080/readyz
```

`/healthz` returns 200 while HTTP is available. `/readyz` returns 200 when all
enabled radio adapters are online, or 503 while one is connecting, retrying,
failed, or stopping. A host with no radio adapters enabled is ready. The JSON
names each protocol and reports its state, connection attempts, reconnection
count, and current retry wait. It contains no device paths, peer addresses, or
message contents. This endpoint does not prove RF delivery or peer convergence.

Use readiness failures to alert on radio access, rather than restarting an
otherwise useful host continuously. `failed` means the operator must check the
logs: missing dependencies, invalid adapter settings, or a cleanup failure need
attention and a service restart. Device connection details remain in local logs.

## Linux user service

Run one service per host configuration. From a downloaded source checkout:

```sh
mkdir -p ~/.config/systemd/user
cp examples/mesh-bbs.service ~/.config/systemd/user/mesh-bbs.service
```

Open that file and check `ExecStart`: it must point to your installed executable
and the intended region's configuration. Adjust it for `UV_TOOL_BIN_DIR`, XDG
paths, or another region. For another simultaneous instance, use a distinct unit
name, data directory, and web port.

After checking the config and testing `serve` in the foreground:

```sh
systemctl --user daemon-reload
systemctl --user enable --now mesh-bbs.service
systemctl --user status mesh-bbs.service
journalctl --user -u mesh-bbs.service -f
```

The example restarts failed processes after ten seconds. It does not restart
after a deliberate `systemctl --user stop mesh-bbs.service`. Configuration edits
take effect after `systemctl --user restart mesh-bbs.service`.

User services normally follow the user's login session. An administrator can
enable lingering for the service account when the host must run while logged
out. Follow your distribution's `loginctl enable-linger` policy; the installer
does not change it. On macOS, run in the foreground for a pilot or configure an
operator-managed launchd service; the systemd unit is Linux-only.

## Connect protocols deliberately

| Access | Equipment or software | Operator setup |
| --- | --- | --- |
| Reticulum | Reticulum installation and its configured interfaces | Dedicated `reticulum.config_dir`, then `enabled = true` |
| MeshCore | Radio with stock companion firmware | Explicit serial device or TCP address; no Room Server |
| Meshtastic | Radio with stock Meshtastic firmware | Explicit serial device or TCP address |
| Web/RSS | Any browser or RSS reader reaching the host | Public reading; contributor key for posting; HTTPS and `public_url` for remote writes |

No custom radio firmware is required by the adapters. Use a firmware/library
combination you have checked in a local pilot. Close other programs that own the
same serial device. Grant the service account serial access using the operating
system's device permissions; do not solve permission errors by running the
whole BBS as root.

MeshCore and Meshtastic users send the BBS node a direct message such as `help`,
`boards`, or `news latest`. `more` retrieves the next bounded page. Broadcast
channel messages are not a command interface. Operators can enable brief new-thread
and new-board notices on a discovery channel; see [channel announcements](#channel-announcements).
Readers DM the announcing node with `read ID`, then send `more` as separate
messages to continue. `threads BOARD` lists the threads on a newly announced board.

For a short post, send `@meetup-1 post general Meetup | Saturday at nine`.
For longer text, use the numbered draft commands in the
[command guide](commands.md). The same guide covers LXMF and packet sessions.

Reticulum users browse the announced NomadNet destination or send commands to
the LXMF destination printed in the service logs. These are separate from the
host's federation destination. Reticulum's interface configuration decides what
links carry traffic; enabling it may transmit announcements on those links.

An LXMF delivery receipt confirms transport delivery, not a command result.
The BBS resolves a previously unseen sender's identity and verifies the original
message signature before executing it. Discovery waits up to ten seconds, with
at most eight pending messages in a separate queue, so established readers keep
working. If the log says `LXMF sender identity unavailable`, use **Announce now**
in the sending app and retry the command. Invalid signatures are never accepted.

Local protocol tests use temporary profiles and loopback interfaces. The test
suite includes actual SDK connections to MeshCore and Meshtastic TCP protocol
emulators, including lost ACKs and reconnects. These emulators do not run radio
firmware or establish successful RF operation. Validate delivery, airtime,
retry behavior, and disconnect recovery
with volunteer operators before inviting a community-wide load.

## Limit radio traffic

Each radio has a persisted rolling budget for estimated reply transmission time.
The example reserves 10 seconds per possible packet transmission, including
every retry, with 120 seconds available per hour. These deliberately cautious
defaults permit six MeshCore responses or three Meshtastic responses per hour
when their entire retry allowances are reserved. Set `packet_airtime_seconds`
to an upper bound measured for your firmware, modem settings, maximum reply
length, and protocol overhead; a smaller verified value makes more replies
available within the same budget. Changing spreading factor, bandwidth, coding
rate, path format, or firmware retry behavior requires checking that bound again.

The scheduler reserves capacity before it executes a request. Once the budget
is used, queued requests wait instead of advancing reading cursors or publishing
posts that cannot yet receive a reply. It does not refund unused retries. The
reservation remains charged for a full window after processing finishes; an
unfinished reservation receives a fresh cooldown after restart. Deleting the
airtime database discards this history, so keep it with the host's data.

These limits cover BBS response transmissions made through the adapters. They
do not measure or limit radio advertisements, protocol ACKs, other applications,
or repeater forwarding, and they are not proof of regulatory duty-cycle
compliance. Operators must account for that additional traffic separately.
`min_interval` also spaces responses. Queue capacity and per-sender admission
limits reject excess requests without sending more congestion traffic.

## Start a trusted federation

Agree on the same region ID and board slugs before initializing each host.
Each operator creates a separate host identity and keeps their own private key.
Exchange the values printed by `init`: `origin` and `public_key`. When Reticulum
is enabled, also exchange its identity hash from the logs. The adapter derives
the sync destination from that identity; it is not a separate configuration value.
Verify these values through a channel you trust.

Add an explicit `[[peers]]` entry for each permitted origin, with the public key,
Reticulum identity, and a list of `allowed_boards`. An empty list grants no board
access. `can_moderate = true` is a separate privilege: do not grant it just to
enable synchronization. Restart after changing peer configuration.

In the first pilot, explicitly configure every host in the small trusted group.
There is no automatic peer directory, membership approval service, or configured
Colorado Mesh bootstrap server. Selecting a region name does not establish
trust. Local writes remain available during a partition; a "Saved locally"
confirmation does not mean another host already has the post.

Test with three hosts: publish on each while disconnected, reconnect them, and
check thread IDs, replies, newsletter corrections, and removals after repeated
sync. A missing parent must stay a missing parent instead of attaching a reply
to an unrelated post. Preserve removal records when copying or recovering data;
deleting their history can make old content appear again.

## MeshCore discovery

The BBS synchronizes the companion's clock when it connects. A radio that has
rebooted can otherwise advertise with an old timestamp and be ignored by peers.
To opt into a daily flood advertisement, set `advert_interval_seconds = 86400`
in `[meshcore]`. The default is `0` (disabled); supported intervals are one hour
to seven days. This setting does not change the radio's frequency or flood scope.

The service saves each attempt in `meshcore-advert.json` before sending it and
charges one packet to the existing airtime budget. Reconnects and restarts keep
the schedule. An uncertain result waits until the next interval rather than
immediately flooding again. The companion's OK response confirms acceptance of
the command, not reception by another node. Meshtastic does not use this option.

## Announce an already running Reticulum service

On a systemd installation, send a manual announce without restarting readers,
radio connections, or the web service:

```sh
sudo systemctl kill --kill-who=main --signal=SIGUSR1 mesh-bbs.service
```

This requires a Mesh BBS version that supports SIGUSR1 and enabled Reticulum.
For a foreground instance on Linux or macOS, `kill -USR1 BBS_PID` does the same.
Manual requests are limited to once per minute. Automatic NomadNet, LXMF, and
sync announces run every **30 minutes**. The scheduled deadline survives restarts;
startup and manual announcements do not postpone it. A failed announce is logged
and does not stop future scheduled attempts. Logs confirm local submission,
not delivery to every reader. Relays can delay or suppress repeated announces,
so an old "last seen" time does not by itself mean the page is unreachable.
Path responses also carry the configured NomadNet display name.

## Pair community hosts

Use `mesh-bbs configure` for connection and feed setup, then `peer export` and
`peer add` to exchange signed public identity files. `peer list` shows configured
trust and the last sync outcome. See [the operator walkthrough](community-setup.md)
for exact commands, region isolation, and future-board permissions. Every host
must explicitly trust every signing origin it intends to replicate.

## Publish newsletters

The Colorado Mesh setup preset imports its
[blog feed](https://blog.coloradomesh.org/feed.xml) with source ID
`colorado-mesh-blog`. Blog articles come from the full-content Atom feed generated
by the organization's `blog` repository. The preset also sets
`newsletter_markdown_base_url` to
`https://raw.githubusercontent.com/Colorado-Mesh/advocacy/main/newsletter/issues/`.
When a published feed entry links an issue PDF under that directory, the importer
reads the issue's sibling `draft.md`, converts it to plain text, and updates the
existing BBS post. It does not scan unpublished issue directories or extract PDFs.
Links, headings, and image captions are retained as text; images are not fetched.

The service checks every 15 minutes, including Markdown corrections when the feed
itself has not changed. The post ID, publication date, and replies stay intact;
unchanged imports create no duplicate posts or revisions. A failed text fetch
keeps the previous post and retries with backoff. Existing installations can add
the `newsletter_markdown_base_url` above to their `[[feeds]]` section and run
`import-feeds` once to upgrade their already imported newsletter introductions.

For another existing newsletter, add its actual RSS/Atom URL and a stable `source_id`
to the config. Every importer for that source must use the same ID and board.
The importer retains item IDs across retries and revisions. Summary-only feed
items remain summaries; the service does not promise full text missing from the
source. Choose a full-content feed when offline reading needs the whole issue, or
configure the Markdown directory if it uses the same `YYYY-MM/draft.md` layout.

NomadNet's home page shows the ten newest entries across boards, with dates and
direct links to full posts and conversations. Board listings also show newest
threads first. Replies remain in conversation order, and page caching is disabled
so reopening a page checks the current local copy.

Check a configured source once with:

```sh
mesh-bbs --region colorado-mesh import-feeds
mesh-bbs --region colorado-mesh command 'news latest'
```

`serve` handles subsequent scheduled polling. Feed failures back off and appear
in logs. Do not give unrelated sources the same `source_id`, and do not change
the ID merely because the publisher changed its URL.

The `news` board only accepts configured automatic imports. No human account can
create a thread or reply there, including editor accounts and local CLI users.
Discuss an issue in `general` or create a community board. Existing posts and
historical replies remain readable; feed corrections preserve their IDs.

The local operator can correct their own community post or remove a
post:

```sh
mesh-bbs edit POST_ID --body-file corrected.txt --operation correction-1
mesh-bbs remove POST_ID --operation removal-1
```

Reusing either operation ID is safe. Removal of a post originally accepted by
this host follows that host's authorship authority. Removing another host's
post applies locally; other hosts only apply that removal when they grant this
host moderation authority for the board. Removal stops public serving but does
not erase historical events, backups, or readers' existing copies.

Radio authors use their transport address, not a nickname. MeshCore, Meshtastic,
and LXMF users have the same community posting capabilities. Legacy `editors`
settings do not bypass the read-only News policy.

## Back up and recover

The database contains the host's private signing key as well as posts, removal
history, drafts, deduplication state, and web contributor credential hashes.
Keep backups private. Do not attach a
database to a public issue or copy one into CI artifacts.

For a consistent SQLite snapshot while the host is running:

```sh
mkdir -p ~/mesh-bbs-backups
chmod 700 ~/mesh-bbs-backups
mesh-bbs --region colorado-mesh backup ~/mesh-bbs-backups/colorado-2026-09-22.sqlite3
```

Choose a new destination each time; the command refuses to overwrite an existing
backup. This snapshots the database, including its signing key. It does not copy
the TOML config, Reticulum interface config, or the separate Reticulum identity
and delivery state in the data directory.

For a complete host backup, stop the service and archive the config directory,
the whole data directory, and the configured Reticulum directory if it lives
elsewhere. Copying a live SQLite file by itself can miss WAL changes; use the
backup command or make the full copy with the service stopped. Store a copy on
another device and periodically practice recovery into a separate temporary
directory with all radio transports disabled.

To recover, stop the existing host and preserve its current files. Restore the
matching config, database, and transport state into the intended data directory,
keep the same region, check filesystem permissions, and run `doctor` before
starting the service. Retain tombstones and event history. After connecting,
verify newer peer events have arrived. Never run a backup clone alongside the
original with the same identity; initialize a fresh host when adding capacity.

Treat logs as operator data: they can include addresses, post identifiers, and
transport errors. Redact feed subscription secrets, private keys, and message
content before sharing diagnostics. A lost signing identity needs a fresh host
and an explicit trust update by peer operators.

## CI coverage

GitHub CI checks formatting and typing once, exercises the core and packaged CLI
on Python 3.12/3.13/3.14 on Ubuntu and Python 3.12 on Apple Silicon and Intel
macOS. The Intel job also builds cryptography from source and runs the actual
installer with all protocol extras. CI runs optional
adapter, TCP protocol emulator, and isolated Reticulum integration tests on
Ubuntu. Dependencies come
from `uv.lock`; uv is pinned to 0.12.3, and actions are pinned to commits.
Superseded branch runs are canceled. Distribution artifacts are test outputs,
not automatic releases or deployments.

Maintainer references: [uv GitHub integration](https://docs.astral.sh/uv/guides/integration/github/),
[setup-uv](https://github.com/astral-sh/setup-uv), and
[Dependabot configuration](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference).


## Channel announcements

Use **one designated announcing host per protocol and overlapping RF mesh**.
Other companions still accept DMs and replicate posts, but remain silent on the
channel. All operators should agree on the same `announcement_owner`: the
64-character `origin` printed by `mesh-bbs init` on the designated host. This is
the BBS signing identity, not a radio public key or a Reticulum address.

Add these options to the existing `[meshcore]` section on participating hosts:

```toml
announcement_owner = "REPLACE_WITH_DESIGNATED_HOST_ORIGIN"
announcement_channel = 1
announcement_channel_name = "#bbs"
announcement_interval_seconds = 600
```

Create the `#bbs` hashtag channel in the MeshCore companion and reader apps
first. A hashtag channel uses the key derived from its exact name. The BBS
verifies the configured index, name and hashtag key before sending; it never
overwrites radio channels automatically. Channel indexes are local to each
radio, so use the slot containing `#bbs` on that companion. Keep its public
name recognizable; the Colorado Mesh pilot uses `coloradomesh.org-bbs`.

Sending exactly `help` (case-insensitive) on the configured MeshCore or Meshtastic channel asks the
designated host for a short reply naming its companion and the DM reading
instructions. DM `help` for the numbered menu, `next` for more, and `menu` to
start over. Choose **3** to write or create a community board. `commands` lists
the advanced syntax for short or multipart posts; use `more` to page through it.
Channel traffic never creates posts or establishes an author's identity.

Only the designated announcement owner answers channel help. There is at most
one public help response every five minutes, with packet replay suppression for
24 hours; both survive restarts. A helper response uses one packet from the
shared airtime budget and is skipped if the budget is exhausted. There is no
help backlog or automatic retry after an uncertain send. Other channels,
ordinary conversation, and help embedded in a longer message remain silent.
The five-minute helper limit is separate from the new-content notice interval.

Meshtastic uses the same announcement options in `[meshtastic]`, with an
operator-configured secondary channel, for example:

```toml
announcement_owner = "REPLACE_WITH_DESIGNATED_HOST_ORIGIN"
announcement_channel = 1
announcement_channel_name = "BBS"
announcement_interval_seconds = 600
```

Create that Meshtastic secondary channel using the local mesh's agreed channel
name and PSK, and share its channel URL/QR with readers through the usual app.
Unlike MeshCore hashtags, a Meshtastic name alone does not establish a shared
channel key. Match the mesh's radio region and modem preset as well. The service
checks the index and channel name but does not provision or publish a PSK.
Meshtastic notices are broadcast text packets without requested acknowledgments
or application replies. Reading remains via a DM to the announcing node.
A Meshtastic radio must be explicitly configured and enabled; enabling MeshCore
does not also enable Meshtastic.

Only the host whose signing identity matches `announcement_owner` sends. Leave
all three owner/channel options absent to disable notices. Do not independently
set every host as its own owner or clone one host's private identity across
running machines. Separate owners on different protocols are fine. Do not use
automatic timeouts to promote replacements: during a partition, two hosts could
both claim leadership and spam the same RF channel. To move the role, stop or
disable the old announcer first, agree on the new origin, and update the configs.
The newly designated host baselines existing content instead of replaying it.

Notices include the board, a UTF-8-bounded title and a short `read ID` command.
They cover new root threads from web, radio, CLI, feeds and trusted replication
across all configured boards, including boards added later. New board notices
point to `threads BOARD`. Readers can create community boards through the guided
DM menu or web interface. Paired peers with wildcard board grants sync new
boards automatically; peers with explicit board lists require updated grants.
Replies, edits, removals and repeated imports do not create new notices.

The default limit is one notice every ten minutes per protocol, sharing the
persisted radio airtime budget with replies and scheduled adverts. Up to 32
notices wait in SQLite; bursts keep the newest 32 and log the skipped count.
Queued notices expire after 24 hours, and historical imports more than 24 hours
old do not generate notices. First enablement is silent about existing content.
Seen thread IDs and the send interval survive restarts. The attempt is recorded
before handing it to the SDK, so a crash or lost serial response cannot create
a retry storm. This deliberately favors avoiding duplicate channel traffic:
an uncertain send can be missed, and companion acceptance does not prove RF
reception. Readers can always use `threads BOARD` or the web/NomadNet view.

### Size the reply allowance for the configured modem

`packet_airtime_seconds` is an upper estimate for **one** transmitted packet,
not the acknowledgement timeout. The default ten seconds is deliberately
conservative for unknown radios. With a 120-second/hour allowance, that permits
only six MeshCore replies (two attempts each), or three Meshtastic replies
(four firmware attempts each). Set a defensible estimate for your actual modem
before expecting an interactive conversation; do not copy a fast profile's
estimate onto a slow LoRa preset.

For the Colorado pilot's MeshCore SF7 / 62.5 kHz / CR 4/5 profile, a maximum
255-byte LoRa packet with explicit header, CRC, and a 32-symbol preamble takes
approximately 0.85 seconds. A one-second estimate retains a margin and permits
up to 60 two-attempt replies per hour under the same 120-second allowance.
MeshCore's pinned [preamble selection](https://github.com/meshcore-dev/MeshCore/blob/d929643/src/helpers/radiolib/RadioLibWrappers.h)
and [airtime calculation](https://github.com/meshcore-dev/MeshCore/blob/d929643/src/helpers/radiolib/RadioLibWrappers.cpp)
use RadioLib. This is an admission estimate, not measured channel occupancy;
forwarders, firmware ACKs, and unrelated traffic are outside this accounting.
Meshtastic needs an estimate for its own preset and firmware retry count.

Changing the estimate or budget with the same rolling window preserves already
charged debt. Raising the estimate scales existing charges conservatively;
lowering it does not refund them. Changing the window retains a full cooldown
because older reservation history may already have expired. Restarting does
not clear the allowance.
