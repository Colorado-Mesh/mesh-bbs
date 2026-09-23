# Architecture and protocol contract

## Host and community identity

One database belongs to one immutable region slug. Running another community
requires another database and configuration. Matching a region name does not
establish trust or configure frequencies. Operators exchange public keys and
explicit board grants.

Each host persists an Ed25519 signing key in its private SQLite database.
`origin` is the SHA-256 of its raw public key. Reticulum uses a separate
persistent identity for its transport destinations. The configuration binds a
transport identity to its board access and separately trusts the original
signing keys of every host whose events it will accept, including forwarded
events. An intermediary cannot replace the original signature.

## Events and ordering

Events use a versioned canonical JSON representation, a SHA-256 content ID, and
an Ed25519 signature. They carry their region, board, author, permanent post ID,
thread ID, and parent ID. Ordinary creation IDs are bound to the originating
host, actor, and publication operation. A trusted peer cannot create a new
ordinary post using someone else's post ID.

Replies whose parents have not arrived retain the parent ID. Revisions and
removals received before a creation are retained and applied once the original
arrives. Revisions cannot change a post's board or parent. Only the originating
host can revise an ordinary post on behalf of its original author. Remote
removal authority is a separate explicit peer grant.

The event clock combines observed logical order with UTC milliseconds. Events
more than five minutes in the future are rejected to prevent a corrupt clock
from disabling local writes. Operators should keep host clocks synchronized.
Concurrent permitted revisions use `(clock, event_id)` for deterministic
selection; all accepted events remain in history. A removal is terminal and
also invalidates reading cursors and cached read responses. Removal hides
content from the service; it is not cryptographic erasure of historical events
or other people's downloaded copies.

## Deduplication and transactions

Publishing a post and storing its operation receipt use one SQLite transaction.
Reusing an operation ID with changed content is rejected. Numbered draft parts
are idempotent, and publication uses the durable draft ID. A fresh identical
post with a different operation ID remains a distinct post.

Incoming transport receipts replay the prior command response rather than
advancing a reading cursor again. Meshtastic's finite native packet IDs expire
after 24 hours; explicit `@operation-id COMMAND` receipts remain persistent.
MeshCore companion messages do not expose a stable packet ID, so the BBS does
not pretend text equality is a reliable identifier. Draft publication provides
safe application retries on all transports.

The single-message `post BOARD TITLE | TEXT` command uses the same permissions
and limits as draft publication. Each new command gets a fresh publication
operation; its post and command receipt commit together. An explicit `@ID`
or usable native request ID replays the result. Equal unlabelled text remains
a new submission, so manual retries should keep their original `@ID`.

A transport ACK does not mean the BBS committed a post or that another host
has replicated it. The application returns a saved-local receipt. Radio queues
are bounded and held in memory: after a restart, a user may need to retry a
command whose application receipt never arrived. Draft publication remains
idempotent across that retry.

## Newsletter import

Source IDs must be consistent across hosts. `(region, source_id, item_id)`
identifies each imported article; item IDs come from RSS/Atom IDs or stable
article URLs. Repeated imports and failover create one logical post. Corrections
create revisions. Source IDs are bound to a board so changing configuration
cannot move an existing issue into a different board accidentally.

Feed publication dates determine chronological display, normalized to UTC.
Undated entries sort behind dated ones. Fetch order, corrections, and later
historical backfills do not decide which issue is latest.

Parsing and fetching have byte, item, timeout, redirect, and decompression
limits. The importer reads configured HTTP(S) feeds, not arbitrary URLs from
radio messages. It does not scrape linked articles, run document scripts, or
fetch images. Feed content is converted to plain text and attribution retained.
Summary-only entries are labelled. A complete blog post may itself only be an
introduction to a linked PDF; the feed importer cannot infer missing content.

Validators, article writes, and the next poll time commit together. Short-lived
claims prevent overlapping polls from replacing newer results. Network work
holds no database transaction; shutdown drains in-flight work before closing
storage. HTTP validators and failure backoff survive restart.

## Reticulum federation

`meshbbs.sync` exposes `/sync/v1/exchange` only to configured Reticulum identities.
Every application envelope includes its region and protocol version. Peers
exchange bounded inventories and request missing events. A signature and an
original-host board grant are required even when another trusted peer forwards
an event.

Cursors advance only after every event in an inventory page has been accepted.
Partial transfers resume by requesting only missing IDs. A completed sweep
restarts at the beginning so events inserted before an old cursor are eventually
discovered. Each peer pulls independently; no permanent leader accepts writes.
Request count, total bytes, and elapsed time are bounded per sweep.

Default limits are 128 inventory IDs, 32 requested event IDs, and responses
below 512 KiB. Resource transfers handle larger posts. Replication does not
broadcast content onto every access radio.

## Access adapters

An adapter provides a verified or explicitly attested sender address and a
message to the shared command handler. `IncomingMessage` contains `protocol`,
`sender`, `text`, and an optional native `message_id`. The handler returns one
bounded text response. The transport handles queueing and delivery status;
posts, drafts, receipts, and reading cursors belong to the core.

- MeshCore: companion DMs only; uniquely resolve the six-byte contact prefix to
  a full contact public key. Ignore unknown or ambiguous prefixes. No Room Server.
- Meshtastic: direct text messages only. Node numbers are addresses attested by
  the gateway, not proof of a person's identity. They cannot grant newsletter
  editor privileges. Firmware handles packet retransmission.
- Reticulum: require a validated LXMF signature. The actor is
  `reticulum:<LXMF destination hash>`, distinct from a server's RNS identity.
- NomadNet: public reading at the `nomadnetwork.node` destination; no CGI or
  external subprocess rendering. User content is rendered as Micron literals.
- Web: public server-rendered reading and RSS, plus authenticated publication
  through the standalone web interface. The listener defaults to loopback.

MeshCore and Meshtastic responses default to 160 UTF-8 bytes including page
markers. LXMF command pages use 4096 bytes. Full posts remain bounded at 64 KiB.
`more` reads a pinned revision; board and thread lists use keyset continuation
IDs so older entries stay reachable.

The cleartext terminal interface accepts an already-associated stdin/stdout
session from an operator-managed packet switch. It requires an explicit board
allowlist, starts read-only, and never grants editorial rights to a callsign.
See [transports](transports.md) for integration and session policy. The switch
owns AX.25 connections, station identification, and RF scheduling; encrypted
Reticulum synchronization must not automatically be sent over amateur bands.
Native TNC management, subscription notifications, attachments, and automatic
PDF extraction remain follow-up work.

## Web contributor access

The operator issues a bearer access key with `web-access create NAME`. The
database stores its hash and a permanent `web:NAME` identity. Revoking a key
does not release its name for reassignment. These identities are separate from
the same person's MeshCore, Meshtastic, or LXMF address; a web login does not
claim cryptographic authorship on their other protocols. Access keys are
credentials, unlike the host's public federation identity.

Public reading requires no login. `/connect` accepts an access key for the
current browser tab. `/new/BOARD` and `/reply/POST_ID` provide forms for a thread
or reply. Browser drafts live in local storage, while the access key uses tab
session storage. Neither drafts nor keys are placed in URLs. Authentication
uses an authorization header, with no authentication cookies. Drafts are local
to that browser and origin, not replicated BBS posts.

Web writes require a loopback origin or a configured HTTPS public origin.
Operators terminating TLS at a reverse proxy set `public_url` to the external
HTTPS origin. The web server validates the origin and key before publication;
public reading does not grant write privileges.

The publication service rechecks contributor revocation and board policy in the
write transaction. `news` accepts automatic imports only; editor accounts cannot
post or reply there. Publication operation IDs provide durable
retries, and changing content under an existing ID is rejected. Thirty new
publications per contributor per minute are permitted; receipt replays do not
consume another publication allowance. The web interface stores the current
operation with its draft so retrying a lost response does not create a second
post. A saved-local receipt still makes no claim about federation delivery.


## Community boards and guided conversations

A signed `board` event announces an empty community board. Its ID is derived from
the region and slug. It never projects into a post. New posts can arrive before
the board event; a peer with an explicit `allowed_boards = ["*"]` grant may add
the named board. Ordinary explicit board lists do not expand automatically.
Federation inventory replies always contain concrete board names, and each event
still needs a matching signing-origin grant. Upgrade cooperating hosts before
enabling the new board-event/wildcard extension; older hosts do not support it.

Numbered DM pages retain bounded snapshots so concurrent arrivals cannot change
the selected target. Menu state, drafts, post publication, and request receipts
share the store transaction. Browse-only sessions expire after a day; draft
sessions remain resumable. Post readers use revision-bound cursors, invalidated
when the post is removed. Packet terminal sessions retain their explicit command
allowlist and do not enter the unrestricted DM menu.
