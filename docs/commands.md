# Post and read over a message connection

Send commands to the configured BBS service. On MeshCore and Meshtastic, open a
direct message to its companion node. On Reticulum, send an LXMF message to the
host's advertised BBS address. The host operator supplies those addresses.
MeshCore Room Servers and hashtag channels are not used for these commands.

For a short post, send one message:

```text
@meetup-1 post general Saturday meetup | Bring a radio. Meet at nine.
```

The first `|` separates the title from the text. Later pipes belong to the text.
The board, title, and text must all be present. `post` publishes immediately and
returns a post ID; it does not create a draft. The confirmation says "Saved
locally" because replication to other hosts happens separately.

Use a different `@operation-id` for each new command. If a confirmation is lost,
resend the exact command with the same ID to the same host. The host returns its
saved response without publishing again, including after a restart. IDs are
scoped to the sender, so switching protocol identities or hosts is not a safe
way to retry a write. Reusing an ID with changed text returns an error. Sending
the same text without an operation ID can create another post.

The `news` board accepts new issues only from configured editors. Other readers
can reply to an issue. All posts are public to readers of that board.

## Read a board or newsletter

```text
boards
threads general
read POST_ID
more
news latest
thread POST_ID
```

Replace `POST_ID` with an ID from a listing or publication confirmation. `read`
opens one post, while `thread` lists its replies. `more` requests the next page
when a response ends with `[more]`. To retry a page safely, use a distinct ID for
each page, such as `@page-1 read POST_ID`, then `@page-2 more`; repeat the same
command and ID if that page's response is lost.

## Longer posts and replies

Every command must fit the current connection's UTF-8 byte limit. MeshCore
allows 160 bytes, including the command and operation ID; multibyte characters
use more than one byte. Keep messages short on Meshtastic as well. The service
pages its radio replies and does not transmit a whole long post automatically.

For text that needs several messages:

```text
@cleanup-1 new general Saturday cleanup
add DRAFT 1 Meet at the trailhead at nine.
add DRAFT 2 Bring water and work gloves.
preview DRAFT
publish DRAFT
```

Replace `DRAFT` with the identifier returned by `new`. Parts are numbered from
one and joined with line breaks. Resending the same part number and text is
safe. `publish DRAFT` returns the same post on repeated attempts. Drafts survive
a restart of the same host. Use `discard DRAFT` to abandon an unpublished draft.

To reply to an existing post:

```text
@reply-1 reply POST_ID I'll be there.
preview DRAFT
publish DRAFT
```

`reply` returns a draft ID, so the reply becomes public only after `publish`.
Posts may contain up to 64 KiB of UTF-8 text and titles up to 256 bytes, but the
individual radio messages used to build them must fit the connection.

## Packet terminal sessions

The same commands are available through an operator-configured packet terminal
session. Enter one command per line; `quit` ends the BBS session. Posting must
be enabled by the operator with `--allow-posts`, and only its selected boards
are available. Terminal sessions cannot publish official newsletter issues.
See [packet terminal setup](transports.md#cleartext-packet-terminal) for the
gateway launcher and required board selection.
