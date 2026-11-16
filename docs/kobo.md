# Kobo and library accounts

Log into the books HTTPS URL with the generated administrator credentials. Add other
readers through **Admin → Add new user**. Give each person their own account; the
default reader role permits downloading, reading, shelf management, and password
changes. Public registration and anonymous library access remain disabled.

For each Kobo:

1. Create a dedicated device account with **Downloads Allowed** and **Viewer**
   permissions. Leave administrator, upload, book editing and deletion permissions
   disabled. Its reading state is separate from the administrator account.
2. Open that account's profile as an administrator and choose
   **Kobo Sync Token → Create/View**.
3. Connect the Kobo by USB and back up `.kobo/Kobo/Kobo eReader.conf`.
4. Under `[OneStoreServices]`, replace `api_endpoint` with the generated HTTPS
   Calibre-Web endpoint and set `image_host` to the Books HTTPS origin. Save,
   eject, then sync the reader over Wi-Fi.

For downloads only when selected, set `MaxDownload=0` under `[FeatureSettings]`
in the reader configuration, then restart the reader. Kobo normally pre-downloads
its [five most recent books](https://help.kobo.com/hc/en-us/articles/360017605034-Download-all-your-books-onto-your-Kobo-eReader).
The override is [reported by a Clara BW owner](https://www.mobileread.com/forums/showthread.php?t=370147&page=2)
and still needs a manual download check on this firmware. It does not remove
existing downloaded books.

Treat the generated endpoint as a password. Each account has its own token, which
can be revoked from the profile. The books endpoint is private to Tailscale. Kobo
sync requires Tailscale on the reader or its network to route through an authorized Tailscale device;
ordinary Wi-Fi alone cannot reach it. The device must support the server's HTTPS certificate.

For the Clara BW, use the [pinned installer from our fork](../SETUP.md#kobo-tailscale-source).
It preserves Kobo's DNS configuration, so private HTTPS names need a resolver or
local hostname mapping that points to the server's Tailscale address. Keep the
HTTPS hostname when configuring sync so certificate validation works.

The reader has a separate Tailscale tag that permits only Books HTTPS. Its node
identity and logs stay on the private system partition, and unsolicited tailnet
connections, SSH and advertised routes are disabled. The installation bootstrap
adds the Books hostname mapping without changing the official store sync endpoint.
The fork's [security review](https://github.com/klever-lab/kobo-tailscale/blob/main/SECURITY.md)
records its package checks, isolated tests and remaining physical checks.

The integration serves the self-hosted library; proxying the official Kobo store is
disabled. Existing store purchases and device-side behavior should be checked on
the actual Kobo before relying on it as a replacement for the store's sync endpoint.
Book deletion and read-state behavior follow the upstream implementation.

After pairing, safely eject the reader, unplug USB, enable Wi-Fi and use the normal
**Sync** action. Books appear under **My Books**; select one to download and read.
The device account syncs the whole library unless **Sync only selected shelves**
is enabled in its profile. For selective sync, enable that setting before the
first sync and mark the desired shelves for Kobo synchronization.

**NickelMenu → Tailscale status** checks the current Wi-Fi link, daemon and private
library HTTPS connection. Wait for the library-reachable result before syncing.
**Setup result** shows the saved installation outcome; it does not establish
current connectivity. A running Tailscale process needs Wi-Fi to reach the server.

If the reader reports that it is full, **NickelMenu → Storage status** displays
free space on both the system and book partitions and saves the result to
`.adds/self-hosted-kobo/storage-check.txt`. Check the actual space, database health
and configured sync endpoint before removing books: the first pairing attempt
on the Clara BW showed this warning with 12.7 GB free and the endpoint reverted
to Kobo's store. No Calibre-Web book sync reached the server during that attempt.
Reapplying the endpoint in place and restarting the reader restored sync. Check
the live status, then retry sync after a restart if the endpoint has been reapplied.

The HTTPS proxy supplies `X-Scheme` and forwarded-protocol headers so generated sync
and download URLs use HTTPS. EPUB-to-KEPUB conversion uses the bundled kepubify binary.

See the upstream [Kobo integration guide](https://github.com/janeczku/calibre-web/wiki/Kobo-Integration)
for model-specific setup and troubleshooting.
