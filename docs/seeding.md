# Seeding operations

Use the queue settings in [operations](operations.md#seeding-and-incoming-peers)
to keep completed torrents eligible to seed. Follow [SETUP.md](../SETUP.md) when
reserving or changing a forwarded port. The port and reservation deadline live in
the private operator configuration. Expiration closes incoming connectivity at
Windscribe; outgoing seeding can continue. There is no automated reservation or
renewal.

## VPN routing and pod security

Gluetun runs as a native sidecar (`vpn`) using kernel WireGuard (`wg0`, MTU `1120`
for Flannel-over-Tailscale headroom) inside the qBittorrent pod's network
namespace. Because Kubernetes PodSecurity Admission evaluates capability grants at
the namespace level, the `qbittorrent` namespace sets
`pod-security.kubernetes.io/enforce: privileged` (with `audit` and `warn` kept at
`restricted`), matching Guaranine's scoped helper namespaces. Within the pod:

- `vpn` (Gluetun) drops all capabilities except `NET_ADMIN` and
  `NET_BIND_SERVICE`, sets `allowPrivilegeEscalation: false`, and uses no
  `privileged` mode, `hostPath`, or host namespaces.
- `qbittorrent`, `configure`, and `webui` run as non-root `UID/GID 1000` with
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
  `seccompProfile: RuntimeDefault`, and `capabilities: {drop: [ALL]}`.

qBittorrent disables proxies (`Proxy\Type=None`, `Session\ProxyPeerConnections=false`)
and binds BitTorrent directly to the tunnel interface (`Session\Interface=wg0`,
`Session\InterfaceName=wg0`) on the configured `Session\Port`. Incoming TCP and UDP
peers arrive on `wg0` with their real source addresses preserved.

Two independent layers enforce fail-closed VPN egress and private WebUI access:

1. **Gluetun firewall:** blocks non-tunnel internet egress, opens only the
   configured forwarded port on `wg0` (`FIREWALL_VPN_INPUT_PORTS`, empty when no
   forward is configured), and allows pod-network ingress only on WebUI port `8080`
   (`FIREWALL_INPUT_PORTS=8080`).
2. **Kubernetes `NetworkPolicy`:** `wireguard-egress` restricts pod egress to the
   resolved WireGuard server UDP endpoint, while `qbittorrent-web` and
   `jellyfin-importer-api` restrict TCP `8080` ingress to `edge`, `book-importer`,
   and Jellyfin's `torrent-importer`.

Windscribe binds ephemeral port forwards during a fresh WireGuard handshake; an
endpoint node holding an existing pre-reservation peer session will ignore the new
forward until that session expires. When `qbittorrent_forwarded_port` is added,
changed, or removed, `./self-hosted deploy qbittorrent` refreshes the resolved
endpoint IP and prefers a different node from the location's DNS pool so the new
forward activates immediately.

## Verify a reservation

After deployment, verify:

1. Authenticated private WebUI access (`8080` via `edge` and `book-importer`),
   unchanged torrent hashes and volume bindings, and preserved Tailscale gateway
   identity.
2. qBittorrent's bound interface (`wg0`) and peer port (`10837`), plus working
   outgoing peer connections, DHT nodes, and trackers.
3. External incoming TCP (BitTorrent handshake) and incoming UDP (DHT ping/reply)
   to Windscribe's public VPN IP on the forwarded port from outside the VPN, with
   port `8080` unreachable on that public VPN IP.
4. Blocked internet egress when bypassing `wg0` (`eth0` bind) or when the VPN
   tunnel is stopped, followed by automatic listener recovery when the tunnel
   resumes.

Record upload counters and elapsed time before and after the trial in protected
operator storage. Compare the same torrents and queue settings over substantial,
comparable periods; successful connectivity alone does not prove a better ratio.
If the trial is effective, the agreed next step is to buy a P2P-capable Windscribe
static IP and use a permanent port forward. Verify both P2P and forwarding support
for the chosen location and connection method before purchasing.

## Live trial verification (2026-11-16 UTC)

Verified on the live `qbittorrent` deployment with ephemeral Matching Port `10837`
(approximate expiry `2026-11-23T13:48:00Z`):

- Replaced the userspace SOCKS5/Dante setup with digest-pinned Gluetun `v3.41.3`
  (`kernelspace` WireGuard `wg0`, MTU `1120`). Confirmed `qbittorrent` runs as
  `UID/GID 1000` with `CapEff=0000000000000000` and `readOnlyRootFilesystem: true`.
- Confirmed all five existing torrent hashes, both PVC bindings (`config` and
  `downloads`), private Tailscale HTTPS login, `book-importer` CronJob execution,
  and the enrolled gateway identity were preserved.
- From outside the VPN, verified a full 68-byte BitTorrent TCP handshake and a
  valid BEP-5 KRPC UDP DHT `ping` response on Windscribe's public exit IP port
  `10837`, while TCP `8080` on that exit IP timed out. Observed active TCP (`BT`)
  and UDP (`μTP`) swarm peers with real peer IPs preserved and immediate upload
  throughput.
- Verified that sockets bound directly to `eth0` fail closed (`ETIMEDOUT` / `EPERM`)
  and that stopping the VPN tunnel blocks HTTPS, raw TCP, and raw UDP egress while
  keeping local WebUI port `8080` responsive; restarting the tunnel automatically
  restored `wg0` and external TCP/UDP `10837` reachability.
