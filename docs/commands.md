# Post and read over a message connection

Send commands to the configured BBS service. On MeshCore and Meshtastic, open a
direct message to its companion node. On Reticulum, send an LXMF message to the
host's advertised BBS address. The host operator supplies those addresses.
MeshCore Room Servers and hashtag channels are not used for these commands.

## Start with the menu

DM `help` on **MeshCore, Meshtastic, or Reticulum/LXMF**. The interaction is identical:

```text
1 News & newsletters
2 Browse boards
3 Write/resume a post
```

Reply with a displayed number. `next` gets another page, `back` returns to the
previous list, and `menu` starts over. Numbers refer to the page you saw, even
if new posts arrive meanwhile. Reading never automatically floods the channel.

To write, choose **3**, select a board or **Create a board**, send a title, then
send the text in one or more messages. The bot saves each part and prompts you.
Send `done` to review, `next` for more preview, then `publish`. Publication needs
that explicit final message; repeating `publish` returns the same post. `add`
returns from preview to add more text. `cancel` discards the draft. `menu` then
**3** resumes it, including after the host restarts. Sessions are private to
that transport address on that host; switching apps/addresses does not move a draft.

When reading a community post, `reply` starts a reply draft and `replies` opens
the conversation. **News is read-only for everyone, including editors and replies.**
Colorado Mesh news comes from its configured automatic sources. Start a community
thread to discuss an issue.

Community board names use lowercase letters, numbers and hyphens (up to 64
characters). The guided radio flow converts spaces to hyphens. There are at most
32 boards per host and eight new boards per sender per day. Empty boards also
replicate when operators explicitly trust peers for all community boards.

Keep radio text messages short. A radio accepts at most 12 requests per sender
per minute, and the operator's airtime allowance also applies. If a body-part
confirmation is lost, blindly resending plain text can append it twice on
MeshCore. For an uncertain link, use the numbered-part commands below: retries
of an explicit part are safe. Meshtastic/LXMF transport retransmissions have
request deduplication, but a manually resent message may be a new operation.

## Optional one-message posting

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

All published community posts are public to readers of that board. News only
accepts automatic imports.

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

The explicit commands above are available through an operator-configured packet terminal
session (the numbered DM menu is not available there). Enter one command per line; `quit` ends the BBS session. Posting must
be enabled by the operator with `--allow-posts`, and only its selected boards
are available. Terminal sessions cannot publish official newsletter issues.
See [packet terminal setup](transports.md#cleartext-packet-terminal) for the
gateway launcher and required board selection.
