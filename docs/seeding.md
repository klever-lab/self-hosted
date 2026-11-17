# Seeding operations

Use the queue settings in [operations](operations.md#seeding-and-incoming-peers)
to keep completed torrents eligible to seed. Follow [SETUP.md](../SETUP.md) when
reserving or changing a forwarded port. The port and reservation deadline live in
the private operator configuration. Expiration closes incoming connectivity at
Windscribe; outgoing seeding can continue. There is no automated reservation or
renewal.

## VPN routing and pod security

The ordinary init container `vpn` configures kernel WireGuard (`wg0`, MTU `1120`
for Flannel-over-Tailscale headroom) and the Pod's firewall, waits for a handshake,
then **exits before qBittorrent starts**. There is no elevated VPN daemon, local
DNS daemon or HTTP control server alongside the application. The kernel retains
the tunnel and handles handshakes and transient WAN recovery. DNS uses the tunnel.
Built-in Pod Security Admission cannot express a per-container
exception, so the namespace retains `enforce: privileged` with restricted audit
and warnings. The enforcing `self-hosted-qbittorrent-security`
[admission policy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/)
and binding close that namespace-wide gap for every Pod creation/update and
debug-container update. Controller-created Pods receive the same checks.
The binding denies violations and the policy fails closed on evaluation errors.
Within the pod:

- The short-lived `vpn` initializer runs as root with only `NET_ADMIN`, a read-only
  root filesystem and no API token, application volume or writable script mount.
  It has no restart-as-sidecar setting and receives only read-only mounts of the
  VPN configuration and its setup script.
- `qbittorrent`, `configure`, and `webui` run as non-root `UID/GID 1000` with
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
  `seccompProfile: RuntimeDefault`, and `capabilities: {drop: [ALL]}`.

The exception requires the initializer named `vpn`, its exact upstream tools
image digest, startup command and prescribed mounts in a Pod labeled
`app: qbittorrent`. Persistent sidecars, environment injection, hooks, probes,
extra capabilities and application-data mounts are rejected. Every container
forbids privileged mode and
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
When updating the initializer's digest, command, or mounts, update its admission exception
together and run `scripts/check_admission.py` as described in [setup](../SETUP.md#tests).
Application service accounts have no permissions to modify these cluster policies.

qBittorrent disables proxies (`Proxy\Type=None`, `Session\ProxyPeerConnections=false`)
and binds BitTorrent directly to the tunnel interface (`Session\Interface=wg0`,
`Session\InterfaceName=wg0`) on the configured `Session\Port`. Incoming TCP and UDP
peers arrive on `wg0` with their real source addresses preserved.

Two independent layers enforce fail-closed VPN egress and private WebUI access:

1. **Pod firewall:** defaults to deny, permits the exact encrypted VPN endpoint,
   tunnel traffic and established replies, opens only the configured forwarded
   port on `wg0`, and allows new pod-network ingress only on WebUI port `8080`.
   IPv6 is blocked. If the tunnel fails or disappears, the rules remain in place.
2. **Kubernetes `NetworkPolicy`:** `wireguard-egress` restricts pod egress to the
   resolved WireGuard server UDP endpoint, while `qbittorrent-web` and
   `jellyfin-importer-api` restrict TCP `8080` ingress to `edge`, `book-importer`,
   and Jellyfin's `torrent-importer`.

The setup script is [network.sh](../apps/qbittorrent/network.sh). Its rollout
fingerprint changes when the script changes, so deployment reruns initialization.
`configure` receives only the peer-port key; only `vpn` mounts the private VPN key.
Application/session volumes and enrolled gateway identities are preserved.

Maintaining native incoming TCP/UDP and original peer addresses is a requirement.
A userspace connection proxy would alter that behavior. The remaining temporary
network capability sets up the kernel path; it is not present in the running
application. Root filesystem and kernel/runtime vulnerabilities remain separate
security boundaries, documented in Guaranine's escalation audit.

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
4. Blocked internet egress when bypassing `wg0` (`eth0` bind) or while the VPN
   endpoint is unavailable, followed by automatic recovery when it returns.
   Deliberately removing the tunnel requires recreating the Pod; no privileged
   daemon remains to reconstruct interfaces after administrator changes.

Record upload counters and elapsed time before and after the trial in protected
operator storage. Compare the same torrents and queue settings over substantial,
comparable periods; successful connectivity alone does not prove a better ratio.
If the trial is effective, the agreed next step is to buy a P2P-capable Windscribe
static IP and use a permanent port forward. Verify both P2P and forwarding support
for the chosen location and connection method before purchasing.

## Measure upload constraints

Compare torrent payload counters over elapsed time with the worker's physical
interface transmit counters. VPN framing, management traffic and Jellyfin share
that interface, so its total is not the same as qBittorrent's payload rate.
Keep short network tests bounded and send generated data, not application files.
To measure spare uplink capacity, record the current global upload limit,
temporarily lower it through the transfer API, and restore the exact prior limit
in cleanup, including interruption handling. Keep the VPN and its firewall active.
An upload test from the worker measures its Wi-Fi/ISP path; repeat from inside the
qBittorrent pod to measure the additional VPN path. A short test establishes an
observation, not the internet plan's advertised or guaranteed capacity.

On the existing spinning-disk worker, also inspect disk I/O pressure and the
qBittorrent cgroup's memory/cache counters. The deployment provides a 4 GiB
container budget and an explicit 2048 MiB cache for the pinned libtorrent 1.2
build. Let the cache warm before comparing sustained throughput. Adding peers
can increase random disk reads and interfere with Jellyfin before network
capacity is exhausted. Jellyfin's public Funnel path has separate provider
bandwidth limits; its server bitrate limit is a different setting.

## Local initializer verification (2026-11-17)

`tests/test_vpn_network.py` runs the shipped script in private user, mount and
network namespaces with synthetic WireGuard keys, a simulated provider and an
unprivileged application. It requires `iproute2`, `iptables`, `wireguard-tools`,
`nftables`, `util-linux` and subordinate UID/GID mappings; it fails rather than skips if the
fixture is unavailable. CI installs the tools and runs it with the unit suite.

The fixture verified incoming TCP and UDP with original source IPs, tunneled DNS,
unchanged private WebUI access, retry after route changes, endpoint outage and
recovery, and denied direct egress even after a fallback route was introduced.
Application probes had zero effective capabilities; the initializer ran with
only `NET_ADMIN`. These are local regression checks, not a new production
forwarding or seeding measurement. The replacement admission policy's rendered
templates and rejected privilege overrides also passed a disposable API server.

## Previous live trial verification (2026-11-16 UTC)

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

## Cache tuning verification (2026-11-16 UTC)

- The operator raised upload slots from 20 globally / 4 per torrent to 250 / 50.
  A short before/after observation rose from 6.15 Mbps over 61 seconds to
  34.2 Mbps over 54 seconds. This was not a controlled benchmark.
- The worker uses a rotational disk. qBittorrent reached its former 1 GiB
  cgroup limit, repeatedly reclaimed file cache, and showed substantial disk
  stalls while the worker still had about 12 GiB available. Deployed the 4 GiB
  allowance and explicit 2048 MiB libtorrent cache; the cache filled and no
  out-of-memory event occurred. Disk pressure varied and decreased in later
  samples, but a separate upload-speed gain from this change was not established.
- After the restart, torrent payload averaged about 31.5 Mbps over 8.5 minutes,
  including brief network tests. A subsequent operator-started download changed
  the workload and reduced observed upload speed. Downloads and seeds share the
  disk, cache and Wi-Fi airtime; compare equivalent workloads before attributing
  changes to a setting.
- Short Cloudflare upload probes were inconsistent, including timeouts and a
  broken pipe. About 40 Mbps of total physical transmit traffic was observed;
  these probes did not establish the Wi-Fi/ISP or VPN throughput ceiling. The
  temporary upload limit was restored to unlimited, with slots left at 250 / 50.
- Verified an external full BitTorrent TCP handshake and observed 90 incoming
  uTP peers, including active UDP uploads. Fresh DHT ping probes from the operator
  did not receive replies; that probe did not establish a UDP forwarding failure.
- All 40 local tests and 51 admission dry runs passed. The existing torrent
  hashes, PVC bindings and Tailscale identities were preserved; the operator's
  new download was retained. Protected results live under `seeding/`, referenced
  by `seeding/current-performance-audit` in the operator directory.
