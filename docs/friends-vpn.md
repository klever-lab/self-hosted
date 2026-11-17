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

Invite a friend from the repository root and share the link printed by the CLI.
Cloudflare sends the email code when they sign in.

```bash
./friends-vpn invite friend@example.com
```

The same dedicated allowlist is editable in the Cloudflare Access dashboard.
Repeated invitations for the same address leave one rule. Work on the list from
one admin session at a time, since Cloudflare updates the whole set of rules.

At [vpn.bahasadri.com](https://vpn.bahasadri.com), the friend names each device
and selects **Create and activate**. **Ready to import** means the gateway
confirmed activation. They can download the `.conf` on the device that needs
it or show a QR code for the WireGuard app on another device. Import it in the
app and enable the tunnel. Creating profiles requires a current browser that
supports Web Crypto X25519.

Each person sees their own devices. **Replace** activates a new profile and
revokes the old one together. **Revoke** removes a lost or retired device.
**Retry activation** resumes the same enrollment after a temporary error. The
browser holds the private key while setup is in progress and clears it when the
friend clicks **Done**. If the setup tab is closed before import, replace the
profile to issue a new key. Treat both a downloaded config and its QR code as
credentials. Give each device its own config since sharing a key disrupts
connections.

The CLI uses the same inventory. Use the device ID shown by `list` for
individual administration. Profiles issued before the portal retain their
existing keys and work with their existing files. Assign the owner to show a
legacy device in their portal and include it in email-wide revocation.

```bash
./friends-vpn add friend-phone --email friend@example.com
./friends-vpn assign existing-laptop friend@example.com
./friends-vpn list --email friend@example.com
./friends-vpn revoke friend-phone
./friends-vpn revoke --email friend@example.com
./friends-vpn offboard friend@example.com
```

`add` prints the path of the private export. Files are mode `0600` under
`${SELF_HOSTED_OPERATOR_DIR:-~/.local/state/self-hosted}/friends-vpn/`.
Revocation archives a CLI export as `.conf.revoked`. Adds and revocations
update the running gateway and preserve other peers' connections, including
when the last peer is removed. Previously allocated addresses are retained to
prevent reuse while old flows are draining. The current address pool supports
253 cumulative allocations. Retry the same command if gateway confirmation
times out.

`offboard` blocks the email at the portal, removes its VPN profiles, removes its
Access invitation, and ends its existing Access sessions for the Cloudflare
account. `revoke --email` only revokes the profiles. Use `invite` to restore
portal access to a previously offboarded email. To complete offboarding through
the Cloudflare dashboard, run `offboard` after removing the email from the policy.

## Traffic and isolation

The `friends-vpn` namespace uses restricted Pod Security. The gateway builds
from pinned [sing-box](https://sing-box.sagernet.org/configuration/endpoint/wireguard/)
with a small patch that applies peer changes to the running userspace WireGuard
interface. The CLI verifies the executable against the release pin before
staging it in immutable ConfigMaps. Both Pods verify the staged release before
starting it in a digest-pinned Alpine image. Containers run
as UID/GID 1000 with no added capabilities, host namespaces, TUN devices, or
writable root filesystems. Gateway and portal service accounts can access only
the shared device Secret and gateway acknowledgement through their scoped
roles. The server and provider private keys are in the gateway's separate
configuration Secret. The portal receives only the verified Access email and
the devices' public keys for provisioning. Client private keys stay in the
browser or in protected CLI exports.

The public NodePort forwards encrypted traffic from the controller to a worker
over the cluster's existing Flannel-over-Tailscale connection. The controller
runs no application Pod. Guaranine owns the controller's narrow public ingress
exception; this repository owns the Service and workload.

Authenticated client TCP and UDP traffic is routed only into the dedicated
Windscribe endpoint. The gateway network policies allow the resolved Windscribe
IPv4/UDP endpoint and the Kubernetes API used for peer updates. The Kubernetes
API is used by the gateway process to reconcile peer state. Responses to
admitted WireGuard client traffic use connection tracking.
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

Run `./friends-vpn backup` to export the current shared inventory, then back up
the protected `friends-vpn/` directory and separate Windscribe profile to
encrypted storage. These files hold server credentials, the device inventory,
and manual CLI exports. The media application's backup covers the media apps.
Keep only one gateway using the Windscribe profile at a time.

If the inventory Secret is lost, restore these files with their original private
permissions and run `./friends-vpn deploy --restore-inventory /private/path/inventory.json`.
Use the latest snapshot and reconcile any subsequent revocations before bringing
the portal back. Restore preserves the existing server and peer keys. If the
controller's public address changes, update `endpoint` in protected
`friends-vpn/config.json` and the `Endpoint` line in each client export; import
the updated file on each device. Then deploy to update the portal's profile
endpoint. Existing keys can still be used.

Changing the profile's endpoint hostname or port makes the next deployment
resolve a fresh address and update network policy and tunnel configuration
together. The selected address is cached in protected configuration. To refresh
the same hostname deliberately, remove `provider_address` there before deploying.
Keep changes in the repository before applying them to Kubernetes.

Portal and connector restarts affect enrollment and device management. The
gateway continues to carry enrolled WireGuard traffic independently. The
first switch from stock sing-box to the live updater replaces the gateway Pod
once. If a connection retains the previous UDP mapping after that migration,
toggle that device's WireGuard tunnel.

The [release workflow](../.github/workflows/friends-vpn-release.yml) runs the
userspace WireGuard test and publishes the Linux amd64 executable. Update
[the release pin](../apps/friends-vpn/release.json) with the workflow's tag,
compressed size, and SHA-256 when upgrading. The source for the gateway includes
the upstream version and [peer update patch](../apps/friends-vpn/live/sing-box.patch).

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
