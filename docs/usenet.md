# Usenet books

altHUB is the indexer: it supplies search results and NZB instructions. Frugal is
the provider: SABnzbd authenticates to its servers to retrieve the actual data.
The two accounts are independent. SABnzbd downloads, verifies, repairs and unpacks
jobs before the book importer uploads supported files to Calibre-Web.

## Selecting and importing books

Download an NZB from altHUB and add it through SABnzbd's **Add NZB** control,
selecting category `books`. EPUB is preferred for Kobo. The importer also accepts
KEPUB, PDF, MOBI, AZW3 and FB2 up to 200 MiB per file. Import does not convert formats.
Files in other categories, failed jobs and jobs still unpacking are ignored.

Every two minutes the importer reads the complete SABnzbd history, including older
pages, and uploads new book files through Calibre-Web's authenticated endpoint.
The library is never mounted in the downloader or edited directly. Existing Kobo
pairing and library accounts remain in Calibre-Web.

Keep the `books` category's destination at `/downloads/complete/books`, with a
separate directory for each job. Do not clear completed history or delete payloads
until their books appear in the library. Successful history entries whose files
have been removed must be cleared manually after confirming the library import;
the importer stops on missing files rather than silently reporting success.

Run immediately or inspect scheduled imports with:

```bash
./self-hosted import-books sabnzbd
kubectl -n sabnzbd get jobs
kubectl -n sabnzbd logs job/JOB_NAME
```

Original downloads remain on the Usenet volume. Its ledger is independent of the
torrent import ledger: identical files are uploaded once per downloader, but
downloading the same file through both services can create duplicate library
entries. A `pending` receipt means an interrupted upload needs review; follow the
[import receipt recovery procedure](media-imports.md), using the `sabnzbd`
namespace and its `/state/imports.sqlite3`.

## Frugal servers

The primary preset uses 10 connections and priority 0; the separate bonus preset
uses 5 connections and priority 1. Both require SSL with strict certificate
verification on port 563. Enter Frugal's normal account username and password,
test each server, and enable it. A lower-priority server is asked only when a
higher-priority server lacks an article. Frugal's bonus allowance is 1,000 GB per
calendar month; check the account's actual entitlement before changing limits.
See [Frugal's server settings](https://frugalusenet.com/server-locations/) and
[FAQ](https://frugalusenet.com/faq-support/).

SABnzbd connects over verified TLS directly to the provider. Its public egress is
limited to TCP 563 for Usenet and TCP 443 for HTTPS, plus cluster DNS. It runs
without the qBittorrent VPN sidecar or elevated capabilities.

## Optional altHUB cart feed

Manual NZB uploads work without storing the altHUB API key in SABnzbd. To have
SABnzbd fetch selected NZBs itself, copy the private **cart RSS URL** from altHUB
into **Config → RSS**, assign `books`, and use that cart only for books. Enable
automatic processing and check the feed preview before adding more items.
SABnzbd skips the first feed batch by default; explicitly select the intended
items if importing that initial cart. Use the cart feed rather than an unrestricted
latest-releases feed. See [SABnzbd RSS behavior](https://sabnzbd.org/wiki/configuration/5.1/rss).

Provider credentials and private feed URLs are retained in the configuration PVC
and included in protected backups. Keep API keys and downloaded account files
outside Git. Deployment instructions belong in [SETUP.md](../SETUP.md).
