# Setup

Requires Python 3.12+, `uv`, `kubectl`, and Tailscale on the operator computer.
Use the existing Guaranine kubeconfig. The cluster needs a Ready worker, the
`openebs-hostpath` default StorageClass, MagicDNS/HTTPS, and the cluster-owned
`tag:guaranine-app-gateway` policy. The operator's Tailscale API token remains in
the Guaranine operator directory and is used only for single-use gateway enrollment.

1. Save a **separate** WireGuard profile for qBittorrent outside the checkout with
   mode `0600`. Never run the same WireGuard private key in two clients.
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

To deploy an application independently:

```bash
./self-hosted deploy qbittorrent
./self-hosted deploy calibre-web
```

Deploying both applications also installs the book-import integration. The shared
`components/edge` directory contains the userspace Tailscale gateway resources.

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
pinned to commit `68a13bf70ddf7d1f811f77a02d2cca6578664de3`:

```bash
git clone https://github.com/klever-lab/kobo-tailscale.git "$HOME/kobo-tailscale"
git -C "$HOME/kobo-tailscale" checkout --detach 68a13bf70ddf7d1f811f77a02d2cca6578664de3
```

Follow the fork's `clara-bw/README.md` for installation. It pins and verifies the
official Tailscale archive. Check the reader's firmware and back up its settings
before copying an installer to it; NickelMenu currently does not support firmware
5.x. Physical installation and sync still need verification on the reader.

Apply Guaranine's reviewed policy for `tag:guaranine-kobo` before enrollment. Its
only service grant is Books HTTPS. Issue a single-use, preauthorized enrollment
key for that tag, expiring within one hour; keep the operator API token off the
reader. Preserve an existing reader identity when reinstalling.

For a stock reader, stage the verified NickelMenu package as `.kobo/KoboRoot.tgz`,
the pinned fork's Clara BW scripts and official ARM archive under
`kobo-tailscale/clara-bw/`, and `integrations/kobo/bootstrap.sh` under
`.adds/self-hosted-kobo/`. That directory also takes a temporary `auth.key` and a
`books-host` file containing the gateway's Tailscale IPv4 address and full HTTPS
hostname on separate lines. Keep these generated files outside Git.

Add NickelMenu entries for **Set up Tailscale** (spawn the bootstrap script with
`/bin/sh`) and **Tailscale status** (display `.adds/self-hosted-kobo/status.txt`).
Eject, unplug, wait for NickelMenu to install, enable Wi-Fi, then run setup. The
bootstrap consumes the key into private system storage before authentication,
deletes it on success and checks private Books HTTPS without bypassing certificate
validation. Reconnect USB to read `status.txt` and the selected firewall-backend
messages in `backend-check.txt`. Keep the inherited firewall bundle in the fork
until native backend initialization and authenticated routing have been verified.

Keep downloaded archives, prepared device files, enrollment keys and identity
backups under the protected operator directory, outside both checkouts. When
updating the fork, validate it and update the pinned commit here together.
