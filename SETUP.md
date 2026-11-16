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

   Initialization generates separate random application passwords. Protected
   settings live in `~/.local/state/self-hosted` by default; select another location
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
qBittorrent is available to the existing authorized tailnet operator; Funnel is
disabled for its gateway. Calibre-Web is publicly reachable over HTTPS, with anonymous
browsing and public registration disabled. Its login is configured before startup.
Both gateways have no grant to administer other tailnet devices.

To deploy an application independently:

```bash
./self-hosted deploy qbittorrent
./self-hosted deploy calibre-web
```

Deploying both applications also installs the book-import integration. The shared
`components/edge` directory contains the userspace Tailscale gateway resources.

The original generated admin username/password are retained in protected
`access.json`. If you change qBittorrent's password through its UI, update that file
and deploy again so the importer can authenticate. Redeployment preserves user
accounts, session data, books, and enrolled gateway identities.

See [Kobo setup](docs/kobo.md) to pair a reader, and [operations](docs/operations.md)
to back up state before substantial changes.
