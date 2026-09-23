# Contributor instructions

Mesh BBS is a standalone Python service. Read [README.md](README.md) for the
product and [docs/architecture.md](docs/architecture.md) for its data contracts.
Keep documentation and examples consistent with the behavior you change.

## Scope and workflow

- Use Python 3.12+ and the locked uv environment. Keep storage and commands
  independent of optional protocol SDKs; offline CLI use must still work.
- Name new branches for their work, such as `fix/radio-reconnect`. Never create
  or rename a branch with an `agent`, `codex`, or other assistant identity prefix.
  An existing branch with such a prefix may be updated without renaming it.
- Do not merge, publish releases, operate real radios, or change an installed
  app's profile without user authorization. Honor authorization already given
  for the task; use temporary profiles and emulators for ordinary tests.
- Verify dependency behavior against installed code or primary documentation.
  Report separately what unit tests, real protocol tests, and RF tests prove.
- Run lint, typing, and relevant tests before committing. Preserve normal hooks.
  Use [the development commands](docs/development.md) and check CI for the exact
  commit pushed. Record operator commands and material decisions in the docs.

## Data and identity

- Commit each accepted operation together with its deduplication receipt.
  Preserve permanent post, thread, and parent IDs through every adapter and sync.
  Independent submissions remain distinct even when their text matches.
- A transport address is a gateway-attested identity unless that transport
  proves more. A host signature must not be presented as a human signature.
- Keep removals, revision ordering, board grants, and originating-host checks
  intact. News accepts automatic imports only, including for editor accounts.
- Short `#N` reading numbers are local aliases. Never reuse or replicate them
  as post identities. Keep alias allocation and notice consumption atomic.
- Bound UTF-8 packets, input, queues, retries, and persisted response cursors.
  Readers request pages; do not queue whole articles for a slow connection.

## Connections and tests

- Retain an SDK client until cleanup succeeds. Do not open a replacement while
  the old connection might still be live. Parent cancellation must survive
  worker shutdown, and reconnects must retain the persisted airtime budget.
- Keep PTY/fork tests in a different process from radio SDK tests: SDK dispatcher
  threads can outlive a connection. Scope synchronous Playwright fixtures to
  their module so the loop closes before asynchronous protocol tests run.
- Test with temporary databases, identities, and loopback interfaces. Do not
  substitute a user's installed configuration for a test fixture.

## Automatically updated hosts

- A passing main push can deploy to opted-in hosts. Preserve compatibility with
  the previous config, database schema, and supervisor startup flags. Breaking
  migrations need an explicit operator upgrade path.
- Stage exact commits with locked dependencies. Keep the live process running
  during preparation, and never start two children with the same identity.
- Roll back application selection without restoring an old database over newer
  posts. Keep snapshots private and installation/snapshot retention bounded.

## Web writes

- Derive author and permissions from authenticated server records, never request
  fields. Recheck key revocation and board policy in the publication transaction.
- Preserve an uncertain publication's operation ID and exact payload on retry.
  A later error does not prove the first request failed to commit.
- Browser drafts must not overwrite another tab's newer text. Delayed sign-in
  responses must not replace a different current account.
