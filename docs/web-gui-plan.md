# Standalone web interface

The web interface is implemented in Mesh BBS. It serves the same local boards
as radio and Reticulum users, with no Mesh Client dependency. This document
records its design and maintenance requirements.

## Reading and composition

Public pages are server-rendered and readable without JavaScript. The layout
uses board navigation, thread rows, readable article text, and mobile wrapping.
CSS and JavaScript ship in the Python package. Fonts come from the system;
there is no frontend build or font download.

JavaScript adds sign-in, creating boards, writing posts and replies, previews,
and browser drafts. Drafts stay local until explicit publication. News is
read-only, including replies and editor accounts; automatic sources supply it.
Community posts from the web use the same permanent IDs, parent references,
and replication as posts from other interfaces.

## Authentication and retry behavior

Operators issue individual bearer keys with `web-access create`. The server
stores hashes and assigns author/permissions from authenticated records.
Revocation is rechecked in the write transaction. Remote publication requires
HTTPS and the exact configured origin. Requests use bounded JSON and an
Authorization header, not authentication cookies or credentials in URLs.

A key remains in tab session storage. Drafts remain in local storage and may
outlive sign-out. Identities are separate across protocols; a web account does
not prove ownership of a radio address.

The draft retains a publication operation ID and payload through refreshes and
uncertain responses. A later failed retry must not erase evidence that an earlier
attempt might have committed. The server returns the original result for an
exact retry. “Saved locally” describes local storage, not remote peer delivery.

## Regressions to preserve

- Another tab's newer saved draft survives stale writes or deletion attempts.
- Delayed authentication responses cannot replace a different current account.
- Revoked credentials and client-supplied author fields cannot bypass policy.
- Cross-origin writes, oversized input, and script-bearing content are rejected
  or rendered safely at the appropriate boundary.
- Exact-parent replies and partial threads remain readable when a root has not
  arrived. Out-of-range peer timestamps do not break the page.
- Sign-in, previews, retries, and publishing work at desktop and mobile widths.
- Package installs contain the assets and can read public pages without JS.

Chromium tests use the real HTTP service and SQLite with temporary identities.
[Development](development.md) explains how to run them. Radio SDK and Reticulum
tests verify their separate access paths. Browser tests do not establish RF
coverage or compatibility with every browser.

[Contributor access](operations.md#web-contributors-and-public-access) covers
keys, HTTPS, Tailscale, and service configuration. A future Mesh Client screen
would be a separate integration, not a prerequisite for this interface.
