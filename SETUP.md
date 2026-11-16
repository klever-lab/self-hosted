# Setup

Requires Python 3.12+, `uv`, `kubectl`, and Tailscale on the operator computer.
Use the existing Guaranine kubeconfig. The cluster needs a Ready worker, the
`openebs-hostpath` default StorageClass, MagicDNS/HTTPS, and the cluster-owned
`tag:guaranine-app-gateway` policy. The operator's Tailscale API token remains in
the Guaranine operator directory and is used only for single-use gateway enrollment.

1. Save a **separate** WireGuard profile for qBittorrent outside the checkout with
   mode `0600`. Choose a [P2P-enabled Windscribe location](https://windscribe.com/knowledge-base/articles/does-windscribe-allow-peer-to-peer-p2p-traffic);
   some locations, including Boston - MIT, block torrent traffic.
   Never run the same WireGuard private key in two clients.
2. Find the worker with `kubectl get nodes` and initialize:

   ```bash
   ./self-hosted init --node YOUR_WORKER --wireguard-profile /private/path/qbittorrent.conf
   ```

   Initialization uses `klever` for both administrator usernames and generates
   separate random application passwords. Override the name with `--username`.
   Protected settings live in `~/.local/state/self-hosted` by default; select another location
   with `SELF_HOSTED_OPERATOR_DIR`. Use `GUARANINE_OPERATOR_DIR` for a nondefault
   Guaranine operator directory.
3. Inspect and deploy:

   ```bash
   ./self-hosted render > /tmp/self-hosted.yaml
   ./self-hosted deploy
   ./self-hosted status
   ./self-hosted credentials
   ```

   `render` contains application manifests and the empty library template, without
   credential values. Deployment creates credentials as Secrets, generates the VPN
   endpoint policy, enrolls isolated gateway identities, checks admission, and applies
   the manifests. Images and new storage can take several minutes on first boot.

The default hostnames are `qbittorrent` and `books` under your tailnet's `.ts.net`
domain. Change them in the protected `config.json` **before initial enrollment**.
Both applications are available only to the existing authorized tailnet operator;
Funnel is disabled for both gateways. Calibre-Web also requires authentication,
with anonymous browsing and public registration disabled. Its login is configured before startup.
Both gateways have no grant to administer other tailnet devices.

qBittorrent serves VueTorrent at its existing HTTPS address, using the same login.
The WebUI initializer downloads the pinned release through the VPN, verifies its
SHA-256, and retains it in the configuration volume. Its pin is
`apps/qbittorrent/vuetorrent.json`; update the version, release URL and checksum
together before redeploying. VueTorrent's display settings include transfer graphs
and bits-per-second speed units.

The read-only download-file service accepts connections only from Jellyfin's edge
pod. Jellyfin's own repository owns its category mapping, import schedule, library
configuration and HTTPS playback route; deploy that integration there after
deploying qBittorrent. Directory listings and symlink access are disabled.

To deploy an application independently:

```bash
./self-hosted deploy qbittorrent
./self-hosted deploy calibre-web
```

Deploying both applications also installs the book-import integration. The shared
`components/edge` directory contains the userspace Tailscale gateway resources.

To change qBittorrent's VPN server, replace the profile referenced by
`wireguard_profile` in the protected `config.json`, keeping it outside the checkout
with mode `0600`, then run `./self-hosted deploy qbittorrent`. Deployment refreshes
the cached endpoint address when the profile names a different endpoint and updates
the VPN and its egress policy together. The restart preserves torrents, downloads
and the gateway identity. An HTTPS check alone does not establish P2P connectivity;
check that peers connect and download progress advances.

The administrator username/password are retained in protected
`access.json`. If you change qBittorrent's password through its UI, update that file
and deploy again so the importer can authenticate. Redeployment preserves user
accounts, session data, books, and enrolled gateway identities.

To rename an existing administrator, use the application's user/account settings,
then update the matching username in `access.json` and deploy again. Renaming the
Calibre-Web account preserves its user ID, library access, and Kobo token.

See [Kobo setup](docs/kobo.md) to pair a reader, and [operations](docs/operations.md)
to back up state before substantial changes.

## Kobo Tailscale source

The Clara BW installation uses our fork, [klever-lab/kobo-tailscale](https://github.com/klever-lab/kobo-tailscale),
pinned to commit `e5039bc5504ef0c9d5bff67f12968039c8c2a002`:

```bash
git clone https://github.com/klever-lab/kobo-tailscale.git "$HOME/kobo-tailscale"
git -C "$HOME/kobo-tailscale" checkout --detach e5039bc5504ef0c9d5bff67f12968039c8c2a002
```

Follow the fork's `clara-bw/README.md` for installation. It pins and verifies the
official Tailscale archive. Check the reader's firmware and back up its settings
before copying an installer to it; NickelMenu currently does not support firmware
5.x. Verify installation and a complete sync on the reader; see the recorded
[physical checks](docs/verification.md#clara-bw-installation-and-pairing).

Apply Guaranine's reviewed policy for `tag:guaranine-kobo` before enrollment. Its
only service grant is Books HTTPS. Issue a single-use, preauthorized enrollment
key for that tag, expiring within one hour; keep the operator API token off the
reader. Preserve an existing reader identity when reinstalling.

For a stock reader, stage the verified NickelMenu package as `.kobo/KoboRoot.tgz`,
the pinned fork's Clara BW scripts and official ARM archive under
`kobo-tailscale/clara-bw/`, and the shell scripts from `integrations/kobo/` under
`.adds/self-hosted-kobo/`. That directory also takes a temporary `auth.key` and a
`books-host` file containing the gateway's Tailscale IPv4 address and full HTTPS
hostname on separate lines. Keep these generated files outside Git.

Copy `integrations/kobo/nickelmenu` into `.adds/nm/self-hosted-tailscale`.
It provides **Set up Tailscale**, live **Tailscale status**, **Setup result**,
**Storage status**, and the laptop-access controls described in [Kobo operations](docs/kobo.md#direct-laptop-maintenance).
Eject, unplug, wait for NickelMenu to install, enable Wi-Fi, then run setup. The
bootstrap consumes the key into private system storage before authentication,
deletes it on success and checks private Books HTTPS without bypassing certificate
validation. Reconnect USB to read `status.log` and the selected firewall-backend
messages in `backend-check.log`. Verify native backend initialization and private
HTTPS connectivity; netfilter mode `off` alone does not establish backend support.

Keep downloaded archives, prepared device files, enrollment keys and identity
backups under the protected operator directory, outside both checkouts. When
updating the fork, validate it and update the pinned commit here together.

## Tests

On Linux, install `bubblewrap`, `shellcheck` and an `awk` implementation, then run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --with-requirements requirements.txt python -m unittest discover -s tests -v
shellcheck integrations/kobo/*.sh
```

The Kobo tests enforce NickelMenu 0.6.0's timeout limits and execute the shipped
menu commands and status helper in isolated namespaces with synthetic Wi-Fi,
daemon and HTTPS responses. They cover missing setup results, stalled commands,
live connectivity, storage output and laptop-access controls. They fail if bubblewrap is unavailable;
they do not use a reader, credentials or network access.

The test workflow runs the full suite on pushes and pull requests using a standard
GitHub-hosted runner. These tests do not replace physical checks of the Kobo UI,
firmware behavior, reconnects or book downloads.
