# Initial deployment verification

The public Funnel check below records the original deployment. It is superseded
by the private access configuration described in [operations](operations.md#private-access).

Verified against the live Guaranine deployment on 2026-11-16 UTC:

- Kubernetes accepted the manifests with restricted Pod Security. Applications and
  gateways run without root, privilege escalation, host mounts, or added capabilities.
- Both HTTPS interfaces accepted their generated credentials. Anonymous library
  access redirected to login. The books endpoint also worked through the public
  Funnel address, independently of tailnet DNS.
- qBittorrent used a distinct WireGuard private key. Its encrypted proxy reached
  public HTTPS endpoints while direct internet access was blocked. Local DNS,
  SOCKS5 UDP relaying, and a native qBittorrent UDP tracker announcement succeeded.
  The existing Jellyfin VPN continued to work with its own identity.
- A public-domain EPUB downloaded through qBittorrent from a Gutenberg HTTPS web
  seed. The importer added it once; repeated imports added no duplicates and
  preserved the original download.
- Calibre-Web converted the sample to KEPUB. A fresh Kobo API session returned a
  valid HTTPS download URL and a valid KEPUB ZIP archive.
- Redeployment and application restarts preserved accounts, the library, imports,
  and gateway identities. Gateway RBAC allowed access to its identity Secret and
  denied access to application credentials.
- An off-worker state backup passed archive checksums, complete archive reads, and
  integrity checks of every archived SQLite database.
- Python compilation, shell syntax, Kustomize rendering, server-side admission,
  whitespace checks, and a scan for the actual deployment secrets passed.

These checks cover the server-side Kobo protocol. The physical reader was not
connected and still needs its one-time endpoint configuration. A complete restore
onto a replacement worker has not been exercised. Download payloads are excluded
from the application-state backup, as described in [operations](operations.md).

## Private access verification

This records the initial lockdown. Jellyfin's separate repository subsequently
restored public access with a single-IP allowlist. Books and qBittorrent remain private.

Verified on 2026-11-16 UTC after disabling public access:

- The live tailnet policy passed its access tests and matched the repository policy
  with no Funnel permissions. The previous policy was backed up outside Git.
- All four application gateways reported Funnel disabled. Books, qBittorrent,
  Jellyfin and Grafana responded over their private Tailscale addresses.
- HTTPS connections forced through every public IPv4 relay address for Books,
  Jellyfin and qBittorrent failed before receiving an HTTP response, including
  addresses cached before the change.
- The cluster had no Ingress resources, public Services, host-network pods or
  host ports. Persistent-volume bindings, application writer pods and enrolled
  gateway identities were preserved.
- Jellyfin's live verification passed its private HTTPS, source-address,
  restricted RBAC and egress-isolation checks.
