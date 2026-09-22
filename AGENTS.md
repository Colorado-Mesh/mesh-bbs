# Project guidance

- Use Python 3.12+ and uv. Keep the storage and command layers independent of
  radio libraries. Optional adapters must not prevent offline CLI use.
- New branches describe their work, for example `feature/newsletter-import`.
  Never create branches with agent, codex, or other assistant identity prefixes.
- Do not merge pull requests, publish releases, operate real radios, or change
  an installed application's profile without explicit user authorization.
- Every accepted operation and its deduplication record commit together.
  Preserve post IDs and parent IDs across gateways and replication. Never
  deduplicate independent posts merely because their text matches.
- Treat transport sender identities as gateway attestations. Do not imply
  cryptographic authorship by the human user when the protocol does not prove it.
- Keep per-packet UTF-8 limits, queues, retries, and persisted response cursors
  bounded. A slow reader must not cause an unbounded transmission backlog.
- Verify external API behavior from primary sources or installed dependency
  code. Distinguish fake-radio tests, real local protocol tests, and RF tests.
- Record material design decisions and reproducible operator commands in docs.
- Run lint, typing, and relevant tests before committing; do not skip hooks.
- Keep the SDK client until connection cleanup succeeds. Parent cancellation
  must survive worker shutdown, and reconnects must reuse the airtime budget.
- Run PTY/fork tests in a separate process: imported radio SDKs can leave
  dispatcher threads alive even after individual connections have closed.
