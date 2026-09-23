# Run a community BBS without hand-editing configuration

Install as your normal Linux or macOS user:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Colorado-Mesh/mesh-bbs/main/install.sh | sh
```

Choose **Colorado Mesh** or **another community**. Existing communities must use
the same region ID on every host; separate regions never replicate into each
other. The Colorado preset imports the organization's blog and published
newsletter text. Other communities supply their own RSS/Atom URL.

The setup wizard offers connection setup. You can return to it any time:

```sh
mesh-bbs --region YOUR-REGION configure
```

Replace `YOUR-REGION` in every example with the chosen region ID. For a custom
config location, use `--config /path/to/config.toml` instead of `--region`.
The wizard backs up the original config before saving, preserves unrelated
settings, and refuses to overwrite a file changed by another process. No radio
opens and no service starts merely by saving configuration.

## Connect readers

Choose MeshCore or Meshtastic in `configure`, then USB serial or TCP. Use standard
Meshtastic client firmware or MeshCore companion firmware. Configure the radio's
region, modem preset, and channel keys in its normal radio app first. The BBS
wizard does not flash firmware or change RF settings.

Choose the radio's actual modem profile for airtime accounting. The MeshCore
SF7/62.5 kHz/CR5 option reserves one second per packet. Meshtastic LongFast at
250 kHz reserves three seconds, and MediumFast at 250 kHz reserves one second;
these include a margin for a maximum packet. For other profiles keep the
conservative estimate or supply a measured upper bound. The wizard displays
how many replies fit the allowance, including retry reservations. See the
[official Meshtastic preset table](https://github.com/meshtastic/meshtastic/blob/master/docs/about/overview/radio-settings.mdx)
and [airtime accounting](operations.md#size-the-reply-allowance-for-the-configured-modem).

For optional public notices and channel `help`, designate exactly one host per
local radio mesh. Select its existing channel slot and name. MeshCore users join
`#bbs`; Meshtastic users need your mesh's matching secondary-channel URL/QR and
modem preset. Keep notices off on other hosts so a replicated post is not
advertised repeatedly. DMs still work on every host.

For Reticulum, choose an existing config directory, or enter a TCP gateway that
your community uses. The wizard can create a dedicated client profile without
altering other apps' Reticulum files. The service supplies LXMF messaging,
NomadNet reading, and BBS replication over that connection. Reaching a Reticulum
gateway does **not** automatically trust every BBS on it.

Create your web contributor and start the service:

```sh
mesh-bbs --region YOUR-REGION init
mesh-bbs --region YOUR-REGION web-access create YOUR-NAME
mesh-bbs --region YOUR-REGION serve
```

Save the printed access key privately. Open the printed local web address and
sign in to create boards and posts. News is reserved for automatic imports,
including for editor accounts. Remote web access needs an HTTPS reverse proxy;
set its public URL through `configure`. Background service and Tailscale examples
are in [operations](operations.md). Restart an already running BBS after changing
configuration.

Every messaging protocol uses the same interaction: DM **help**, choose a number,
and use **3** to write. Choose a board or create one, send a title and text,
then **done** and **publish**. See the [reader guide](commands.md).

## Pair two or more BBS hosts

Each host has its own database and identity. Do not clone a live database to
make another host. Both operators enable Reticulum, select the **same region**,
and export their own signed public card:

```sh
mesh-bbs --region YOUR-REGION peer export my-host.json
```

Send that file to the other operator and receive theirs. The card contains only
public host identity information, not a private key, access key, database, or
radio backup. Independently compare the full origin fingerprint with the
operator you intend to trust; a valid signature alone does not establish that
you know its owner.

On **each** host, import the other host's card:

```sh
mesh-bbs --region YOUR-REGION peer add other-host.json
```

Check the name, region and origin shown, then answer `yes`. The default grant
allows current and future community boards, so a board someone creates over
MeshCore, Meshtastic, LXMF or the web also appears on paired hosts. No moderation
privilege is granted. For a deliberately restricted replica, add
`--boards general,news`; new boards will not automatically be included.

Restart both services. They pull missing signed events every minute, catch up
after outages, and deduplicate posts and feed imports by their identities.
Announcements, drafts, web credentials and hardware settings remain local.
Pair every participating signing origin with each replica that should accept
its posts; trusting a relay does not automatically trust unknown authors' hosts.
Upgrade all peers before using community-board events and wildcard grants.

Check pairing and the last replication result:

```sh
mesh-bbs --region YOUR-REGION peer list
```

`last_sync: null` means no exchange has been recorded. `completed_sweep: true`
means the latest bounded scan finished; `status: error` needs a look at service
logs and Reticulum reachability. A host with zero configured peers imports feeds
but does not replicate with another BBS host. An exported card is not a pairing
until both operators have added it and restarted.

For scripted setup, `peer add ... --trust-origin FULL_ORIGIN` approves only the
matching fingerprint. Keep this explicit approval outside the received file.
For three hosts, exchange cards among all three. No Colorado-owned central
server is required for another community to run the same arrangement.
