# Install and choose a community

Mesh BBS runs as your regular user on Linux or macOS. The installer uses an
isolated Python 3.12 environment and starts the community setup wizard:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Colorado-Mesh/mesh-bbs/main/install.sh | sh
```

Intel Macs need the [prerequisites below](#intel-macs) before this command.

The wizard offers **Colorado Mesh** or **another mesh or region**. For another
community, enter its name and a stable region ID such as `front-range`. Ask the
other operators which ID to use before joining an existing community. Selecting
Colorado Mesh sets local defaults and configures the
[Colorado Mesh blog feed](https://blog.coloradomesh.org/feed.xml). That source
currently supplies announcements and newsletter introductions; linked newsletter
PDFs are not imported as full article text. Fetching begins when the service
runs, and the feed entry can be edited or removed. Other communities start
without a feed until the operator supplies one. The region choice does not
register a host or connect it to an official public federation server; peer
addresses must be provided by their operators.

This is a development install from `main`, not a published stable release. To
install a reviewed commit or tag, use its ref for both the script and package:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Colorado-Mesh/mesh-bbs/REVIEWED_REF/install.sh | sh -s -- --ref REVIEWED_REF
```

Replace `REVIEWED_REF` in both places with the same commit ID or tag. You can also
download and inspect `install.sh` before running `sh install.sh --ref ...`.
`MESH_BBS_REF` selects the package source when no `--ref` is given.

## What installation changes

- Reuses `uv` if available. Otherwise downloads its official installer from
  `https://astral.sh/uv/install.sh`, with shell profile modification disabled.
- Installs Mesh BBS into an isolated uv tool environment with a uv-managed
  Python 3.12. uv obtains that interpreter if needed; system Python is not
  replaced or used. Intel Macs also need the native compiler described below.
- Includes Reticulum, MeshCore, and Meshtastic adapter dependencies so enabling a
  connection later does not need another package install. To limit dependencies,
  use `--extras reticulum` (or a comma-separated list), or `--extras none` for
  local-only use. `MESH_BBS_EXTRAS` supplies the same setting.
- Places the executable in `~/.local/bin`, or your `UV_TOOL_BIN_DIR` if set.
- Creates a new configuration only when you complete setup. Existing files are
  never overwritten by the wizard.

Do not use `sudo`. Installation does not enable radio interfaces, start a
background service, create contributor keys, or edit your shell startup files.
Run `~/.local/bin/mesh-bbs`
directly if that directory is not on your PATH. If `UV_TOOL_BIN_DIR` contains
spaces, quote the executable path when running it.

Without an interactive terminal, the installer prints the setup command. Use
`--no-setup` to deliberately install without prompting:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Colorado-Mesh/mesh-bbs/main/install.sh | sh -s -- --no-setup
~/.local/bin/mesh-bbs setup
```

## Intel Macs

The patched cryptography dependency no longer supplies Intel Mac wheels.
Intel Macs build it from source, so the first install takes longer. With
[Homebrew](https://brew.sh/) installed, prepare these tools first:

```sh
xcode-select --install
brew install rust openssl@3
```

Wait for Command Line Tools installation to finish, then run the installer above.
It checks the C compiler, Rust 1.83 or newer, and OpenSSL headers before changing
the tool installation. It uses Homebrew's `openssl@3` directory, or preserves
your explicit `OPENSSL_DIR` for another OpenSSL installation. It does not install
system packages itself. Apple Silicon users should use a native arm64 terminal
and Python, rather than Rosetta, to use prebuilt wheels.

Upstream [removed Intel Mac support](https://cryptography.io/en/latest/changelog/#v49-0-0).
Mesh BBS CI checks the source-build path on an Intel Mac runner, including the
installer with every protocol extra; broader Intel macOS versions remain
unverified. See the upstream [build prerequisites](https://cryptography.io/en/latest/installation/#building-cryptography-on-macos)
for alternative toolchains.

## Community configuration

Default locations for Colorado Mesh are:

```text
~/.config/mesh-bbs/colorado-mesh/config.toml
~/.local/share/mesh-bbs/colorado-mesh/
```

Other communities get their own region subdirectory. Absolute
`XDG_CONFIG_HOME` and `XDG_DATA_HOME` values are honored on Linux and macOS.
Each command can select a configuration with `--config PATH`; running multiple
communities requires separate data directories and different web ports.

The region is fixed once the database is initialized. Create a new instance to
run another community instead of changing an existing database's region.
Back up configuration, the entire data directory, and host identity together.
Do not copy an identity to two simultaneously running hosts.

The wizard prints these next steps using your actual configuration path:

```sh
~/.local/bin/mesh-bbs --config ~/.config/mesh-bbs/colorado-mesh/config.toml init
~/.local/bin/mesh-bbs --config ~/.config/mesh-bbs/colorado-mesh/config.toml web-access create alice --editor
~/.local/bin/mesh-bbs --config ~/.config/mesh-bbs/colorado-mesh/config.toml serve
```

Replace `alice` with your contributor name. The second command prints an access
key once; save it privately. Open `http://127.0.0.1:8080/connect` and enter that
key to write posts or newsletter issues. Ordinary contributors get a key with
the same command without `--editor`. Public board reading at
`http://127.0.0.1:8080` needs no key. This is a standalone Mesh BBS interface and
requires no Mesh Client installation.

For another region or a config path containing spaces, use the exact commands
printed by setup. The browser keeps the key in tab session storage and unfinished
drafts in local storage. A web contributor appears as `web:NAME`; that identity
is separate from their radio identity.

For remote posting, configure an HTTPS reverse proxy and set `public_url` to
the external origin, for example `https://bbs.example.org`. Plain HTTP writes
are limited to loopback access. `public_url` also controls links in the feed;
it does not change the listener or configure DNS or TLS. See
[web contributor operations](operations.md#web-contributors-and-public-access)
for issuing and revoking keys and configuring public access.
See [the annotated configuration](../examples/config.toml) for feeds, editors,
trusted peers, and explicit transport settings. A peer with no `allowed_boards`
has no board access. Giving a peer `can_moderate = true` is a separate decision
from allowing it to share posts.

Newsletter imports use the configured RSS/Atom URL. Use the same stable
`source_id` on every host importing that source. Colorado Mesh uses
`colorado-mesh-blog` for its blog feed. A region choice does not grant editorial
permission or connect a radio.

Reticulum requires an explicit `config_dir` before it can be enabled. Configure
that directory's interfaces deliberately; use a dedicated temporary profile for
local tests. MeshCore uses a companion radio connection, and Meshtastic uses
its own serial or TCP connection. Both are disabled by default.

## Update or remove

Rerunning the installer reinstalls the requested application ref. It leaves
configuration and data in place; use `--no-setup` when updating. Back up before
changing application versions and follow any migration notes for that version.

Remove the application with `uv tool uninstall mesh-bbs`. Configuration, local
posts, and host identity remain until you remove their directories yourself.

uv behavior: [tool environments](https://docs.astral.sh/uv/guides/tools/) and
[installer options](https://docs.astral.sh/uv/reference/installer/).
