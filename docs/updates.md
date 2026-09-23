# Automatic application updates

Automatic updates are optional. A running host can follow new pushes to
Colorado-Mesh/mesh-bbs `main` after that commit's push CI succeeds. Checks run
every 15 minutes, starting about 30 seconds after startup. A failed, canceled,
missing, or unfinished CI run leaves the current application running.

## Enable it

In `mesh-bbs setup` or `mesh-bbs --region YOUR-REGION configure`, choose
**6 Automatic updates**. Existing hosts can use:

```sh
mesh-bbs --region colorado-mesh updates enable
```

Replace the region, then restart the service once. Foreground users can start
with `mesh-bbs --region colorado-mesh serve --auto-update`; this also saves the
opt-in setting. The usual systemd or launchd command remains `serve`. The
supervisor and the BBS run as the same normal user on Linux or macOS.

```sh
mesh-bbs --region colorado-mesh updates status
mesh-bbs --region colorado-mesh updates check
mesh-bbs --region colorado-mesh updates disable
```

`status` shows the active commit, last check, pending/failed commit, and recorded
errors. `check` queries GitHub now but does not install anything. `disable` stops
future background checks when the supervisor next reads its configuration; it
keeps the currently selected application version, including after restart.
Changes during preparation cancel that cutover.

The default TOML settings are:

```toml
auto_update = false
update_interval_seconds = 900
```

The interval can be 300–86400 seconds. An enabled updater needs outbound HTTPS
to GitHub and package sources, plus uv, free disk space, and the same build
prerequisites as installation. GitHub API limits or an outage postpone updates.
No GitHub account or token is required. For a manually managed/pinned host,
leave this feature disabled.

## What happens during an update

The supervisor checks the exact main commit and its latest matching CI run.
It downloads that commit into a private directory and builds a separate Python
environment with its uv.lock and protocol extras. The current BBS keeps
serving during download, dependency installation, and preflight checks.

Preflight opens a copy of the database with the candidate's doctor command.
It does not connect radios or run the copied identity. Before switching, the
updater rechecks the branch, CI, and configuration. It retains a private database
and config snapshot, stops the old service, and starts the candidate using the
same live data. It never runs two BBS children on that data directory at once.

Startup confirmation must come from that specific new process after runtime
initialization. If the old radios were ready, the new radios must also become
ready. The check allows up to a minute, including five seconds of stable health.
The transition briefly interrupts access; downloads do not.

A preparation failure leaves the current service running. A failed startup
selects the previous application and records the failed commit so it is not
retried on every poll. An interrupted cutover is recovered when the service
manager starts the host again. Radio settings, identities, posts, drafts,
receipts, budgets, and discovery schedules remain in their existing locations.
The updater does not flash firmware, change RF settings, or pair federation peers.

Application rollback does not restore an older database over newer writes.
A code rollback cannot repair an incompatible data migration. Automatic updates
therefore require maintainers to keep the previous application able to read
migrated data, or require an operator-managed upgrade for a breaking migration.
Keep a separate [complete host backup](operations.md#back-up-and-recover).

## Storage and recovery

Managed files live under `DATA_DIR/updates/`. The active/previous installations
and a candidate may coexist; stale release directories are pruned before the
next preparation. `state.json` records the selection and failure status.
`backups/` retains the latest two preparation snapshots of SQLite and TOML.
These contain private signing material. They are not a full Reticulum/profile
backup and may predate writes accepted during preparation.

Check service logs for errors. On Linux user services:

```sh
journalctl --user -u mesh-bbs.service -n 100
```

For a system service use its system journal. Missing native compiler tools on
Intel macOS must be fixed in the service account's environment, as described in
[installation](install.md#intel-macs). Do not delete airtime or post databases to
recover an application update.

To return to a specific manually installed version:

1. Disable automatic updates and stop the service.
2. Install the chosen reviewed ref with `--no-setup` using the normal installer.
3. Run `mesh-bbs --region colorado-mesh updates reset`.
4. Start the service and check its health and protocol access.

Reset refuses while a service or supervisor owns the host. It clears the
cached version selection, not posts or configuration. It also clears the failed
commit exclusion; re-enabling updates allows that commit to be tried again.
Do not reset during an active update or remove managed files used by a running
process.

## Maintainer contract

Keep configuration, data migrations, and the supervisor's startup flags compatible
with the preceding automatically updated version. Test candidate startup,
preflight failure, interrupted exec, parent death, rollback without data loss,
and instance locking. Build from the committed lockfile. CI approval is a release
gate for participating hosts: a push to main can become a deployed application
once its CI completes.
