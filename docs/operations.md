# Operations and recovery

Run `./self-hosted status` for pods and volumes. Kubernetes chooses workers without
hostname selectors; existing local volumes still keep their consumers on the
owning worker. The gateways can restart on other workers using their existing
Kubernetes identity Secrets. Guaranine's control plane remains dedicated to platform work.
Applications use single replicas and Recreate deployments so updates do not
start multiple writers against their state.

qBittorrent retains its kernel WireGuard interface and original peer addresses.
Its [short-lived network initializer](seeding.md#vpn-routing-and-pod-security)
finishes before the application starts. Inspect that init container's logs and
completion status when startup stops before qBittorrent becomes Ready.

qBittorrent has a 4 GiB memory limit and a 512 MiB request. Its pinned
libtorrent 1.2 build uses an explicit 2048 MiB disk cache, set by the deployment
initializer. Keep the cache and container memory budget aligned: the remaining
allowance accommodates process data, peer buffers and filesystem cache. The
worker's spinning disk is shared with Jellyfin; increasing concurrent uploads can
increase random reads and delay other applications. Reassess caching when moving
to a libtorrent 2 build, which manages disk reads differently.

## Seeding and incoming peers

To remove the active-seed limit in VueTorrent, open **Settings → BitTorrent →
Torrent Queueing**, keep queueing enabled, and save these values:

| Setting | Value |
| --- | --- |
| Maximum active downloads | `3` |
| Maximum active uploads | `-1` (unlimited) |
| Maximum active torrents | `-1` (unlimited) |

Keep **Do not count slow torrents in these limits** unchecked to retain the
three-download limit even when downloads stall. Both unlimited values are needed
so the combined torrent limit does not constrain seeds. The configuration volume
retains these settings, and the deployment initializer leaves them unchanged.
After saving, check that completed, started torrents leave the queued state;
`stalledUP` means they are available to seed but are not currently uploading.
Applied and verified on 2026-11-16 through qBittorrent's preferences API: active
uploads and total active torrents are unlimited, while active downloads remain
limited to three. The configuration volume preserves these values.

The plan agreed on 2026-11-16 is to trial Windscribe's
[ephemeral port forwarding](https://windscribe.com/knowledge-base/articles/what-is-ephemeral-port-forwarding-and-how-to-use-it)
first. Pro accounts include one temporary forward, which expires after seven
days; record its port and expiry in protected operator storage. See
[seeding operations](seeding.md) for tunnel, firewall and peer-address behavior,
the local regression checks and the separate live-trial history.

Verify the forwarded peer port from outside the VPN and compare uploaded bytes
over comparable periods with the same torrents and queue settings. If incoming
connectivity works and seeding improves enough to justify the cost, **purchase a
P2P-capable Windscribe static IP and switch to a permanent port forward**. Confirm
the selected location and connection method support both P2P and permanent
forwarding before buying. The ephemeral forward is a trial, not the intended
long-term arrangement; no static IP has been purchased as part of this change.

## Private access

All HTTPS gateways disable Tailscale Funnel and accept connections only through
the tailnet's access policy. Guaranine owns the policy and gives these gateways no
Funnel permission. Deployments preserve the existing gateway identities and hostnames.
Devices without an authorized Tailscale connection cannot reach these applications;
see [Kobo access requirements](kobo.md) before configuring a reader.

## Storage

qBittorrent requests 1 GiB for configuration and 200 GiB for downloads.
Calibre-Web requests 1 GiB for configuration and 20 GiB for the library. The
operator's `calibre_worker` setting binds both Books volumes to the selected
worker by using claims named `config-WORKER` and `library-WORKER`. SABnzbd
requests 1 GiB of durable configuration and caps its temporary download volume
at 20 GiB. OpenEBS preserves local volumes on claim deletion. Monitor free
space on the backing disk. [Guaranine storage guide](https://github.com/klever-lab/guaranine/blob/main/docs/storage.md)

### Books snapshots and relocation

```bash
./self-hosted backup-books
./self-hosted move-books wild
```

`backup-books` briefly stops the Books writer and suspends the importer
schedules during the archive. It pauses active qBittorrent seeds for export
and upload, and resumes those same torrents after the upload completes or an
error occurs. It publishes a SHA-256-verified archive and its manifest under
`backups/` in the private Books bucket. The archive includes the Calibre catalog,
book files, accounts, Kobo pairing and reading state. The initial snapshot also
carries the former import ledgers for deduplication.

`move-books` takes the backup first and keeps the source writer stopped until
cutover. It downloads the archive from OVH on the destination worker, verifies
the checksum, checks both SQLite databases and starts Calibre-Web on the new
claims behind the existing Books HTTPS gateway. It keeps old claims available
for rollback. qBittorrent seeds during the destination restore and validation.

To stage a known snapshot on an available worker, run
`./self-hosted restore-books SNAPSHOT_ID WORKER`. It verifies and loads empty
claims without starting a Books writer. When replacing a failed worker, fence
it, set `calibre_worker` in private `config.json`, and deploy Calibre-Web.
Check the catalog, import sidecar and Kobo sync before retiring old storage.

The first move installs the qBittorrent inbox publisher before exporting
Calibre-Web and leaves the former SABnzbd importer suspended after the restore.
Deploy SABnzbd after verifying Books. Its deploy checks the queue and stages
completed jobs from the retained downloads claim before updating SABnzbd, then
removes the superseded importer and ingress rule. Retain the old downloads and
import-ledger claims through verification.

The staging check allows empty URL grabs with a unique title match in the live
cart and leaves their entries there for the new downloader. Drain other queued
work before the storage switch.

## Backups

```bash
./self-hosted backup
```

This suspends each deployed downloader's imports, waits for active importer jobs, briefly stops
application writers, and streams their configuration, import ledger, and Calibre
book library to protected operator storage. Gateway identities, application Secrets,
operator configuration, optional Usenet credentials, and the VPN profile are included.
SABnzbd's retained configuration includes its provider credentials and private RSS
URLs. Only deployed applications are exported. Application replica counts and
each import schedule are restored afterward.

Successful backups contain `manifest.json` with archive SHA-256 checksums. Every
archive member is read to check archive integrity. Incomplete exports retain a
`.partial` suffix. Archives use Kubernetes' SPDY exec transport for reliable streams
across this cluster. Treat the entire directory as private credentials and keep
an independent encrypted copy away from the operator computer.

**Download payloads are excluded.** Back up the download volume separately if the
original seeding files are needed; the book-library backup includes imported books.
Protecting a PVC with Retain does not produce an independent backup.

## Recovery

1. Verify backup checksums and fence the old worker before starting restored writers
   or reusing gateway identities elsewhere.
2. Provision the destination worker through Guaranine. Create fresh claims through
   this repository; changing a bound claim's node or storage class does not migrate data.
3. Keep application replicas at zero and the import CronJob suspended. Restore each
   archive's directories into the corresponding claims using a restricted maintenance
   pod running as UID/GID 1000. Preserve ownership and private modes.
4. Restore Secret **data** from the protected JSON exports into same-named Secrets;
   omit old object UIDs, resource versions, and managed fields. Preserve Tailscale state
   rather than enrolling a second copy of an existing identity.
5. Restore protected operator configuration, update its worker setting if needed,
   then redeploy and check authenticated login, library contents, and Kobo sync.

Redownload excluded Usenet payloads as needed. Restore torrent payloads or
redownload them before expecting old seeding state to work. Do not delete retained claims or worker directories as
an update procedure.

## Dependency pins and empty library template

Upstream image references include immutable digests. Update and verify each service
independently; no GitHub Actions runner or automatic deployment is required.

`apps/calibre-web/empty-library.db` is the empty native database supplied by
[Calibre-Web](https://github.com/janeczku/calibre-web/blob/e71a3452e19e3631a492a143c3ddde60c10578bc/library/metadata.db).
SHA-256: `fac2f7e37ef4bc0fb2955e9a3f612604707d4ef573af5c28bd7a8369339f4bd4`.
It has no books and is copied only when the library does not exist. This tracked
template is distinct from the private live `metadata.db` and `app.db` files.
