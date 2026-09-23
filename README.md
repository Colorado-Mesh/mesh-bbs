# Mesh BBS

Mesh BBS is a community bulletin board you can read and write through a browser,
MeshCore, Meshtastic, or Reticulum. It runs on a computer or Pi connected to your
networks. People can share newsletters, create boards, and hold conversations
without everyone using the same radio protocol or app.

Each host saves its own copy. Operators can pair trusted hosts over Reticulum
so posts catch up after an outage. A region name alone does not connect hosts;
[community setup](docs/community-setup.md) explains pairing.

This is pilot software. Tests cover storage, retries, real Reticulum links,
browsers, and radio SDKs connected to protocol emulators. Community-scale RF
reliability and airtime still need field testing. See [project status](docs/plan.md).

## Try it as a reader

Ask your operator for the BBS node or address, then:

| Connection | Start here |
| --- | --- |
| MeshCore | Open a private chat with the BBS companion and send `help`. |
| Meshtastic | Send the BBS node a direct message containing `help`. |
| Reticulum messaging | Send `help` to the BBS's LXMF address. |
| NomadNet | Open the BBS node's `/page/index.mu` page. |
| Web | Open the host's web address. Reading needs no account. |

The DM menu is the same on all three messaging protocols: **1 News, 2 Boards,
3 Write/resume**. Reply with a number. To write, choose a board or create one,
send a title and text, then send `done` to review and `publish` to save it.
`menu` returns to the start; **3** resumes your draft. Long posts arrive one page
at a time when you request them.

**News is reserved for automatic imports.** Human posts and replies belong on
community boards, including when the author has an editor key.

Optional channel notices tell you what changed and how to read it:

```text
New post in running:
"mesh runners!"
To read it, send me a private message: read #7
```

Send `read #7` to the node that posted the notice, then `next` when prompted.
The number stays attached to that post on that host. For the full walkthrough,
see [reading and posting](docs/commands.md).

## Run a host

On Linux or macOS, install as your normal user. Intel Mac users need the
[source-build prerequisites](docs/install.md#intel-macs) first.

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Colorado-Mesh/mesh-bbs/main/install.sh | sh
```

The setup wizard offers **Colorado Mesh** or **another community**, then helps
configure radios, Reticulum, feeds, and web access. You can rerun connection
setup with `mesh-bbs --region YOUR-REGION configure`.

For Colorado Mesh, the next steps are:

```sh
mesh-bbs --region colorado-mesh init
mesh-bbs --region colorado-mesh web-access create alice
mesh-bbs --region colorado-mesh serve
```

Replace `alice` with your name and save the printed key privately. Open
`http://127.0.0.1:8080`, or the address printed by your configuration. Sign in
at `/connect` to create boards and posts. Another community uses its chosen
region in those commands. Setup prints commands for custom paths and ports.

The Colorado preset imports full blog articles and the published newsletters'
Markdown text every 15 minutes. Other communities can supply their own RSS or
Atom feeds. Paired hosts share posts and new boards; drafts, keys, radio settings,
and short reading numbers stay local.

Use standard MeshCore companion or Meshtastic firmware. The BBS needs neither
custom firmware, MeshCore Room Servers, nor Mesh Client. An operator-managed
[packet terminal](docs/transports.md#cleartext-packet-terminal) is also available.

## Automatic updates

To follow new main commits after CI passes, run `mesh-bbs --region colorado-mesh updates enable` and restart the host. Setup also offers **6 Automatic updates**.
Updates prepare a separate installation while the BBS runs, then briefly restart
it. [The updater guide](docs/updates.md) covers status, disabling updates,
snapshots, and recovery. Existing hosts stay opted out.

## Documentation

| I want to… | Guide |
| --- | --- |
| Read, post, reply, or recover a draft | [Commands](docs/commands.md) |
| Install or update the application | [Installation](docs/install.md) |
| Start another community or pair hosts | [Community setup](docs/community-setup.md) |
| Run a service, use Tailscale, back up, or diagnose a connection | [Operations](docs/operations.md) |
| Enable automatic updates or recover a failed update | [Updates](docs/updates.md) |
| Edit configuration by hand | [Annotated configuration](examples/config.toml) |
| Understand identity, deduplication, and replication | [Architecture](docs/architecture.md) |
| Add a transport or packet gateway | [Transport integration](docs/transports.md) |
| Build and test a change | [Development](docs/development.md) |

[Project status](docs/plan.md), [radio recovery](docs/reliability-plan.md), and
[web design](docs/web-gui-plan.md) record implemented behavior and remaining work.
[AGENTS.md](AGENTS.md) contains contributor instructions.

## License

[GPL-3.0-or-later](LICENSE).
