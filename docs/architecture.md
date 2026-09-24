# Architecture and data contracts

A host runs one SQLite store and a shared command service. Optional adapters
translate incoming messages into commands; HTTP and NomadNet render the same
saved posts. Trusted hosts exchange signed events over Reticulum. Mesh Client
and custom radio firmware are not dependencies.

```text
MeshCore / Meshtastic / LXMF / packet terminal --> commands
Web contributors ------------------------------> authenticated writes
RSS / Atom / newsletter Markdown --------------> importer
                                                      |
                                                      v
                                             SQLite + signed events
                                                |             |
                                     web / NomadNet / RSS   Reticulum peers
```

## Identity and trust

A database belongs to one immutable region. Another community needs its own
configuration, data, and identity. A shared region name does not establish
trust, radio settings, or peer connectivity.

The database holds an Ed25519 signing key. Its `origin` is the SHA-256 of the
raw public key. Reticulum uses a separate persistent transport identity, with
distinct sync, LXMF, and NomadNet destinations. Peer configuration binds that
transport identity to access grants and separately trusts the signing origins
whose events may be accepted. Relaying cannot replace an event's signature.

Authors are protocol-qualified addresses. MeshCore contacts use full public
keys; LXMF requires a validated message signature. Meshtastic node numbers and
packet callsigns are gateway-attested addresses, not proof of human authorship.
A host signature means the host accepted the event; it is not a signature by
the person named in the post. Web identities come from server-issued keys.

## Events and projection

Canonical versioned JSON, a SHA-256 event ID, and an Ed25519 signature describe
each event. It carries a region, board, author, permanent post/thread/parent
references, and clock. Ordinary post IDs bind the host, actor, and publication
operation. Another host cannot create a new ordinary post under that ID.

Posts are a projection of accepted events. A reply retains its parent reference
when the parent is missing. Early revisions and removals wait for creation.
Revisions cannot move a post to another board or parent. An ordinary revision
requires the original host acting for the original author. Moderation of other
hosts' content requires a separate grant.

Logical order and UTC milliseconds form the clock. Events over five minutes in
the future are rejected; keep host clocks synchronized. Concurrent permitted
revisions resolve by `(clock, event_id)`. Removal is terminal and invalidates
read cursors and cached read responses. Historical events and downloaded copies
remain; removal is not cryptographic erasure.

A signed `board` event identifies an empty board by region and slug. It is not
a post. With an explicit `allowed_boards = ["*"]` grant, a peer may add boards
from events, including when a post arrives before its board event. Concrete
board grants never expand implicitly. Every event still needs its original
signing host's grant. Older peers need upgrading before this extension is used.

## Atomic writes and retries

Accepted changes and their operation receipts commit in one SQLite transaction.
The same operation with changed content is rejected. A different operation can
publish identical text as a separate post. Numbered draft parts and durable
draft publication provide application-level retries on every transport.

Command receipts return the previous response instead of advancing a cursor or
appending text again. Explicit `@operation-id` receipts persist. Native
Meshtastic packet IDs have a 24-hour lifetime because those finite IDs can be
reused. MeshCore has no native message ID: the adapter fingerprints the resolved
full sender key, uint32 sender timestamp, and exact UTF-8 text together. Firmware
forwards those unchanged across attempts; route and signal metadata are excluded.
The resulting receipts expire 24 hours after processing, using the host clock.
They commit with the command and survive reconnects and host restarts. Timestamp
alone and text alone are not retry identifiers. Identical text from the same
sender with the same timestamp is indistinguishable from a retry; use a new
timestamp or explicit operation ID for a separate submission.

Radio retries already queued or being answered are coalesced before reserving
another queue slot or sender allowance. A later retry receives the stored response
under the normal airtime budget, without repeating a write or advancing a cursor.
See the pinned firmware's [receive handling](https://github.com/meshcore-dev/MeshCore/blob/d929643/src/helpers/BaseChatMesh.cpp)
and [companion forwarding](https://github.com/meshcore-dev/MeshCore/blob/d929643/examples/companion_radio/MyMesh.cpp).

The short-post command and multipart publication use the same policy and
transaction boundaries. A radio ACK is not a commit receipt. “Saved locally”
confirms storage here, not peer convergence. Radio transmission queues are
bounded and in memory; an interrupted reply may require a safe user retry.

`resend` (also `again` or `repeat`) returns the last successful command response
for that sender without executing the command or changing navigation. The reply
commits with the command before transport delivery, so an unacknowledged reply
is recoverable. The cache holds one bounded response per sender, expires after
24 hours, and keeps at most 4,096 senders. Read responses retain their revision
reference for removal invalidation. Replaying an older request receipt does not
replace the current navigation or last-reply cache.

## Menus, reading numbers, and long posts

Numbered DM menus save bounded snapshots. An arrival cannot change what a
previously displayed choice selects. Menu state, draft writes, publication,
and request receipts share a transaction. Browse-only sessions expire after
24 hours; sessions holding drafts remain resumable.

`read #7` resolves a permanent **local** alias in `post_numbers`. It is allocated
when a notice is consumed, in the same transaction. A number is never reassigned,
even after removal, and all protocols on that host resolve it to the same
canonical post. It is not a replicated identifier or a menu-page position.
Older hexadecimal references continue to resolve. All access policies apply
to the resolved post. A shortcut can open a post without discarding a draft.

Readers fetch one bounded page at a time. Cursors pin a revision; removal
invalidates them. Explicit board/thread listings use keyset continuation IDs.
Core posts are limited to 64 KiB UTF-8, titles to 256 bytes. Radio replies default
to 160 bytes, LXMF pages to 4096. Transport framing can reduce the available
channel-notice budget. Truncation keeps complete UTF-8 characters and reading
instructions.

Packet sessions use the explicit command interface with a board allowlist;
they cannot enter the unrestricted numbered DM menu.

## Feed imports

`(region, source_id, item_id)` identifies an imported article. Keep source IDs
consistent between hosts; RSS/Atom item IDs or stable URLs identify items.
Duplicate imports converge on one post. Corrections retain its ID and produce
revisions. A source is bound to its board so configuration changes cannot move
an existing issue silently.

Publication dates determine display order in UTC; undated items sort behind
dated ones. Fetch order and old-issue backfills do not redefine “latest.”
Configured RSS/Atom content becomes plain text with attribution. Colorado's
published newsletter links can select a sibling `draft.md` under a configured
HTTPS base. This does not scan unpublished issues, extract PDFs, or fetch images.
Summary-only content remains a summary without an available text source.

Fetching has byte, item, timeout, redirect, and decompression limits. Network
work happens outside database transactions. Short claims prevent overlapping
polls from replacing newer results. Content, validators, and the next poll time
commit together; backoff survives restart. Shutdown drains pending work before
closing storage.

News accepts automatic imports only. Human posts and replies are rejected
across web, messaging, CLI, and packet access, including editor identities.

## Replication

`meshbbs.sync` serves `/sync/v1/exchange` to configured Reticulum identities.
Envelopes identify their protocol version and region. Peers exchange bounded
inventories and request missing signed events. Both transport access and the
original signing host's board grant are required.

A cursor advances after every event in its inventory page has been accepted.
Interrupted transfers request only missing events. Completed sweeps start over
so an event inserted before an old cursor is eventually found. Each peer pulls
independently; there is no write leader or implicit trust in a relay's peers.

Defaults allow 128 inventory IDs, 32 requested IDs, and responses below 512 KiB.
Per-sweep request, byte, and elapsed-time limits bound work. Reticulum resources
carry larger content. Sync does not broadcast posts over each access radio.

## Access boundaries

| Interface | Input policy |
| --- | --- |
| MeshCore | Companion DMs; resolve the six-byte prefix to one full contact key; ignore unknown/ambiguous senders |
| Meshtastic | Direct text to this node; gateway-attested sender; firmware owns retries |
| LXMF | Verify the signed message and identify the actor by LXMF destination |
| NomadNet | Public reading; plain bodies stay literal; marked bodies use the bounded display-only [Micron subset](micron.md); no CGI/subprocess execution |
| Web | Public reading/RSS; authenticated writes; loopback listener by default |
| Packet terminal | Trusted launcher supplies callsign and explicit board allowlist; read-only by default |

A separate opt-in channel path handles discovery notices and exact `help`.
Only the designated owner transmits, with persistent cooldowns and replay
suppression. It does not turn channel conversation into commands or posts.
See [operations](operations.md#channel-announcements) for owner and queue rules.

## Web publication

An operator-issued bearer key maps to a permanent `web:NAME`. Only its hash is
stored. Revocation never frees the name for another user. Identity and privileges
come from authenticated server records, not fields in a request.

Public pages are server-rendered. `/connect` signs in; `/new/BOARD` and
`/reply/POST_ID` compose posts. Keys stay in tab session storage and travel in
Authorization headers, not URLs or cookies. Drafts use browser local storage.
They remain local to that browser and origin.

Writes require a loopback origin or the configured HTTPS public origin, bounded
JSON, and a valid key. Publication rechecks revocation and board policy inside
the write transaction. Thirty new publications per contributor per minute are
allowed; receipt replays do not spend another publication allowance.

The client preserves an uncertain operation and payload for retry. It avoids
overwriting another tab's newer draft and ignores stale authentication results
from a different key. See [web design](web-gui-plan.md) for regression coverage.
