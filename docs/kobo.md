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

On a paired reader with stale cover URLs, keep the same `api_endpoint` and set
`image_url_template` to that endpoint followed by
`/{ImageId}/{Width}/{Height}/false/image.jpg`. Set
`image_url_quality_template` to the same endpoint followed by
`/{ImageId}/{Width}/{Height}/{Quality}/{IsGreyscale}/image.jpg`.
These values contain the device token and belong only in the private reader
configuration. Restart once after correcting them and verify that they persist.

The Clara BW currently uses `MaxDownload=0` under `[FeatureSettings]` to request
downloads only when selected. Changes require a reader restart. Kobo normally pre-downloads
its [five most recent books](https://help.kobo.com/hc/en-us/articles/360017605034-Download-all-your-books-onto-your-Kobo-eReader).
The override is [reported by a Clara BW owner](https://www.mobileread.com/forums/showthread.php?t=370147&page=2)
and was checked on firmware 4.46.23852: the operator downloaded and opened a
selected book without other books downloading automatically. It does not remove
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
identity and logs stay on the private system partition. The default installation
blocks incoming tailnet connections and disables SSH and advertised routes. The installation bootstrap
adds the Books hostname mapping without changing the official store sync endpoint.
The fork's [security review](https://github.com/klever-lab/kobo-tailscale/blob/main/SECURITY.md)
records its package checks, isolated tests and remaining physical checks.

## Direct laptop maintenance

The installed Tailscale 1.104.7 client includes an SSH server and SFTP. No update
server, cluster workload or additional SSH daemon is needed. Apply Guaranine's
[laptop-only reader policy](https://github.com/klever-lab/guaranine/blob/main/docs/security.md#tailnet-policy)
before enabling access. It permits TCP 22 from the enrolled operator laptop only,
over IPv4 and IPv6, and requires the operator's Tailscale identity with a root SSH
check every 12 hours. The reader retains its Books-only outbound grant.

After safely ejecting and unplugging USB, enable Wi-Fi and choose
**NickelMenu → Enable laptop access**. From the operator laptop, connect with:

```bash
tailscale ssh root@kobo-clara-bw
```

Complete Tailscale's browser authentication check when requested. SCP/SFTP can
then copy reviewed files directly from the laptop to the reader. Keep library
settings, databases and `/usr/local/tailscale/state` intact; updating the menu or
status helper does not require reinstalling or enrolling Tailscale again. Do not
write onboard storage remotely while it is exported over USB.

Kobo can shut Wi-Fi down when its own network workflow is idle, even while an
SSH client is waiting. For remote work, choose **Keep Wi-Fi on**, then turn Wi-Fi
off and back on once. This uses NickelMenu's native
[`DeveloperSettings.ForceWifiOn` control](https://github.com/pgaskin/NickelMenu/blob/v0.6.0/res/doc#L98);
the setting takes effect on the next Wi-Fi toggle. It keeps the radio available
and uses more battery. Choose **Normal Wi-Fi power saving** and toggle Wi-Fi again
when finished. These controls change Wi-Fi idle behavior, independently of SSH
authorization and the reader's screen sleep settings.

**Disable laptop access** disables SSH and blocks incoming Tailscale connections;
library access remains available. Access settings persist across daemon restarts.
Running **Set up Tailscale** restores the installer's default blocked settings,
so enable laptop access again afterward if needed. Tailscale's SSH server handles
only tailnet traffic and opens no public or Wi-Fi LAN SSH listener. Native SSH,
SFTP and private library access passed the [physical reader checks](verification.md#clara-bw-installation-and-pairing).

Setup and storage diagnostics use `.log` filenames so Kobo does not import them
as TXT books. After removing or renaming old local files, **Refresh local books**
runs Kobo's full local import workflow to update the catalog. It does not request
a Calibre-Web library sync. The local import can briefly pause remote access.

## Library synchronization

The integration serves the self-hosted library and enables Calibre-Web's upstream
Kobo-store compatibility option. Books and covers in Calibre-Web are served
locally; requests for existing Kobo purchases can use the official store. This
preserves access to purchases that have not been imported as usable local files.
Restore an archived Kobo purchase with **Kobo.com → My Books → Archive → Restore
to Library**, then run the reader's normal **Sync**. Kobo documents this
[restore workflow](https://help.kobo.com/hc/en-us/articles/360017765873-Move-items-out-of-your-account-archive).
The restored purchase remains separate from any imported Calibre-Web copy.
Book deletion and read-state behavior follow the upstream implementation.

After pairing, safely eject the reader, unplug USB, enable Wi-Fi and use the normal
**Sync** action. Books appear under **My Books**; select one to download and read.
The device account syncs the whole library unless **Sync only selected shelves**
is enabled in its profile. For selective sync, enable that setting before the
first sync and mark the desired shelves for Kobo synchronization.

Manage routine changes in Calibre-Web: upload an EPUB or KEPUB, or edit an
existing book's metadata and cover, then sync the Kobo. Keep the existing book
record when correcting it so its UUID and pairing with the reader remain intact.
The reader can then download the books selected for offline reading. Cover
changes do not require reinstalling Tailscale or editing the reader configuration.

Native sync supports EPUB and KEPUB; the server converts EPUB with its bundled
kepubify. DRM-protected imports need a readable replacement or the original
purchased copy through Kobo. Keep an unusable imported duplicate out of the
device's catalog by archiving it while signed in as the device account. This
retains the library record and file. PDF and other unsupported sync formats need
a separate transfer workflow; see the upstream guide below.

To free reader storage while retaining a synced book in its catalog, use
**Remove → Remove download**. For several books, use **My Books → ⋯ → Manage
books**, select them, and choose **Remove download** in the removal dialog.
Kobo documents the distinction from [Remove from My Books](https://help.kobo.com/hc/en-us/articles/360017481294-Delete-multiple-books-from-your-eReader),
which removes the catalog entry and requires restoring it from the source's
archive. Original USB-loaded files may offer only removal of the entire entry;
their reading state is separate from a Calibre-Web copy with a different UUID.
Do not treat an uploaded copy as a migration of the original entry's progress.

If a selected download remains **Pending** despite working private HTTPS and a
valid download URL, use Calibre-Web's **Force Full Kobo Sync** action for the
dedicated device account, then run the reader's normal **Sync**. This resends the
catalog without replacing the library or clearing reading-state records. If a
book was removed from My Books, let the reader sync that removal first, then
restore the book from the device account's archive before requesting the full
refresh. This sequence restored *The Trial* with its existing library UUID and
resolved the observed stuck download on the Clara BW.

**NickelMenu → Tailscale status** checks the current Wi-Fi link, daemon and private
library HTTPS connection. Wait for the library-reachable result before syncing.
**Setup result** shows the saved installation outcome; it does not establish
current connectivity. A running Tailscale process needs Wi-Fi to reach the server.

If the reader reports that it is full, **NickelMenu → Storage status** displays
free space on both the system and book partitions and saves the result to
`.adds/self-hosted-kobo/storage-check.log`. Check the actual space, database health
and configured sync endpoint before removing books: the first pairing attempt
on the Clara BW showed this warning with 12.7 GB free and the endpoint reverted
to Kobo's store. No Calibre-Web book sync reached the server during that attempt.
Reapplying the endpoint in place and restarting the reader restored sync. Check
the live status, then retry sync after a restart if the endpoint has been reapplied.

The HTTPS proxy supplies `X-Scheme` and forwarded-protocol headers so generated sync
and download URLs use HTTPS. EPUB-to-KEPUB conversion uses the bundled kepubify binary.

See the upstream [Kobo integration guide](https://github.com/janeczku/calibre-web/wiki/Kobo-Integration)
for model-specific setup and troubleshooting.
