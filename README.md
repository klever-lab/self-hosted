# Self-hosted applications

Application deployments and integrations on [Guaranine](https://github.com/klever-lab/guaranine).
Guaranine owns the Kubernetes platform. This repository owns the applications.

- **qBittorrent / VueTorrent:** private HTTPS administration, persistent downloads, and a dedicated
  Windscribe WireGuard tunnel via Gluetun (`wg0` interface binding and kill switch).
- **Calibre-Web / K Books:** authenticated HTTPS library, EPUB/KEPUB support, and
  per-user Kobo sync. The library is available only through Tailscale.
- **Book importer:** completed downloads in qBittorrent's `books` category are uploaded
  to Calibre-Web every two minutes. The original files remain available for seeding.
- **Jellyfin file gateway:** read-only access to download payloads for the separately
  managed Jellyfin integration. Original files remain on the download volume.
- **Gutenberg reading list:** a fixed selection of 200 English classics from
  The Greatest Books ranking, imported on demand into the same library.

Each application has its own namespace, storage, access policies, and deployment.
Deployments use digest-pinned upstream images on existing workers.

Start with [SETUP.md](SETUP.md). See [media imports](docs/media-imports.md),
[Kobo setup](docs/kobo.md), and [operations and recovery](docs/operations.md).
The initial [verification record](docs/verification.md) lists completed checks and
their limits.

This repository contains configuration and an empty Calibre library template.
Credentials, VPN profiles, book collections, user databases, and backups live outside Git.
