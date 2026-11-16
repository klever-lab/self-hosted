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
can be revoked from the profile. No Tailscale client is needed on the Kobo because
the books endpoint uses Funnel. The device must support the server's HTTPS certificate.

The integration serves the self-hosted library; proxying the official Kobo store is
disabled. Existing store purchases and device-side behavior should be checked on
the actual Kobo before relying on it as a replacement for the store's sync endpoint.
Book deletion and read-state behavior follow the upstream implementation.

The HTTPS proxy supplies `X-Scheme` and forwarded-protocol headers so generated sync
and download URLs use HTTPS. EPUB-to-KEPUB conversion uses the bundled kepubify binary.

See the upstream [Kobo integration guide](https://github.com/janeczku/calibre-web/wiki/Kobo-Integration)
for model-specific setup and troubleshooting.
