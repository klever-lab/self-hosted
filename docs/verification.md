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

These initial checks cover the server-side Kobo protocol. Physical reader checks
are recorded below. A complete restore onto a replacement worker has not been
exercised. Download payloads are excluded
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

## Clara BW installation and pairing

Verified on the physical reader on 2026-11-16 UTC:

- The Clara BW runs firmware 4.46.23852 and Linux 4.9.77. The pinned official
  Tailscale 1.104.7 ARM daemon enrolled with the restricted Kobo tag. Its temporary
  USB enrollment key was consumed and removed.
- Native daemon logs show TUN engine creation, iptables and ip6tables absent, and
  successful selection of nftables. The reader reached the private Books HTTPS
  login endpoint without disabling certificate validation. This verifies the
  backend and authenticated tailnet routing without the inherited firewall bundle.
- A separate Calibre-Web device account has only download and viewer permissions.
  Its token authenticated Kobo initialization over HTTPS, generated private HTTPS
  cover URLs, and received HTTP 403 from both administrator pages checked.
- All 200 library books have EPUB and KEPUB copies on the server. The physical
  reader contains catalog entries for all 200, and four downloaded KEPUB archives
  passed complete ZIP integrity checks. No synthetic sync consumed the device's
  first library response.
- The reader configuration was backed up, and the application-account database
  backup passed SQLite integrity checks. Pairing changed only `api_endpoint` and
  `image_host`; a later `MaxDownload=0` setting requests downloads only when
  selected. Credentials, backups and device logs remain in protected operator
  storage outside Git. The reader was safely ejected.

The first physical sync attempt reported that the reader was full. USB inspection
then found 12.7 GB free, a healthy reader database, the sync endpoint reverted to
Kobo's store, and no Calibre-Web sync history for the device account. Reapplying
the endpoint in place and restarting the reader led to successful catalog sync
and downloads. USB readback confirmed that the endpoint persisted. The reader
reported 649 MiB free on the system partition and 12,126 MiB on the book partition.

The original **Tailscale status** menu displayed a saved installation result and
could misleadingly show a successful connection with Wi-Fi off. Its replacement
checks the current Wi-Fi link, the daemon and private Books HTTPS. Four isolated
checks covered Wi-Fi off, daemon unavailable, library unreachable and successful
connectivity. Shell syntax and ShellCheck passed for the scripts and menu commands.
Both updated reader files matched the repository after copying. The installation
result remains available separately under **Setup result**.

The live status display, on-demand download setting and opening a book still need
verification on the physical screen. Recovery after sleep and Wi-Fi changes has
not been exercised.

The first live status check exposed an invalid 10,000 ms NickelMenu timeout;
version 0.6.0 accepts values strictly below 10,000 ms. An unreadable setup result also
caused NickelMenu to display a process error. The menu now uses 9,000 ms and shows
a readable fallback when the saved result cannot be read. The HTTPS probe has
an outer five-second deadline so a stalled request fits within the menu timeout.
Regression tests reproduced both reported errors and the stalled-request case
before these fixes, then passed with the corrected commands. Native readback of
the final correction and the screen results remain separate physical checks.
