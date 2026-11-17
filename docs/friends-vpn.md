# Friends VPN operation

Friends connect to the cloud controller's public IPv4 address on UDP `31820`
using the standard [WireGuard app](https://www.wireguard.com/install/). No
Tailscale installation or tailnet membership is needed on their devices.
Each device gets its own private configuration and revocable key. Follow
[SETUP.md](../SETUP.md#public-friends-vpn) for initial deployment.

[Deferred ideas](friends-vpn-ideas.md) records possible Funnel/TLS alternatives
and additional security controls. The current deployment continues to use
WireGuard through OVH.

Windscribe's [personal-account sharing rules](https://windscribe.com/knowledge-base/articles/am-i-allowed-to-share-my-windscribe-account)
prohibit sharing an account with other people. [ScribeForce](https://windscribe.com/scribeforce)
is its group product; arrange suitable provider access before distributing the
service. Deploying this gateway does not change the provider's account terms.

## Client management

From the repository root, create and activate a client:

```bash
./friends-vpn add friend-phone
./friends-vpn deploy
./friends-vpn list
```

The CLI prints the private configuration's **path**, never its contents. Files
live under `${SELF_HOSTED_OPERATOR_DIR:-~/.local/state/self-hosted}/friends-vpn/`
with mode `0600` in a mode-`0700` directory. Give each device its own configuration;
simultaneous use of one WireGuard key on multiple devices disrupts connections.
`add` stages access locally; `deploy` activates it. A deployment briefly
recreates the single VPN Pod. Existing peer keys and the server identity persist.
An existing connection can retain its previous UDP mapping briefly after a Pod
replacement; toggle the client's tunnel if it does not reconnect promptly.

To remove a device immediately:

```bash
./friends-vpn revoke friend-phone
```

Revocation removes the peer from the configuration and redeploys. If it was the
last client, the CLI stops the service and waits for its Pod to disappear. A
failure applying the change is reported; resolve it before assuming access is
revoked. The old export is renamed with `.conf.revoked`. Previously allocated
addresses are not reused. No other client's key changes.

## Traffic and isolation

The `friends-vpn` namespace uses restricted Pod Security. Digest-pinned upstream
[sing-box](https://sing-box.sagernet.org/configuration/endpoint/wireguard/)
provides two WireGuard endpoints in userspace as UID/GID 1000, with no added
capabilities, host namespaces, TUN device, service-account token, or writable
root filesystem. The Kubernetes Secret contains generated configuration;
credentials and client exports are never stored in Git.

The public NodePort forwards encrypted traffic from the controller to a worker
over the cluster's existing Flannel-over-Tailscale connection. The controller
runs no application Pod. Guaranine owns the controller's narrow public ingress
exception; this repository owns the Service and workload.

Authenticated client TCP and UDP traffic is routed only into the dedicated
Windscribe endpoint. There is no direct-internet outbound or fallback. Network
policy permits new egress only to the resolved Windscribe IPv4/UDP endpoint;
responses to admitted WireGuard client traffic use connection tracking.
The application also rejects private, loopback, link-local, CGNAT and multicast
destinations, other VPN clients, and traffic initiated through the upstream
endpoint. This keeps cluster, tailnet, metadata and LAN destinations unavailable.
Client DNS queries to `1.1.1.1` travel inside the Windscribe tunnel.

Both default routes (`0.0.0.0/0` and `::/0`) appear in client profiles. The current
service carries IPv4 internet traffic and rejects IPv6, so native IPv6 cannot
bypass the VPN while it is selected. Userspace routing supports TCP and UDP;
arbitrary raw IP protocols are not part of this service. Client WireGuard MTU is
`1280`; the nested Windscribe endpoint uses `1120` for the cluster's overlay.
Public WireGuard UDP sockets permit fragmentation across the overlay.
WireGuard apps must keep the tunnel selected to protect traffic. Device settings
such as Android's always-on VPN and block-without-VPN are client-side controls.

This is a single-replica service on laptop workers. Throughput and availability
depend on the selected worker, Wi-Fi, the controller path and Windscribe. A Pod
being Running does not establish upstream connectivity. Verify public IPv4 and
DNS from a client after changes; also check IPv6 and private destinations remain
unavailable. Do not change either media application's VPN profile to fix this
service.

## State and recovery

Back up the protected `friends-vpn/` directory and its separate Windscribe profile
to private encrypted storage. They contain the server private key, client peer
keys and exported client secrets. The media application's `./self-hosted backup`
does not include this separate service. There is no PVC or mutable in-Pod state.
Keep only one deployment using the Windscribe profile at a time.

Restore these files with their original private permissions, keep the existing
server and peer keys, and follow the deployment procedure in SETUP.md. If the
controller's public address changes, update `endpoint` in protected
`friends-vpn/config.json` and the `Endpoint` line in each client export; import
the updated file on each device. Existing keys do not need replacement.

Changing the profile's endpoint hostname or port makes the next deployment
resolve a fresh address and update network policy and tunnel configuration
together. The selected address is cached in protected configuration. To refresh
the same hostname deliberately, remove `provider_address` there before deploying.
Keep changes in the repository before applying them to Kubernetes.

## Live verification

Verified on 2026-11-17 through the controller's public IPv4/UDP endpoint, using
an upstream sing-box WireGuard client on the operator computer:

- HTTPS and a 64 KiB download succeeded; two external services reported
  Windscribe exit IPv4 `149.88.98.74`, with country `CA`.
- Raw UDP DNS to `1.1.1.1` returned answers through both WireGuard tunnels.
- Kubernetes API, tailnet controller, cloud metadata, server-private and IPv6
  destinations were refused through the tunnel.
- A temporary second client worked, then lost access after revocation. The
  retained client's profile still worked.
- Replacing only this service's upstream with an unavailable WireGuard endpoint
  blocked internet traffic. Restoring the original configuration restored the
  Windscribe exit address.
- The VPN container reported UID/GID 1000 and zero effective capabilities;
  direct internet egress from the Pod was blocked.

The full application test suite, qBittorrent's live admission dry runs, and
Guaranine's native-profile and firewall packet checks passed. Private receipts
are in the protected operator `friends-vpn/` directory. This validates the
public WireGuard protocol path; a physical phone/desktop WireGuard app was not
used for these checks.
