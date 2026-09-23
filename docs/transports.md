# Transport integration

Every messaging adapter calls `CommandService`. Storage owns IDs, permissions,
menus, drafts, paging, and deduplication. Adapters own recipient validation,
sender association, framing, queueing, and delivery. Keep SDK imports optional
so local CLI commands work without radio libraries.

## Add a message adapter

Use `IncomingMessage` and `QueuedRadioAdapter` in
[`adapters/base.py`](../src/mesh_bbs/adapters/base.py). MeshCore and Meshtastic
show the current implementations.

1. Validate the recipient, sender address, encoding, and input size before
   enqueueing. Ignore self echoes. Broadcast discovery needs a separate explicit
   policy; do not execute arbitrary channel messages as commands.
2. Give each sender a stable protocol-qualified address. A nickname is not an
   identity. Document which claims the protocol verifies and which the gateway
   merely attests. Never derive operator privileges from remote text.
3. Pass the available UTF-8 response budget, including framing costs. Execute
   one request and deliver one response. Let the reader request further pages.
4. Pass a native request ID only when it identifies a request reliably. Scope
   it by sender and expire finite reusable IDs. Support the core's explicit
   `@operation-id` receipts; identical text alone is not a duplicate identifier.
5. Bound queues, retry counts, timing, and connection recovery. A committed post,
   an SDK-accepted transmission, an ACK, and a delivered application receipt
   are different states. Preserve these distinctions in logs and tests.

Retain the SDK client until cleanup completes. Do not reconnect beside a
possibly live old connection. Preserve cancellation and the persisted airtime
budget across retries. See [radio recovery](reliability-plan.md).

Test contracts with simulated SDK objects, then use the actual pinned SDK with
an isolated protocol peer. MeshCore's current emulator speaks the companion
wire protocol; Meshtastic's speaks protobuf over TCP. Neither runs firmware or
establishes RF reliability. [Development](development.md) lists commands.

Primary protocol references:

- [MeshCore companion protocol](https://github.com/meshcore-dev/MeshCore/blob/main/docs/companion_protocol.md)
- [MeshCore Python SDK](https://github.com/meshcore-dev/meshcore_py)
- [Meshtastic Python library](https://meshtastic.org/docs/development/python/library/)
- [Reticulum](https://github.com/markqvist/Reticulum), [LXMF](https://github.com/markqvist/LXMF), and [NomadNet](https://github.com/markqvist/NomadNet)

## Cleartext packet terminal

`mesh-bbs terminal` serves one stdin/stdout session. It opens no radio or
network listener. Attach it to an operator-managed terminal switch after that
switch associates the station's connection with a callsign.

A local pipe demonstration:

```sh
printf 'boards\r\nnews latest\r\nquit\r\n' | mesh-bbs --region colorado-mesh terminal --callsign N0CALL --boards general,news --max-bytes 256
```

Replace the example callsign with the address established by the trusted
launcher. Pass process arguments separately; never interpolate a remote
callsign into a shell command. Syntax validation checks an AX.25-style address
and optional SSID 0–15, not station authentication or licensing.

BPQ, LinBPQ, or Linux AX.25 installations need their own session launcher. Mesh
BBS does not implement their connection management, a BPQ login protocol, KISS,
or a TNC driver. A raw KISS stream is not terminal input. Verify the handoff
with pipes before attaching an RF interface.

### Framing and limits

Commands occupy one UTF-8 line. CR, LF, CRLF, and a final line at EOF are accepted.
Control characters and oversized input are rejected without losing the next
complete line. `quit` or `exit` ends the BBS process's session; the switch owns
the actual station disconnect.

The default output limit is 256 bytes including CRLF, with input limited to
254 bytes. Supported frame limits are 66–8192 bytes. Responses strip terminal
control characters and flatten paragraph breaks into one line. Long responses
use `more`. One response is flushed at a time, without a transmission backlog.
The gateway must supply session timeouts, connection limits, pacing, and actual
radio airtime control.

### Board and author policy

`--boards` is mandatory. It selects local public boards and restricts listings,
reads, replies, drafts, and continuations, including short `#N` aliases. A later
session with a narrower policy cannot replay a broader read receipt or cursor.
With the same identity, board policy, and frame budget, cursors survive a
reconnect. Concurrent sessions for one identity share that position and drafts;
allow one active session per station.

Sessions are read-only unless the operator supplies `--allow-posts`. Writable
sessions can use the [explicit draft commands](commands.md#multipart-example)
on selected community boards. They cannot create boards through the DM menu
or post/reply on News. Packet access never grants editor rights.

Authors appear as `packet:CALLSIGN`, retaining a nonzero SSID. The greeting
labels this as gateway-attested. Another session asserted as that same station
can access its drafts and receipts, so callsign association is the launcher's
responsibility. Publication retries are local to this host and identity.

### Python entry point

`mesh_bbs.adapters.terminal.run_terminal` takes:

```python
run_terminal(
    service,
    input_stream,
    output_stream,
    callsign="N0CALL",
    allowed_boards=("general", "news"),
    max_bytes=256,
    allow_posts=False,
)
```

Streams are binary file objects and `service` is a `CommandService`. The
terminal builds a restricted command view and does not inherit editor grants.

## Amateur-radio deployments

The packet terminal is a separate cleartext application interface. It does not
route encrypted Reticulum federation onto amateur frequencies. The station
operator chooses suitable boards and posting policy and supplies station
identification, control, traffic handling, and RF configuration for the service.

For U.S. deployments, consult the applicable FCC Part 97 text on
[transmissions](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-B/section-97.113),
[third-party communications](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-B/section-97.115),
[control](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-B/section-97.109),
and [identification](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-B/section-97.119).
The project's pipe and protocol tests do not validate licensed station operation
or interoperability with a particular switch or radio.
