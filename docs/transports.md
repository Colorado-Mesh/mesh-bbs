# Add a transport or packet terminal

Transport adapters deliver commands to `CommandService`; the store owns post
identity, drafts, paging, and deduplication. An adapter must identify the sender
using a stable, protocol-qualified address. Display names and channel names do
not establish identity. A gateway attests the address it received; a host's
event signature does not prove that a human author signed the text.

## Radio adapter contract

`adapters/base.py` defines `IncomingMessage` and `QueuedRadioAdapter`. The
MeshCore and Meshtastic adapters provide examples of receive parsing, bounded
queues, and delivery handling. Optional radio libraries are imported only when
their adapter starts, so core CLI commands remain usable without them.

For another message transport:

1. Validate the recipient, sender, text encoding, and payload size before
   queueing. Ignore broadcasts unless a separate discovery feature explicitly
   handles them. Ignore echoes from the BBS's own address.
2. Map the sender to a stable namespace such as `protocol:address`. Keep local
   operator identities and editor privileges out of remotely supplied data.
3. Set the actual UTF-8 payload budget, including any application framing.
   Submit a single command and send its single bounded response. The reader
   requests another page with `more`; do not enqueue the rest of a long post.
4. Pass a native message ID only if the protocol provides a usable identity for
   that request. Scope it to the sender and use a finite lifetime when IDs can
   be reused. Otherwise users can supply `@operation-id COMMAND`. Equal text is
   not sufficient evidence that two commands were the same operation.
5. Bound pending requests and schedule transmissions for the actual link.
   Report delivery failure; do not turn an unacknowledged send into successful
   delivery. A stored post and a delivered confirmation are different states.

Implement tests with fake transports first. Verify library calls against the
pinned dependency, then test the real protocol in an isolated environment.
Local protocol tests do not establish RF reliability or permissible airtime.

## Cleartext packet terminal

`mesh-bbs terminal` reads commands from standard input and writes responses to
standard output. It opens no network listener or radio device. Attach it as an
application session in an operator-managed terminal switch after that switch
has associated the connection with a station callsign.

For a local pipe demonstration:

```sh
printf 'boards\r\nnews latest\r\nquit\r\n' | mesh-bbs --region colorado-mesh terminal --callsign N0CALL --boards general,news --max-bytes 256
```

The callsign is supplied by the trusted gateway launcher, not by a prompt inside
the BBS. `N0CALL` is an example. Replace it with the station address established
by your terminal switch. Pass arguments as separate process arguments; do not
interpolate a remotely supplied callsign into a shell command. The adapter
checks AX.25-style address syntax and an optional SSID from 0 through 15. That
check is not authentication or license verification.

An operator integrating BPQ, LinBPQ, or a Linux AX.25 application must provide
the session launcher and callsign association for that installation. The BBS
does not implement AX.25 connection management, a BPQ login protocol, raw KISS,
or a TNC driver. Connecting a KISS byte stream directly to this command is not
a supported setup. Test the session handoff with local pipes before configuring
an RF interface.

Each command occupies one input line. CR, LF, and CRLF are accepted, along with
a final unterminated line at EOF. Input must be UTF-8 without control characters.
Overlong or malformed input is rejected; the next complete line can still be
processed. `quit` and `exit` close the BBS session. The terminal switch remains
responsible for disconnecting the remote station.

The default maximum is 256 bytes per output frame, including its CRLF ending.
Input commands have the same limit minus those two framing bytes. Limits from
66 through 8192 bytes are supported; choose a limit your packet application can
carry. Responses contain no terminal control characters. Paragraph breaks are
shown as spaces, and each response is one line. Long listings and posts retain
the shared `more` interface. The process writes and flushes one response at a
time without an application transmission backlog. The gateway must enforce
session timeouts, connection limits, and actual radio pacing and airtime limits.

`--boards` is a required, explicit list of locally configured public boards.
It restricts board listings, newsletter browsing, post reads, thread listings,
drafts, replies, and continuation cursors. A later session with a different
allowlist cannot continue a page or replay a read receipt from the broader
policy. Existing cursors survive reconnection when the policy and frame budget
are unchanged. Two simultaneous sessions using the same station identity share
the reading position and drafts; the gateway should allow one active session
per station identity.

Sessions are read only by default. An operator may add `--allow-posts` to permit
drafts and replies on the selected boards. Authors are recorded as
`packet:CALLSIGN`, including a nonzero SSID. The greeting labels that identity
as gateway-attested. Another connection asserted as the same station has
access to its drafts and retry receipts, so the gateway's station association
must be trustworthy. This interface never grants editor rights, even when the
underlying command service has an editor entry with that address. It cannot
post or reply on News. With posting enabled it can write on selected community boards.

Numbered draft parts support long posts without sending a large radio packet:

```text
@cleanup new general Saturday cleanup
add DRAFT 1 Meet at the trailhead at nine.
add DRAFT 2 Bring water and work gloves.
preview DRAFT
publish DRAFT
```

Replace `DRAFT` with the identifier returned by `new`. Keep the same `@cleanup`
identifier when retrying that draft-creation command. Retrying `publish DRAFT`
returns the original post. Drafts and receipts survive a process restart on the
same host; this does not transfer a draft to another host.

The Python integration entry point is
`run_terminal(service, input_stream, output_stream, *, callsign,
allowed_boards, max_bytes=256, allow_posts=False)` in
`mesh_bbs.adapters.terminal`. Streams are binary file objects. The supplied
`CommandService` provides storage; the terminal creates its own restricted
command view and does not inherit editor privileges.

## Amateur-radio operation

This interface provides a separate cleartext application path. It does not
authorize encrypted Reticulum federation on amateur bands or establish that
every board is suitable for retransmission there. The station operator chooses
which public boards to expose and whether incoming posts are allowed, and
configures identification, third-party traffic handling, and station control
for the intended service and jurisdiction.

For a U.S. deployment, review the applicable FCC Part 97 requirements, including
[prohibited transmissions](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-B/section-97.113),
[third-party communications](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-B/section-97.115),
[station control](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-B/section-97.109),
and [station identification](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-B/section-97.119).
The project tests use memory streams and local processes, not licensed station
operation or hardware interoperability tests.
