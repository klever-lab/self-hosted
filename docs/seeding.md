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
namespace. Built-in Pod Security Admission cannot express a per-container
exception, so the namespace retains `enforce: privileged` with restricted audit
and warnings. The enforcing `self-hosted-qbittorrent-security`
[admission policy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/)
and binding close that namespace-wide gap for every Pod creation/update and
debug-container update. Controller-created Pods receive the same checks.
The binding denies violations and the policy fails closed on evaluation errors.
Within the pod:

- `vpn` (Gluetun) drops all capabilities except `NET_ADMIN` and
  `NET_BIND_SERVICE`, sets `allowPrivilegeEscalation: false`, and uses no
  `privileged` mode, `hostPath`, or host namespaces.
- `qbittorrent`, `configure`, and `webui` run as non-root `UID/GID 1000` with
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
  `seccompProfile: RuntimeDefault`, and `capabilities: {drop: [ALL]}`.

The exception requires the native init sidecar named `vpn`, its exact Gluetun
image digest, startup command and health probes, an allowlist of environment
variables, kernelspace `wg0`, and its sole read-only VPN Secret mount, in a Pod
labeled `app: qbittorrent`. Extra command hooks, imported/duplicate environment
variables and a public control server are rejected. It permits root and only the
two listed capabilities. Every container still forbids privileged mode and
privilege escalation. Other containers require non-root user/group IDs,
read-only root filesystems, and no added capabilities. The policy also blocks
host namespaces, host ports/mounts, shared process namespaces, alternate runtimes,
custom sysctls, unconfined profiles, and probe/lifecycle requests targeting hosts.
All container images require digest pins.

`apps/qbittorrent/security.yaml` and its binding must remain installed while the
namespace's built-in enforcement is privileged. Deployment installs the policy
before changing namespace labels, waits for successful type checking, verifies
an actual denial, and dry-runs Pod templates before applying controllers. A new
namespace starts restricted until that denial proves the custom guard is active.
When updating Gluetun's digest, command, or mount, update its admission exception
together and run `scripts/check_admission.py` as described in [setup](../SETUP.md#tests).
Application service accounts have no permissions to modify these cluster policies.

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

During the trial, the existing VPN endpoint did not apply the new reservation;
switching to another resolved endpoint activated incoming connectivity. When
`qbittorrent_forwarded_port` is added, changed, or removed, deployment refreshes
DNS and prefers a different endpoint IP when the location has one. This is an
observed recovery procedure, not a guarantee that each provider endpoint will
refresh its forwarding state. Verify external TCP and UDP after every reservation.

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

## Admission hardening verification (2026-11-16 UTC)

- Installed the policy and Deny binding scoped to qBittorrent requests, with successful API
  type checking. All 51 server dry runs passed: seven permitted requests
  (six workload templates and a restricted debug container) were accepted, and
  44 unsafe requests were rejected by this policy. Cases cover host access,
  root/capability overrides, fake VPN containers, command/probe hooks, environment
  injection, security profiles, and ephemeral-container updates. No probe Pods or
  debug containers were created.
- Deployment requires the policy and enforcing binding, rejects type errors and
  stale type-check results, and proves a denial before applying the namespace's
  VPN exception. A new namespace remains restricted during this check. Dry runs
  use strict API-server validation without downloading kubectl's OpenAPI schema.
- Both application service accounts (`default` and `edge`) were denied Pod
  creation, namespace modification, admission-policy/binding modification, and
  access to the WireGuard Secret. Edge retains its separate gateway-state access.
- The live deployment stayed healthy without replacing application Pods.
  All five torrent hashes, both applications' PVC bindings and enrolled gateway
  identities were unchanged. The importer completed after enforcement began.
  Private qBittorrent and Books logins worked. Both library databases passed
  SQLite checks; the library retained 259 books and 510 formats.
- Removed active upload/total-torrent queue caps while retaining the limit of
  three downloads. All five torrents became eligible to seed. Their upload
  counters advanced by more than 1 GB during this audit, with TCP and uTP peers.
  This observation does not establish a controlled ratio improvement.
- Repeated the VPN failure check with successful TCP, UDP DNS, and HTTPS
  baselines. Direct `eth0` and IPv6 probes failed. Stopping the tunnel blocked
  all three egress checks while local WebUI access returned HTTP 200; restarting
  restored them. An early incoming probe timed out during recovery; subsequent
  full external BitTorrent handshakes and UDP DHT replies passed automatically.
  Public VPN port 8080 remained unreachable.

Private baselines and results are retained under the operator directory's
`security/` directory. The ephemeral reservation still expires approximately
`2026-11-23T13:48:00Z`; admission hardening does not renew it.
