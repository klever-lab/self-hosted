# Usenet books

altHUB is the indexer: it supplies search results and NZB instructions. Frugal is
the provider: SABnzbd authenticates to its servers to retrieve the actual data.
The two accounts are independent. SABnzbd downloads, verifies, repairs and unpacks
jobs before the book importer uploads supported files to Calibre-Web.

## Operator CLI

Use `./althub` from the checkout. It loads `althub_api_key` from the existing
private operator `usenet.json` and uses the repository's pinned dependencies.
It does not require cluster access for searches or cart changes.

```bash
./althub search "Asimov I Robot"
./althub files RELEASE_ID
./althub cart add RELEASE_ID
./althub cart remove RELEASE_ID
```

Copy `id` from a search result in place of `RELEASE_ID`. Search defaults to the
ebook category and one page of 20 results. Use `--category books` or
`--category all`, `--limit 1..100`, and `--offset N` when needed. Results include
sizes, posted dates and public details links. File formats come from the quoted
filenames in NZB metadata through the separate `files` command; release titles
are not treated as format evidence. An unparseable filename is reported without
a guessed format. The command does not check the book's contents or provider availability.

`files` fetches the NZB metadata and counts against altHUB's NZB-grab allowance;
it does not fetch the book payload from Frugal. The client follows NZB redirects
only within altHUB's HTTPS hosts. API keys and private download URLs are excluded
from output. All successful commands return JSON on stdout; operational errors
return JSON on stderr and exit 1. Invalid command arguments exit 2.

Cart add reports `added` or `already_present`. There are no automatic retries,
and a timed-out cart request may have succeeded; repeating an add will not create
another entry. Cart removal affects only the supplied release. Read the cart in
altHUB's website: its API does not expose the standard `t=cart` read operation.
If a cart RSS feed is enabled in a downloader, adding to that cart can trigger
its downloads. The CLI does not configure or enable feeds.

### Reuse audit

The existing NZBGet executable controls a downloader and has no indexer search or
cart commands. [clnns](https://github.com/dryes-old/clnns/blob/master/clnns.py)
provides a Python 3 search/download CLI but no cart API. The
[Newznab Python wrapper](https://github.com/gugahoi/newznab_python_wrapper/blob/master/client.py)
uses Python 2 syntax, defaults to HTTP, and sends `guid` where altHUB's cart API
requires `id`. [maybe-newznab-client](https://github.com/furbyhaxx/maybe-newznab-client/blob/master/src/client.rs)
is a Rust library with caps/search support and no cart command. The Python
[`nzb` package](https://pypi.org/project/nzb/) parses and edits NZBs but does not
provide indexer search or cart interaction.

None was verified as a drop-in replacement for this workflow. The small adapter
instead reuses `requests.Session` (the existing requests 2.34.2 dependency) for
HTTP, encoding, redirects and connection cleanup; `argparse`, `json`,
`urllib.parse` and `xml.etree.ElementTree` for parsing;
and the deployment manager's protected credential loader. It adds no dependency.
Only altHUB's endpoint, output fields, redirect host policy and
[Newznab API operations](https://newznab.readthedocs.io/en/latest/misc/api.html)
are application-specific.

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
