# Operations and recovery

Run `./self-hosted status` for pods and volumes. The applications run on the selected
existing worker; Guaranine's control plane remains dedicated to platform work.
Both applications use single replicas and Recreate deployments so updates do not
start multiple writers against their state.

## Private access

Both HTTPS gateways disable Tailscale Funnel and accept connections only through
the tailnet's access policy. Guaranine owns the policy and disables Funnel permission
for the tailnet. Deployments preserve the existing gateway identities and hostnames.
Devices without an authorized Tailscale connection cannot reach these applications;
see [Kobo access requirements](kobo.md) before configuring a reader.

## Storage

qBittorrent requests 1 GiB for configuration and 200 GiB for downloads. Its import
ledger requests 1 GiB. Calibre-Web requests 1 GiB for configuration and 20 GiB for
the library. OpenEBS retains volumes on claim deletion. These are declarations,
not enforced disk quotas; monitor the worker's free space. The volumes are local
and do not follow workloads to another worker. See Guaranine's
[storage guide](https://github.com/klever-lab/guaranine/blob/main/docs/storage.md).

## Backups

```bash
./self-hosted backup
```

This suspends new imports, waits for active importer jobs, briefly stops both
application writers, and streams their configuration, import ledger, and Calibre
book library to protected operator storage. Gateway identities, application Secrets,
operator configuration, and the VPN profile are included. Application replica
counts and the import schedule are restored afterward.

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

Redownload excluded torrent payloads or restore a separate copy before expecting
old seeding state to work. Do not delete retained claims or worker directories as
an update procedure.

## Dependency pins and empty library template

Upstream image references include immutable digests. Update and verify each service
independently; no GitHub Actions runner or automatic deployment is required.

`apps/calibre-web/empty-library.db` is the empty native database supplied by
[Calibre-Web](https://github.com/janeczku/calibre-web/blob/e71a3452e19e3631a492a143c3ddde60c10578bc/library/metadata.db).
SHA-256: `fac2f7e37ef4bc0fb2955e9a3f612604707d4ef573af5c28bd7a8369339f4bd4`.
It has no books and is copied only when the library does not exist. This tracked
template is distinct from the private live `metadata.db` and `app.db` files.
