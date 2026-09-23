# Operate a BBS host

Start with [installation](install.md) and [community setup](community-setup.md).
Examples use `colorado-mesh`, the default paths, and port 8080. Substitute your
region or pass `--config /path/to/config.toml` before the command. Use
`~/.local/bin/mesh-bbs` if the executable is not on PATH.

```sh
mesh-bbs --region colorado-mesh init
mesh-bbs --region colorado-mesh doctor
mesh-bbs --region colorado-mesh serve
```

`init` creates the database and prints its public signing identity. `doctor`
checks local storage and enabled dependencies, not remote reachability. `serve`
starts web reading, feed polling, and enabled transports. Ctrl-C stops it.

## Web contributors and public access

Reading is public to anyone who reaches the listener. Issue an individual key
to each web contributor:

```sh
mesh-bbs --region colorado-mesh web-access create alice
mesh-bbs --region colorado-mesh web-access list
mesh-bbs --region colorado-mesh web-access revoke alice
```

A key is printed once. Share it privately and enter it at `/connect`. Names use
lowercase letters, numbers, and hyphens. Revocation is immediate and permanently
reserves the name; replace a lost key with a new contributor name. `list` shows
roles and status without exposing keys.

Contributors can create boards, posts, and replies. News is import-only, even
for accounts created with the legacy `--editor` flag. The author is `web:NAME`,
separate from that person's radio identities.

The browser keeps its key in tab session storage and drafts in local storage.
A shared computer can retain drafts after sign-out. Keep keys out of URLs,
screenshots, public posts, and diagnostics. The UI preserves a publication's
operation ID across uncertain retries; a saved-local receipt means this host
committed the post, not that a peer has received it.

For remote posting, terminate HTTPS at a reverse proxy and configure its exact
external origin:

```toml
public_url = "https://bbs.example.org"
```

Keep the local listener on `127.0.0.1` when the proxy is on the same machine.
Forward `Origin` and `Authorization` headers. `public_url` sets permitted write
origins and published links; it does not create DNS, certificates, or a proxy.
Restart after changing it. Plain HTTP writes are allowed only for loopback
origins, not an exposed LAN HTTP address.

### Tailscale access

With Tailscale connected and HTTPS enabled for your tailnet, inspect existing
Serve routes before choosing an unused HTTPS port:

```sh
tailscale serve status
tailscale serve --bg --https=13004 http://127.0.0.1:8080
```

This example exposes the BBS within the tailnet. Use the HTTPS URL printed by
Tailscale, including port 13004, as `public_url`, then restart the BBS. Readers
must have tailnet access; posting still requires a BBS key. Do not reset Serve
or replace an existing application's route. Tailnet permissions and certificate
setup are managed in Tailscale; see [Serve documentation](https://tailscale.com/kb/1312/serve).

Keep a host's public display `name` separate from its HTTPS hostname. NomadNet
publishes the display name and Reticulum links; it need not advertise a private
Tailscale hostname.

## Linux user service

Test foreground `serve` first. From a source checkout, install the example unit:

```sh
mkdir -p ~/.config/systemd/user
cp examples/mesh-bbs.service ~/.config/systemd/user/mesh-bbs.service
```

Check its `ExecStart` paths and region. Use a separate unit, data directory, and
port for another instance. Then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now mesh-bbs.service
systemctl --user status mesh-bbs.service
journalctl --user -u mesh-bbs.service -f
```

Restart with `systemctl --user restart mesh-bbs.service` after config changes.
The unit restarts failed processes after ten seconds. An explicit stop stays
stopped. To run while logged out, ask the administrator to enable lingering for
the account under the distribution's policy. macOS needs a separately configured
launchd service or a foreground process; the example unit is Linux-only.

Some installations use a system service instead. For those, replace
`systemctl --user` with `sudo systemctl` and use the corresponding system journal.
Use the existing unit type consistently rather than starting a second instance.

## Connections and recovery

Use `mesh-bbs --region colorado-mesh configure` to select serial/TCP radios,
Reticulum interfaces, feeds, and web settings. The [sample TOML](../examples/config.toml)
shows the same settings for manual configuration.

MeshCore needs companion firmware; Meshtastic needs stock client firmware.
Neither requires custom firmware. Configure RF settings in the radio's normal
app, close other serial clients, and grant the service account device access
through OS permissions. Do not run the whole BBS as root to bypass serial errors.

Each radio reconnects independently. Retry waits grow from one second to one
minute; 30 seconds of stable connection resets the wait. The old client must
close before replacement. Web reading, feeds, and other transports continue
during a radio outage. Cursors, drafts, receipts, and airtime debt survive it.
Pending in-memory replies may be lost: users can retry the same operation or
draft publication to recover a saved result.

### Health and readiness

```sh
curl -fsS http://127.0.0.1:8080/healthz
curl -sS http://127.0.0.1:8080/readyz
```

`/healthz` reports HTTP liveness. `/readyz` returns 200 when configured radios
are online, or 503 while any is connecting, retrying, failed, or stopping. With
no radios enabled it is ready. The response lists states and retry counts,
without device paths or message content. It does not prove RF reception, feed
freshness, or peer convergence.

For a `failed` radio, inspect logs for missing dependencies, invalid settings,
or cleanup failure, fix the cause, and restart. Repeatedly restarting an
otherwise useful host is not a substitute for checking the failed connection.

### Reticulum access

Give readers the NomadNet node address for pages and the **LXMF destination**
for chat. These differ from the Reticulum identity used in peer cards and the
BBS sync destination. Logs print the addresses. Share a readable display name.

An LXMF transport receipt is not the command response. The BBS validates the
sender's signature; an unknown identity gets a bounded discovery wait of ten
seconds, with at most eight pending messages. If logs report `LXMF sender
identity unavailable`, announce from the sending app and retry. Do not infer
that two similar display names are the same address or person.

## Channel announcements

Choose one announcing host per protocol and overlapping RF mesh. Use its
64-character BBS signing `origin` as `announcement_owner` on participants. This
is not the radio public key or an LXMF address. Other hosts still serve DMs.

For MeshCore, add to `[meshcore]`:

```toml
announcement_owner = "REPLACE_WITH_DESIGNATED_HOST_ORIGIN"
announcement_channel = 1
announcement_channel_name = "#bbs"
announcement_interval_seconds = 600
```

Create `#bbs` in the companion and reader apps first. The service verifies its
slot, exact name, and hashtag-derived key. It does not overwrite channels.
Use the companion's slot, which may differ from a reader's slot. A recognizable
companion name helps people open the private chat.

For Meshtastic, use the same options in `[meshtastic]`, with the agreed secondary
channel name, such as `BBS`. Provision its PSK in the normal app and share the
channel URL/QR. A name alone does not establish a Meshtastic channel key. Readers
also need the matching region and modem profile. The service checks the slot
and name, but does not create or publish a PSK. Enable a Meshtastic radio
explicitly; MeshCore setup does not provide one.

New threads from any interface, feeds, or trusted replication can produce a
notice, including threads on newly created boards:

```text
New post in running:
"mesh runners!"
To read it, send me a private message: read #7
```

A `#N` shortcut is permanent on that host and shared by its protocols. Allocation
and notice consumption commit together. Shortcuts are local; federation still
uses original post IDs. Titles and board labels shorten with an ellipsis when
needed, preserving the complete reading instruction. Hexadecimal commands from
older notices remain valid. New-board notices ask readers to DM `boards` and
choose from its menu. Replies, edits, removals, and repeated imports stay silent.

Sending exactly `help`, case-insensitive, on the configured channel returns
instructions to message the node privately. Embedded help words, ordinary
conversation, and other channels stay silent. Only the designated owner answers.
Public help has a five-minute cooldown and 24-hour replay suppression, both
persisted. It spends a packet from the shared budget, with no backlog or
uncertain-send retry. It never creates a post or establishes an author identity.

New-content notices default to one per ten minutes per protocol. SQLite holds
at most 32; bursts retain the newest and log skipped counts. Queued notices
expire after 24 hours. Historical imports older than a day stay silent. Initial
enablement baselines existing content instead of broadcasting the archive.
Attempts are recorded before SDK submission, so an uncertain send may be missed
rather than repeated after restart. SDK acceptance is not proof of reception.

Leave owner, slot, and channel-name options absent to disable channel traffic.
To move the role, stop or disable the old announcer first, then agree on the new
origin. The replacement baselines history. Do not set every host as its own
owner, clone identities, or elect replacements automatically during partitions.

## Size the reply allowance for the configured modem

`packet_airtime_seconds` estimates one transmission, not the ACK timeout.
`airtime_budget_seconds` limits reservations within `airtime_window_seconds`.
`min_interval` spaces transmissions. All possible retries are charged in advance;
unused attempts are not refunded.

| Example reservation | With a 120-second hourly allowance |
| --- | --- |
| Default 10 seconds/packet, two MeshCore attempts | Six responses |
| Default 10 seconds/packet, four Meshtastic firmware attempts | Three responses |
| Verified 1 second/packet, two MeshCore attempts | Up to 60 responses |

The default is conservative for an unknown modem. The wizard offers estimates
for known profiles: MeshCore SF7/62.5 kHz/CR5 at one second, Meshtastic LongFast
at three seconds and MediumFast at one second. These are admission estimates,
not measurements. Use an upper bound appropriate to your modem, maximum packet,
path overhead, and retry settings. Recheck it after changing those settings.
MeshCore's pinned [preamble](https://github.com/meshcore-dev/MeshCore/blob/d929643/src/helpers/radiolib/RadioLibWrappers.h)
and [airtime implementation](https://github.com/meshcore-dev/MeshCore/blob/d929643/src/helpers/radiolib/RadioLibWrappers.cpp)
are the basis for the fast-profile estimate.

The scheduler reserves before processing a request. When exhausted, it waits
rather than advancing a cursor or publishing without reply capacity. Finished
reservations remain charged for a full window; unfinished ones get a fresh
cooldown after restart. Raising an estimate scales debt conservatively, lowering
it does not refund debt, and changing the window retains a full cooldown.

These reservations include BBS replies, channel notices/help, and BBS-scheduled
MeshCore adverts. They do not measure firmware ACKs, unrelated adverts, other
apps, or repeater forwarding. Account for that traffic separately. Queue and
per-sender limits reject excess requests without adding congestion replies.
Never delete budget state as a way to make a stalled bot send faster.

## MeshCore discovery

The BBS sets the companion clock on connection so adverts do not carry a stale
reboot timestamp. Opt into a daily flood advert in `[meshcore]` with
`advert_interval_seconds = 86400`. Zero disables it; supported intervals are
one hour to seven days. This changes neither frequency nor flood scope.

`meshcore-advert.json` records each attempt before submission and survives
reconnect/restart. Each attempt uses one packet reservation. An uncertain result
waits for the next interval. A companion OK means it accepted the request, not
that another node heard it. Meshtastic does not use this setting.

## Announce an already running Reticulum service

Current versions announce NomadNet, LXMF, and sync destinations every 30 minutes.
The scheduled deadline survives restarts; startup/manual announces do not move
it. Failed submissions are logged and later scheduled attempts continue.
NomadNet path responses also carry the configured display name.

To request an extra announce from the user service:

```sh
systemctl --user kill --kill-who=main --signal=SIGUSR1 mesh-bbs.service
```

For a system service use `sudo systemctl kill --kill-who=main --signal=SIGUSR1
mesh-bbs.service` as one command. Foreground Linux/macOS instances accept
`kill -USR1 BBS_PID`. Confirm that the installed BBS supports this handler before
sending the signal; older versions without it would terminate. Reticulum must
be enabled. Manual requests are limited to once a minute.

Logs confirm local submission only. Relays can delay or suppress repeated
announces; a stale “last seen” timestamp alone does not establish that a page
is unreachable. Try opening the actual address before repeatedly restarting.

## Pair community hosts

Follow [the pairing walkthrough](community-setup.md#pair-two-hosts): same region,
separate identities, public-card exchange, fingerprint verification, explicit
board grants, then restart. `peer list` reports recorded sync outcomes. There
is no automatic public peer directory or Colorado bootstrap service.

Each accepted event needs its original host's board grant, even if a trusted
intermediary forwards it. `allowed_boards = ["*"]` includes future boards; an
explicit list does not expand. Empty grants give no access. `can_moderate = true`
is a separate privilege, not a requirement for ordinary sync.

Check a new group by writing independently during a disconnect, reconnecting,
and confirming IDs, reply parents, corrections, and removals. Keep clocks in
sync. A missing parent stays missing rather than attaching to another post.

## Publish newsletters

The Colorado preset uses `https://blog.coloradomesh.org/feed.xml` with stable
source ID `colorado-mesh-blog`. Full blog content comes from that feed. Its
`newsletter_markdown_base_url` points to
`https://raw.githubusercontent.com/Colorado-Mesh/advocacy/main/newsletter/issues/`.
Published feed entries linking an issue PDF select the sibling `draft.md` text.
The importer does not scan unpublished directories or extract PDF content.

Every 15 minutes, `serve` checks feeds and linked Markdown corrections, including
when feed validators report no change. Headings, links, and image captions
become plain text; images are not fetched. Corrections retain the post ID,
publication date, and replies. Unchanged content creates no new revision. Failed
fetches retain saved content and retry with backoff.

For another source, configure its URL, board, and stable `source_id`. Use that
same ID and board on every importing host. Do not reuse it for unrelated sources
or change it merely because a publisher moved URLs. Summary-only feeds remain
summaries unless the supported Markdown source supplies their full text.

```sh
mesh-bbs --region colorado-mesh import-feeds
mesh-bbs --region colorado-mesh command 'news latest'
```

The first command checks due sources once; `--force` bypasses the scheduled
wait. Automatic polling requires `serve`. Read the exported RSS at
`/feeds/news.xml`. NomadNet shows the ten newest entries on its home page and
newest threads first on each board, with complete posts and conversations.

News accepts imports only. Human threads and replies belong on community boards;
legacy editor settings do not override this. Historical content remains readable.

### Correct or remove community content

The local operator can revise their own community post or record a removal:

```sh
mesh-bbs --region colorado-mesh edit POST_ID --body-file corrected.txt --operation correction-1
mesh-bbs --region colorado-mesh remove POST_ID --operation removal-1
```

Reuse an operation ID for an exact retry. Other replicas accept removals under
the origin's authorship authority or an explicit moderation grant. A removal
stops serving content but does not erase historical events, backups, or copies
already downloaded by readers.

## Back up and recover

The database contains private signing material, drafts, receipts, contributor
key hashes, and post/removal history. Keep it out of public issues and artifacts.
For a consistent live SQLite snapshot:

```sh
mkdir -p ~/mesh-bbs-backups
chmod 700 ~/mesh-bbs-backups
mesh-bbs --region colorado-mesh backup ~/mesh-bbs-backups/colorado-snapshot.sqlite3
```

Choose a new destination each time; backups do not overwrite an existing file.
This includes the signing key, but not the TOML config, Reticulum interface
profile, or separate transport identity and delivery state.

For a full backup, stop the service and copy the config directory, entire data
directory, and Reticulum profile if stored elsewhere. A bare copy of a running
SQLite file can miss WAL changes. Keep a backup on another device and rehearse
restoring it into a temporary directory with radios disabled.

To recover, stop the old host, preserve its current files, restore matching
configuration/database/transport state, check private permissions, run `doctor`,
and start it. Retain the region, tombstones, history, budgets, and identities.
Verify peers catch up. Do not run a backup clone alongside the original; a new
host needs a new identity. Losing that identity requires re-pairing with peers.

Logs can contain addresses, identifiers, and connection errors. Redact private
keys, subscription secrets, and message content before sharing diagnostics.

## Automatic updates

[Enable the updater](updates.md) to follow CI-approved pushes without rerunning
the installer yourself. Preparation leaves the current service running; the
final switch briefly restarts it. Check `updates status` and the service logs.
SIGUSR1 announce requests are forwarded to the live Reticulum child.

## Troubleshooting checklist

| Symptom | Check |
| --- | --- |
| No radio DM response | `/readyz`, serial ownership, actual modem/channel, reply budget, service logs |
| No public `help` response | Exact `help`, correct channel slot/key, designated owner, five-minute cooldown, budget |
| Node advert not seen | Clock, firmware role/contact type, matching RF settings, nearby reception; SDK OK is not reception |
| LXMF delivered but no command result | Correct BBS LXMF address, sender announce/identity, signature errors, reverse path |
| NomadNet last seen is old | Open the full address; inspect local announce logs and relay reachability |
| Feeds update but peers do not | `peer list`; same region and explicit trust on both hosts |
| Remote web reading works but posting fails | HTTPS, exact `public_url` including port, proxy headers, valid contributor key |
| New board absent on another host | Compatible versions and future-board grant, or update its explicit board list |

For automated validation and its limits, see [development](development.md).
