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

qBittorrent uses SOCKS5 for trackers, peers, RSS, and miscellaneous requests. Its
local DNS forwarder sends hostname lookups through the same VPN. The only permitted
external pod traffic is encrypted UDP to its configured
WireGuard peer. A sing-box userspace WireGuard endpoint carries TCP and UDP traffic
through SOCKS5, including UDP trackers and public-torrent DHT. It starts before
qBittorrent and stays up until qBittorrent exits. Local discovery and
incoming port forwarding are disabled. The initial tunnel is IPv4; there is no
direct-internet fallback when the VPN fails. Private torrents retain their normal
restrictions on DHT and peer exchange.
