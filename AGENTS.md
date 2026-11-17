# Self-hosted applications

Read README.md and SETUP.md before changing deployments. Guaranine owns the
Kubernetes platform; this repository owns application deployments and integrations.

- Keep services independently deployable under apps/. Use upstream images pinned
  by digest and restricted Pod Security. qBittorrent's admission policy enforces
  its namespace with one pinned Gluetun sidecar exception; keep that policy and
  binding active and verify changes with scripts/check_admission.py.
- Keep credentials, VPN profiles, state, and backups outside Git. The operator
  directory defaults to ~/.local/state/self-hosted with private permissions.
- Preserve existing application data and enrolled Tailscale identities on deploy.
- Make configuration changes in this repository before applying them to the cluster.
- Validate changes, then commit and push completed work to main.

SETUP.md owns deployment instructions. docs/ owns operations and service behavior.

## Ebook selection

- Select ebooks for Kian's Calibre-Web library and Kobo Clara (the enrolled
  reader is a Clara BW). For science-fiction requests, favor complete English
  editions of well-regarded works; use reputable reading lists, awards, and the
  user's preferences to guide choices. Check the author, edition or translation,
  and series reading order; exclude samples, summaries, and study guides.
- Prefer DRM-free, reflowable EPUB or KEPUB with readable text, working chapter
  navigation, and correct title, author, language, cover, and series metadata.
  PDF/scans, audiobooks, and MOBI/AZW3/FB2-only releases do not fit this library's
  native Kobo sync workflow. General format conversion is disabled; see
  docs/media-imports.md and docs/kobo.md for the implemented format behavior.
- Verify actual filenames and, for archives, the inner file listing. A release
  title containing "epub" or "retail" is not proof of its format or quality.
  Mark archives without an inner listing as unverified. Metadata alone cannot
  establish DRM status, completeness, formatting, or provider availability;
  validate the extracted ebook before calling it ready for import. Each book
  file must fit the importer's 200 MiB limit.
- Compare candidates with the existing library and cart before adding them.
  Avoid duplicate works and unintended omnibus overlap; distinguish an unusable
  existing copy from a readable one. For existing Kobo imports, check chapter
  readability: a valid ZIP and absent encryption.xml do not prove DRM-free text.
  Preserve existing library records, UUIDs, and reading state when correcting
  a book.
- The altHUB CLI is ./althub at the repository root; its implementation is
  scripts/althub.py. Keep cart review read-only unless changes or downloads are
  requested: a cart feed can trigger downloads. Keep review receipts, downloaded
  books, credentials, and private feed URLs in the protected operator directory,
  outside Git.
