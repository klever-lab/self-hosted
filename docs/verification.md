# Initial deployment verification

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
