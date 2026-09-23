# Radio recovery design and verification

Radio access must recover without making web reading or newsletter imports
wait for a device. This behavior is implemented; this document records the
contract and the regressions its tests protect.

## Connection ownership

Each configured MeshCore or Meshtastic adapter has one supervisor. A missing
or disconnected radio retries with bounded exponential backoff. A stable
connection resets the wait. Shutdown interrupts retry waits and closes the
client before its storage or budget disappears.

The adapter retains its SDK client until cleanup succeeds. A cleanup exception
must not permit a replacement beside a possibly live old connection. Parent
cancellation must survive worker shutdown, including SDK cleanup that consumes
cancellation internally. Readiness changes before closure completes.

Other protocols, web access, and feeds continue during a radio outage.
Reticulum's process-wide runtime has its own lifecycle. Reconnects reuse the
persisted airtime budget, cursors, drafts, and receipts; they never create a
fresh allowance. Queued in-memory replies may be lost, so applications must
retain safe operation/publication retries.

## Health semantics

`/healthz` answers whether HTTP is alive. `/readyz` describes configured radio
states and returns 503 while one is unavailable. Neither proves reception over
the air or convergence between peers. A terminal failure requires investigating
the cause; it should not prompt an endless restart loop that disrupts working
interfaces. See [operations](operations.md#health-and-readiness).

## Regression coverage

| Boundary | Test expectation |
| --- | --- |
| Initial connection | Missing radio does not prevent web/feed startup |
| Reconnect | Old connection closes before replacement; waits back off |
| Shutdown | Cancellation survives cleanup and interrupts waits |
| Budget | Reconnect and restart retain debt and unfinished reservations |
| Requests | Stable operation receipts survive loss of a response |
| SDK protocol | Real SDK handshakes, framing, callbacks, and lost-ACK behavior work against loopback peers |
| Discovery | Notice/help/adverts use bounded budgets and persistent cooldowns; uncertain sends do not become retry storms |
| Reticulum announces | Scheduled deadlines survive restart; manual/startup announces do not defer them; failures leave later attempts running |

Additional regressions cover bounded draft-ID collision retries and starting an
HTTP IP listener without a reverse-DNS lookup. Security dependency upgrades are
checked against signed-event and real Reticulum tests. Intel macOS exercises
native crypto compilation and the all-protocol installer in CI.

## Run and interpret the checks

Use the separate processes in [development](development.md). Simulated SDK
contracts establish application decisions; wire emulators add real SDK framing
and timing. They are not firmware CPU emulators, RF measurements, or evidence
of licensed station operation. Tests use temporary databases and loopback ports.

A field pilot still needs actual devices, agreed RF settings, measured upper
packet estimates, busy-channel observation, and reconnect exercises. Record
what was received at the other end, not merely what the SDK accepted locally.
