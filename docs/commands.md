# Read and post by message

Open a private chat with the BBS companion on MeshCore or Meshtastic. For
Reticulum, use the BBS's **LXMF messaging address**. Ask the operator for the
contact if you cannot find it. The same menu works on all three protocols.

## Start here

Send `help`. The menu offers:

```text
1 News & newsletters
2 Browse boards
3 Write/resume a post
```

Send the number beside your choice. `next` requests another page, `back` returns
to the previous list, and `menu` returns to the start. The bot sends one response
per request. Menu numbers refer to the choices you saw, even when new posts
arrive before you reply.

In a configured discovery channel such as MeshCore `#bbs`, send exactly `help`
for instructions to contact the BBS privately. Only the designated host answers,
and public help replies are limited to one every five minutes. The channel is
for discovery, not for posting or reading whole articles.

## Open a channel notice

A notice might say:

```text
New post in running:
"mesh runners!"
To read it, send me a private message: read #7
```

Send **`read #7` to the node that sent the notice**. Send `next` when prompted
to keep reading. This number stays attached to the same post after new arrivals
and host restarts. It works across that host's protocols, but another BBS host
may assign a different number. It is different from a temporary menu choice
such as `1`.

A new-board notice asks you to send `boards`; choose the board from that menu.
Old notices with hexadecimal IDs still work through `read ID` and `more`.

## Write a post or create a board

1. Send `help`, then `3`.
2. Choose a board, or choose **Create a board** and send its name.
3. Send your title.
4. Send the post's text in one or more short messages. Each is saved in the draft.
5. Send `done` to review. Use `next` if the preview spans several pages.
6. Send `publish` to make it public. Repeating `publish` returns the same post.

`add` returns from preview to writing. `cancel` discards the unpublished draft.
To browse without losing it, send `menu`; choose `3` later to resume. Drafts
survive a restart on that host. Switching apps, addresses, protocols, or hosts
does not move your draft to a new identity.

When reading a community post, `reply` starts a reply draft and `replies` shows
the conversation. **News accepts automatic imports only.** Nobody can post or
reply there, including editors. Start a thread on a community board to discuss
an issue.

Board names use lowercase letters, numbers, and hyphens, up to 64 characters.
The guided flow converts spaces to hyphens. A host allows 32 boards and eight
new boards per sender per day. New boards sync to peers that explicitly grant
access to future boards.

## If a response is missing

Allow for radio delivery and the host's reply budget. A radio ACK is not a BBS
publication receipt. The service accepts at most 12 requests per sender per
minute, and an exhausted airtime budget can delay processing.

For a draft, retrying `publish` is safe. Resending plain body text after a lost
confirmation can append it twice, particularly on MeshCore, whose companion
messages have no stable packet ID. For unreliable links, use the explicit
operation IDs and numbered parts below. Do not interpret identical text as a
safe retry identifier.

## Explicit commands

These are useful for scripts, packet sessions, and users who prefer commands.
Send `commands` for the reference, then `more` to page through it.

| Command | Result |
| --- | --- |
| `boards` | Board choices in a DM; a listing in a packet terminal |
| `threads general` | Recent threads on `general` |
| `read ID` | Open a post by full ID or an unambiguous hexadecimal prefix |
| `read #7` | Open this host's permanent shortcut from a notice |
| `thread ID` | List a conversation's posts and replies |
| `news latest` | Open the latest saved newsletter |
| `more` | Next page of an explicit-command response |
| `post general Title | Text` | Publish a short post immediately |
| `new general Title` | Create a multipart draft |
| `add DRAFT 1 Text` | Save numbered part 1 |
| `preview DRAFT` | Read the unpublished draft |
| `publish DRAFT` | Publish once, or return its previous publication |
| `discard DRAFT` | Remove an unpublished draft |
| `reply ID Text` | Create a reply draft, still requiring publication |

Replace `ID` and `DRAFT` with the identifiers returned by the bot. Each radio
command must fit its packet: the MeshCore limit is 160 UTF-8 bytes including the
command and operation ID. Meshtastic replies default to 160 bytes as well.
Emoji and accented characters may consume several bytes each. A complete post
can contain 64 KiB of UTF-8 text and a title up to 256 bytes.

### Safe retries

Prefix an explicit command with a unique `@operation-id`:

```text
@meetup-1 post general Saturday meetup | Bring a radio. Meet at nine.
```

The first `|` separates title from body; later pipes are body text. This command
publishes immediately. If its response is lost, resend the **exact command with
the same ID to the same host from the same address**. The saved response is
returned without another write. Changed content under that ID is rejected.
Use a new ID for a new action. Equal text sent without an ID may create another
post. “Saved locally” does not promise that another host has synced yet.

Give each page request its own ID too: `@page-1 read ID`, then `@page-2 more`.
Repeating a request with its original ID returns the same page rather than
advancing again. Native Meshtastic/LXMF retransmissions also have request
receipts; a manual resend may be a new transport operation.

### Multipart example

```text
@cleanup-1 new general Saturday cleanup
add DRAFT 1 Meet at the trailhead at nine.
add DRAFT 2 Bring water and work gloves.
preview DRAFT
publish DRAFT
```

Replace `DRAFT` after the first response. Parts start at one and join with line
breaks. Resending a part with the same number and text is safe. An explicit
reply follows the same preview/publication steps:

```text
@reply-1 reply ID I'll be there.
preview DRAFT
publish DRAFT
```

## Packet terminal

An operator-provided packet session uses the explicit commands, one per line;
it does not enter the numbered DM menu. `quit` ends the BBS session. Posting
requires the operator's `--allow-posts` setting, and every command is restricted
to their selected boards. See [terminal integration](transports.md#cleartext-packet-terminal).
