# Start a community and share it between hosts

One host can serve web readers, MeshCore, Meshtastic, and Reticulum at the same
time. Additional hosts keep their own copies and exchange signed posts over
Reticulum. This works for Colorado Mesh and for independent communities; no
Colorado-owned server is required.

## Choose the community

[Install Mesh BBS](install.md) and complete `mesh-bbs setup`. Choose Colorado Mesh
or enter your community's name and a stable region ID such as `front-range`.
Hosts that share boards must use the same region. Different regions do not sync.

The examples below use `YOUR-REGION`; replace it with that ID. For custom paths,
use `--config /path/to/config.toml` instead of `--region YOUR-REGION`.

```sh
mesh-bbs --region YOUR-REGION configure
```

The connection wizard preserves unrelated settings, backs up the previous file,
and refuses to overwrite a concurrent change. Saving does not start a service.
Restart a running host after changing configuration.

## Connect people

For MeshCore or Meshtastic, choose serial or TCP in the wizard. Use stock MeshCore
companion firmware or stock Meshtastic firmware. Set the region, modem profile,
and channel keys with the radio's normal app first. The wizard does not change
RF settings. Close any app holding the same serial device.

Choose an airtime estimate for the radio's actual modem profile. The wizard
explains how many replies fit the allowance. An estimate for a fast profile is
not suitable for a slower preset. See [airtime accounting](operations.md#size-the-reply-allowance-for-the-configured-modem).

For Reticulum, select an existing configuration directory or your community's
TCP gateway. The wizard can create a dedicated client profile without editing
other apps' profiles. This connection carries LXMF messages, NomadNet pages,
and BBS replication. Being on the same Reticulum network does not establish
trust between BBS hosts.

For the web interface, choose the local port. Remote contributors need an HTTPS address and
matching `public_url`; [operations](operations.md#web-contributors-and-public-access)
covers reverse proxies and Tailscale. Keep a private hostname out of the host's
display name if you do not want it in Reticulum announcements.

Colorado Mesh includes its full blog feed and published newsletter
text. Other communities enter their RSS/Atom source. News accepts imports only;
people create posts and replies on community boards.

Start the host:

```sh
mesh-bbs --region YOUR-REGION init
mesh-bbs --region YOUR-REGION web-access create YOUR-NAME
mesh-bbs --region YOUR-REGION serve
```

Save the printed key privately. In the web interface, sign in at `/connect` to
create boards and posts. Radio and LXMF users send `help` to the BBS for the same
numbered menu. Share the correct address for each protocol; an LXMF messaging
address, NomadNet node address, and federation identity are different things.

The connection wizard also offers **6 Automatic updates**. It is off by default.
Enable it to follow new application commits once CI passes; all communities
use the same [updater and recovery commands](updates.md). Posts and identities
stay in their existing data directory.

## Keep discovery channels quiet

Choose **one announcing host per protocol in each overlapping radio mesh**.
All other hosts can still answer DMs. Agree on the designated host's signing
`origin`, printed by `init`, and use it as `announcement_owner` on participants.

For MeshCore, create `#bbs` in the companion and reader apps. For Meshtastic,
create an agreed secondary channel and share its URL/QR; a channel name alone
does not set its key. Select that companion's actual channel slot in the wizard.
Indexes can differ between radios.

The announcer sends brief new-thread and new-board notices plus rate-limited
responses to channel `help`. Full posts stay in private chats. There is no
automatic announcer election during outages: that could make both sides of a
partition transmit duplicate notices. See [channel configuration](operations.md#channel-announcements)
before handing the role to another host.

## Pair two hosts

Initialize each host separately. Do not clone a database to add capacity: it
contains the private signing identity. Enable Reticulum on both hosts, then
export a public card on each:

```sh
mesh-bbs --region YOUR-REGION peer export my-host.json
```

Exchange the files. Each card contains public identity information, not private
keys or radio backups. Compare the **full origin fingerprint** through a trusted
conversation with the other operator. A valid signature proves possession of
a key, not that the key belongs to the person you intended to trust.

On each host, import the other operator's card:

```sh
mesh-bbs --region YOUR-REGION peer add other-host.json
```

Check the displayed name, region, and origin, then answer `yes`. By default the
grant includes current and future boards (`["*"]`). Creating a board through web,
MeshCore, Meshtastic, or LXMF then makes it available to those peers too. No
moderation authority is granted. To restrict a replica, use
`--boards general,news`; that grant will not expand when a new board appears.

For scripts, `--trust-origin FULL_ORIGIN` approves only the exact fingerprint.
Obtain that approval independently of the received file.

Restart both services. Each pulls missing events every minute and catches up
after outages. Posts, replies, board creation, feed revisions, and removals use
permanent identities. Drafts, keys, radio settings, short `#N` reading numbers,
and discovery schedules remain local.

## Check that sync is working

```sh
mesh-bbs --region YOUR-REGION peer list
```

`last_sync: null` means no exchange has been recorded. `completed_sweep: true`
means a bounded inventory scan finished. `status: error` needs a look at service
logs, peer configuration, and Reticulum reachability. Verify that an agreed test
post arrives with the same ID and parent references on both hosts.

A host with zero peers may import feeds successfully but is not replicating with
another BBS. Merely exporting a card is not pairing. For three hosts, exchange
cards among all three: a trusted relay does not confer trust on unknown signing
origins. Every replica must trust each origin whose posts it accepts, even when
those posts arrive through an intermediary.

Keep peers on compatible versions before using board events and wildcard grants.
During an outage, “Saved locally” means that host has committed the post; it is
not a receipt from the other hosts.
