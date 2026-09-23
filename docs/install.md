# Install Mesh BBS

Use a normal Linux or macOS account. The installer prepares an isolated Python
3.12 environment, installs the protocol libraries, and opens a setup wizard.
Intel Macs need [the prerequisites below](#intel-macs) first.

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Colorado-Mesh/mesh-bbs/main/install.sh | sh
```

This installs development `main`, not a stable release. To use a reviewed commit
or tag, substitute it for **both** occurrences of `REVIEWED_REF`:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Colorado-Mesh/mesh-bbs/REVIEWED_REF/install.sh | sh -s -- --ref REVIEWED_REF
```

You can download and inspect the script before running it. `MESH_BBS_REF` selects
the package ref when `--ref` is absent.

## Complete setup

Choose Colorado Mesh or enter another community's name and region ID. If joining
an existing community, get its region ID from an operator. The region becomes
fixed when you initialize the database.

The wizard offers connection setup for radios, Reticulum, feeds, and the web
listener. It saves configuration; it does not flash a radio, change RF settings,
start the service, or create a contributor key. You can return later:

```sh
mesh-bbs --region colorado-mesh configure
```

Initialize, create your web key, and start the host:

```sh
mesh-bbs --region colorado-mesh init
mesh-bbs --region colorado-mesh web-access create alice
mesh-bbs --region colorado-mesh serve
```

Use your selected region and contributor name. Save the key privately; it is
printed once. Open `http://127.0.0.1:8080/connect` to sign in. Setup prints the
actual commands if you chose another config path or port. Reading at `/` needs
no key. See [community setup](community-setup.md) to connect users and pair hosts.

Colorado Mesh starts with its blog feed and published newsletter text sources.
Another community supplies its own feed or runs without one. Choosing a region
neither pairs federation peers nor registers with a central server.

## Files and dependencies

| Item | Default location for Colorado Mesh |
| --- | --- |
| Executable | `~/.local/bin/mesh-bbs` |
| Configuration | `~/.config/mesh-bbs/colorado-mesh/config.toml` |
| Database and private host state | `~/.local/share/mesh-bbs/colorado-mesh/` |

Absolute `XDG_CONFIG_HOME` and `XDG_DATA_HOME` override the directory roots on
Linux and macOS. `UV_TOOL_BIN_DIR` overrides the executable directory. Quote
paths containing spaces. Use `~/.local/bin/mesh-bbs` directly if it is not on PATH.

The installer reuses uv or obtains it from its official installer with shell
profile changes disabled. uv supplies a managed Python; system Python stays in
place. Reticulum, MeshCore, and Meshtastic extras are included by default.
`--extras reticulum`, a comma-separated list, or `--extras none` restricts them;
`MESH_BBS_EXTRAS` supplies the same setting.

Initial setup refuses to overwrite an existing configuration. Later `configure`
runs make a private backup and reject concurrent edits. Use `--config PATH`
instead of `--region NAME` for a custom file. Simultaneous communities need
separate configuration, data directories, identities, and web ports.

For an unattended install, or to update without reopening setup:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Colorado-Mesh/mesh-bbs/main/install.sh | sh -s -- --no-setup
```

Run `~/.local/bin/mesh-bbs setup` when ready. Without an interactive terminal,
the installer prints this step rather than prompting.

## Intel Macs

The cryptography dependency used here builds from source on Intel macOS. With
Homebrew available, install its prerequisites:

```sh
xcode-select --install
brew install rust openssl@3
```

Wait for Command Line Tools to finish. The BBS installer checks for a C compiler,
Rust 1.83+, and OpenSSL headers before replacing its tool environment. It uses
Homebrew's `openssl@3`, or your explicit `OPENSSL_DIR`. It does not install system
packages itself. Apple Silicon users should use a native arm64 terminal.

The Intel source-build path and all-extras installer run in CI on macOS 15;
other Intel macOS versions have not been established by that check. Upstream
provides the [support change](https://cryptography.io/en/latest/changelog/#v49-0-0)
and [build instructions](https://cryptography.io/en/latest/installation/#building-cryptography-on-macos).

## Update or uninstall

Back up the host, stop its service, rerun the installer with the chosen ref and
`--no-setup`, then start it and check `/readyz`. Application updates retain config
and data. Follow [backup and recovery](operations.md#back-up-and-recover) before
changing versions; restoring an older executable may also require restoring its
matching data snapshot.

`uv tool uninstall mesh-bbs` removes the application environment. It leaves
configuration, posts, and identities in place. Delete those only when you intend
to retire the host. Never run a restored identity alongside its original.

For uv itself, see [tool environments](https://docs.astral.sh/uv/guides/tools/)
and [installer options](https://docs.astral.sh/uv/reference/installer/).
