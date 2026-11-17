# Friends VPN deferred ideas

Decision recorded on 2026-11-17: keep the deployed WireGuard service through
the OVH controller. The alternatives and additional controls below are deferred;
this note does not enable them. [Current operation](friends-vpn.md) and
[setup](../SETUP.md#public-friends-vpn) describe the deployed service.

## Current public entry

```text
Friend's WireGuard app
  -> OVH controller public IPv4, UDP 31820
  -> existing Flannel-over-Tailscale connection
  -> sing-box VPN Pod on a worker
  -> dedicated Windscribe WireGuard tunnel
  -> Internet
```

The controller uses Kubernetes' existing NodePort forwarding and Guaranine's
narrow host firewall exception. The added controller configuration is a
[Kairos profile](https://github.com/klever-lab/guaranine/blob/main/kairos/public-vpn.yaml)
with an nftables rule and a startup hook that runs and exits. It adds no resident
VPN or proxy daemon. sing-box terminates the VPN connections on a worker.
Guaranine's [public ingress guide](https://github.com/klever-lab/guaranine/blob/main/docs/security.md#public-vpn-ingress)
owns the controller rule and its removal procedure.

## Possible Funnel entry

The motivation is to move public VPN ingress and its traffic load away from the
controller without requiring friends to join the tailnet. A candidate path is:

```text
Friend's TLS-capable VPN app
  -> Tailscale Funnel public relay
  -> isolated gateway Pod on a worker
  -> dedicated Windscribe WireGuard tunnel
  -> Internet
```

The worker would host the Funnel endpoint; the controller would no longer forward
these public VPN sessions. The gateway would still need authentication,
revocation, workload isolation and restricted internet routing.

[Funnel](https://tailscale.com/docs/features/tailscale-funnel) accepts TLS over
TCP on ports 443, 8443 and 10000. Its ingress routes using the TLS ClientHello's
SNI hostname; merely selecting TCP port 443 is insufficient. Tailscale describes
[this routing](https://tailscale.com/blog/introducing-tailscale-funnel).

| Candidate | Fit for Funnel | Client implications |
| --- | --- | --- |
| Standard WireGuard | No direct UDP forwarding. | Existing WireGuard profiles cannot connect through Funnel. |
| Normal IKEv2/IPsec | No direct forwarding of its UDP/ESP traffic. | Ordinary IKEv2 profiles do not solve the Funnel requirement. |
| OpenVPN over TCP | Its packet framing is not an ordinary TLS connection. An additional outer TLS wrapper could adapt it. | A normal OpenVPN Connect profile alone is insufficient; wrapper support must be checked per platform. |
| OpenVPN with a stealth transport | OpenVPN inside TLS, or inside secure WebSockets, is a possible approach when it supplies the required TLS hostname. | Requires a client that implements the same transport and supports our gateway. |
| Trojan over TLS | Starts with ordinary TLS and carries TCP and UDP within the connection. First candidate to evaluate using the existing sing-box software. | Requires a compatible app and a new profile per device. |

References: [IKEv2 transport](https://docs.strongswan.org/docs/latest/howtos/ipsecProtocol.html),
[OpenVPN framing](https://openvpn.net/community-docs/openvpn-protocol/),
[stunnel's TLS wrapper](https://www.stunnel.org/manual.html),
[Windscribe's Stealth and WStunnel explanation](https://windscribe.com/blog/types-of-vpn-protocols/),
and the [Trojan protocol](https://trojan-gfw.github.io/trojan/protocol.html).

"Stealth" describes a family of transports, not a guarantee of Funnel
compatibility or resistance to detection. The relevant requirement here is an
ordinary TLS-over-TCP connection with the correct Funnel hostname.

The existing [sing-box Trojan inbound](https://sing-box.sagernet.org/configuration/inbound/trojan/)
supports individual user credentials. Its [graphical clients](https://sing-box.sagernet.org/clients/)
cover Android, Apple platforms and desktop systems. A client configured to
capture device traffic could present a normal VPN toggle and handle TLS itself,
without a separate stunnel process. Actual profile import, device routing and
Funnel compatibility have not been tested.

The friends-to-gateway protocol is independent of the gateway-to-Windscribe
protocol. Switching the public entry would not require changing the existing
Windscribe WireGuard profile to OpenVPN or IKEv2. Selecting Stealth only for the
Windscribe connection would not adapt the public entry to Funnel.

## Performance and costs to establish

Funnel's documentation states that bandwidth limits are non-configurable but
does not publish a numeric Mbps or monthly transfer allowance. No throughput is
promised or measured for this candidate. Benchmark public connections from the
actual client devices, including concurrent clients, downloads, uploads, UDP
DNS and behavior under packet loss, before choosing it as the sole entry.

No additional paid server or subscription is expected for a basic Funnel trial
or local hardening using existing resources. This is an estimate, not a billing
verification. Free country lists and existing monitoring could avoid new service
fees; they would use some additional CPU, memory and storage. Paid notification
services, hardware upgrades and provider access plans would be separate choices.

## Other deferred security ideas

- **North America source-IP filter:** filter connection origins rather than the
  websites clients may visit. Decide whether this means US/Canada/Mexico or the
  whole continent, including Central America and the Caribbean. Use maintained
  country data, validate updates and retain a working ruleset if downloads fail.
  Geolocation can misclassify addresses and can be bypassed using a proxy in an
  allowed country; device credentials remain the access control. On the current
  path, enforce the filter on the OVH controller. With Funnel, establish how to
  obtain and trust original client addresses before designing a filter; the
  worker's transport peer alone does not identify the public client.
- **Monitoring and alerts:** check end-to-end Windscribe connectivity, container
  restarts, resource use and unusual traffic volumes. Avoid collecting browsing
  histories or packet payloads.
- **Optional access expiry:** define temporary device access and a reliable
  revocation mechanism. Existing explicit revocation remains available.
- **Client kill switches:** document each supported device's always-on and
  block-without-VPN settings, where available.
- **Security maintenance:** review upstream VPN and platform security updates,
  updating image digests through the normal validation process.

Country filtering, the additional VPN monitoring and automatic access expiry
have not been implemented as part of this discussion.

## Checks before any future switch

1. Confirm friends' operating systems and acceptable client apps; verify TLS
   certificate and hostname validation, profile import and device VPN mode.
2. Keep the existing isolation, individual revocation, Windscribe-only routing,
   private-destination blocking, DNS protection and IPv6 leak prevention.
3. Keep a single active tunnel per Windscribe key. An alternate ingress can
   share the existing gateway's upstream; a separate parallel trial needs a
   separate provider key.
4. Measure the public Funnel path and verify unauthorized clients, revoked
   credentials, private destinations and upstream outages are handled correctly.
5. Only after a verified replacement, retire the NodePort and remove the
   controller's public VPN exception using Guaranine's procedure. Keep secrets
   outside Git and preserve a recovery path.
