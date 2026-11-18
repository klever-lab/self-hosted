# Download and library imports

qBittorrent downloads to `/downloads/incomplete` and `/downloads/complete` on its
retained download volume. The initial `books` category uses `/downloads/complete/books`.
Select that category when adding a book torrent. Other categories remain ordinary
downloads unless a separate integration is configured for them.

The Jellyfin integration publishes stream links for completed video downloads.
Its category mapping and catalogue state live with the private Jellyfin deployment.
The file gateway mounts downloads read-only, supports byte-range requests, and
accepts only the Jellyfin edge pod. It has no internet egress. The gateway does
not change qBittorrent's VPN routing or permit direct internet fallback.

Every two minutes, the qBittorrent publisher queries completed `books` torrents.
The SABnzbd sidecar reads completed `books` jobs from its local history. Both hash
supported files up to 200 MiB and publish them to the same private OVH bucket
under `inbox/<sha256>`. A conditional write lets the first publisher create the
object. Both publishers verify its hash metadata and size before treating it as
stored. Torrent originals stay on disk for seeding.

Calibre-Web's sidecar lists the inbox every two minutes, verifies the SHA-256 of
each downloaded file and submits it through the authenticated upload endpoint.
Its one import ledger deduplicates across both downloaders and is stored with
the Calibre-Web configuration. Only Calibre-Web writes its catalog and library.
The bucket is configured during [setup](../SETUP.md#books-object-storage).

EPUB is the preferred input for Kobo. The importer also accepts KEPUB, PDF, MOBI,
AZW3, and FB2; importing a format does not convert it into an EPUB. EPUB-to-KEPUB
conversion is supported by kepubify. General Calibre format conversion is disabled
because this image's bundled Calibre executables do not run correctly under Alpine.

Deleting a library book keeps its import receipt, so a repeated download of
the same bytes stays excluded. See [Usenet operation](usenet.md) for cart handling.

Publish completed torrents immediately with:

```bash
./self-hosted import-books
```

The command prints the Job name. Inspect the publication and the inbox importer with:

```bash
kubectl -n qbittorrent get jobs
kubectl -n qbittorrent logs job/JOB_NAME
kubectl -n calibre-web logs deploy/calibre-web -c books-inbox
```

The importer commits a `pending` receipt before submitting a book. Later polls
leave that digest pending and continue with other books. Verify the matching
files in Calibre-Web before repairing `/config/inbox/imports.sqlite3`. With the
Calibre-Web deployment stopped, record the correct book ID if the upload succeeded.
Remove the pending row only after confirming the upload did not succeed.

## Gutenberg reading list

The fixed [200-book list](../integrations/books/greatest-200.json) follows
[The Greatest Books](https://thegreatestbooks.org/) all-time ranking, passing over
works without an identified English Project Gutenberg edition. The manifest records
the ranking snapshot, original rank, Gutenberg IDs, and edition choices. General
poetry and story collections use the identified public-domain editions; these can
differ from modern collections. Summaries, critical studies, indexes, and incomplete
volumes do not substitute for the ranked work.

Run from the operator computer with `rsync`, Tailscale, and cluster access:

```bash
./self-hosted import-gutenberg --dry-run
./self-hosted import-gutenberg
```

The command imports this list once and stops. `--limit N` selects the first N entries,
up to 200. It checks local and worker free space, fetches only the named EPUB paths
from Gutenberg's rsync mirror, validates each EPUB's ZIP contents and Gutenberg
identifier, then uses Calibre-Web's authenticated upload endpoint. Existing Gutenberg
IDs are skipped. Application deployment settings and the torrent import schedule
are independent of this operator command.

Multipart works are combined into a single EPUB with
[EpubMerge](https://github.com/JimmXinu/EpubMerge/tree/8cc76632722a94abc2ad08ff0f5cbda83ffb96eb).
The script downloads its engine at that commit and verifies its SHA-256 before use.
The merged book retains each volume's contents and navigation and uses the first
volume's Gutenberg URL as its source identifier. This keeps each selected work in
one library entry. The manifest lists all constituent volumes.

Downloads, the merge engine, `imports.sqlite3`, and `completion.json` live under
`gutenberg-top-200/` in the protected operator directory. The final receipt maps all
selected Gutenberg IDs to verified library book IDs. Keep the receipt and ledger
with operator backups; book files and library databases stay outside Git.

The ledger records upload intent before each POST. A later run reconciles an
interrupted upload against the library's identifiers. If an uncertain upload has no
matching library book, it stops for review. Confirm that Calibre-Web finished the
request and that the book is absent before removing that one `pending` ledger row
and retrying. A previously recorded book that has been deleted also needs review.
An existing first volume of a multipart work needs review before it can be treated
as the complete combined edition. The command only reads the live library database;
Calibre-Web remains its sole writer.

## Torrent network access

qBittorrent uses SOCKS5 for trackers, peers, RSS, and miscellaneous requests. Its
local DNS forwarder sends hostname lookups through the same VPN. The only permitted
external pod traffic is encrypted UDP to its configured
WireGuard peer. A sing-box userspace WireGuard endpoint carries TCP and UDP traffic
through SOCKS5, including UDP trackers and public-torrent DHT. It starts before
qBittorrent and stays up until qBittorrent exits. Local discovery and
incoming port forwarding are disabled. The initial tunnel is IPv4; there is no
direct-internet fallback when the VPN fails. Private torrents retain their normal
restrictions on DHT and peer exchange.
