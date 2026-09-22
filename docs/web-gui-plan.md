# Web reading and posting

The existing HTTP reader has plain links and requires radio commands to post.
Build the standalone web interface first; Mesh Client integration is a later
step. Every interface uses the same local posts, full identifiers and replication.

## Design

A compact bulletin board: slate navigation, amber actions, a board sidebar and
thread rows. Use locally available Trebuchet for controls and Charter/Georgia for
long articles; no font downloads or frontend build step. Keep public pages server
rendered and readable without JavaScript. Progressive enhancement adds sign-in,
post/reply composition, previews and browser drafts. Mobile navigation wraps.

## Posting and identity

Operators issue individual access keys through `mesh-bbs web-access create`.
The server stores hashes, never accepts a caller-supplied author, and rechecks
revocation when publishing. Keys stay in tab session storage and travel only in
Authorization headers. Public hosting requires HTTPS. Writes require a matching
configured origin, JSON, bounded bodies and authenticated server-side board
policy. No cookies or cross-origin API access. Newsletter roots require editor
access; other contributors can reply. Identity remains separate across protocols.

Structured web posts use Store.publish's atomic event/receipt transaction.
Browser drafts preserve their operation ID across refreshes and uncertain sends.
The UI distinguishes local saving from later peer replication. Radio users gain
`post BOARD TITLE | TEXT`, retaining multipart drafts for long messages.

## Acceptance

- [x] Responsive boards, readable threads, full posts, timestamps and transport labels.
- [x] Accessible sign-in, new post, exact-parent reply, preview, drafts and clear errors.
- [x] Authenticated web posts and radio posts appear in the same boards/threads.
- [x] Contributor creation/revocation and newsletter permission enforcement.
- [x] Refresh/retry dedupe; no author impersonation or cross-origin writes.
- [x] XSS-safe rendering, strict body/field limits and bounded request handling.
- [x] Package includes CSS/JS; public reading still works without JavaScript.
- [x] Real browser desktop/mobile tests plus protocol/core regressions.
- [x] Operator docs explain setup, key distribution and protocol command access.

Handoff requires a passing [current-commit CI run](https://github.com/Colorado-Mesh/mesh-bbs/actions/workflows/ci.yml),
including the dedicated Chromium job. Browser tests exercise the real HTTP server
and SQLite using temporary identities. Protocol wire tests separately exercise
the real radio SDKs against loopback emulators.

## Review regressions

Retain a publication's operation and payload when a response is lost; an error
from a later retry cannot prove the first attempt failed. Associate asynchronous
authentication results with the key that initiated them. Check saved draft state
before overwriting or deleting it, so another tab's newer text survives. Display
partial threads even when their root has not arrived, and tolerate out-of-range
peer timestamps. These cases have regression tests.

No Mesh Client changes, PRs, releases or physical radio transmissions are part
of this iteration. Push validated work directly to the authorized BBS repository.
