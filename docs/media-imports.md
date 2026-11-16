# Download and library imports

qBittorrent downloads to `/downloads/incomplete` and `/downloads/complete` on its
retained download volume. The initial `books` category uses `/downloads/complete/books`.
Select that category when adding a book torrent. Other categories remain ordinary
downloads and are available for future integrations.

Every two minutes, the importer queries qBittorrent for completed `books` torrents.
It hashes supported completed book files up to 200 MiB and uploads new content through Calibre-Web's
authenticated upload endpoint. It has a read-only download mount and a separate
durable import ledger; Calibre-Web alone owns its library files and database. No
download volume is shared across namespaces and no library file is edited directly.

EPUB is the preferred input for Kobo. The importer also accepts KEPUB, PDF, MOBI,
AZW3, and FB2; importing a format does not convert it into an EPUB. EPUB-to-KEPUB
conversion is supported by kepubify. General Calibre format conversion is disabled
because this image's bundled Calibre executables do not run correctly under Alpine.

Original files and torrents stay available for seeding. The SHA-256 ledger prevents
repeat imports of identical files, including files found in multiple torrents.
Deleting a library book does not clear its ledger entry or import it again.

Run an import immediately with:

```bash
./self-hosted import-books
```

The command prints the Job name and a log command. Inspect scheduled runs with:

```bash
kubectl -n qbittorrent get jobs
kubectl -n qbittorrent logs job/JOB_NAME
```

The uploader commits a `pending` ledger entry before sending data. If a response is
interrupted, it stops rather than risking duplicate books. Check the Calibre-Web
library before changing a pending entry in `/state/imports.sqlite3`: mark it
`imported` with the correct book ID if the upload succeeded, or remove that entry
to retry after confirming it did not. Suspend the CronJob while repairing the ledger.

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
