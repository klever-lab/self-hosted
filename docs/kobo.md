# Kobo and library accounts

Log into the books HTTPS URL with the generated administrator credentials. Add other
readers through **Admin → Add new user**. Give each person their own account; the
default reader role permits downloading, reading, shelf management, and password
changes. Public registration and anonymous library access remain disabled.

For each Kobo:

1. Sign into Calibre-Web as the reader who owns the device.
2. Open the profile and choose **Kobo Sync Token → Create/View**.
3. Connect the Kobo by USB and back up `.kobo/Kobo/Kobo eReader.conf`.
4. Under `[OneStoreServices]`, replace `api_endpoint` with the generated HTTPS
   Calibre-Web endpoint. Save, eject, then sync the reader over Wi-Fi.

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

The HTTPS proxy supplies `X-Scheme` and forwarded-protocol headers so generated sync
and download URLs use HTTPS. EPUB-to-KEPUB conversion uses the bundled kepubify binary.

See the upstream [Kobo integration guide](https://github.com/janeczku/calibre-web/wiki/Kobo-Integration)
for model-specific setup and troubleshooting.
