#!/bin/sh
# Show current connectivity; status.txt records only the last setup attempt.
set -eu
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

wifi_connected=false
for carrier in /sys/class/net/wlan*/carrier; do
  if [ "$(cat "$carrier" 2>/dev/null || true)" = 1 ]; then
    wifi_connected=true
    break
  fi
done
if [ "$wifi_connected" = false ]; then
  printf 'Wi-Fi disconnected.\nThe library is unavailable until Wi-Fi reconnects.\n'
  exit 0
fi

printf 'Wi-Fi connected.\n'
if ! timeout 3 /usr/local/tailscale/bin/tailscale status --peers=false 2>&1; then
  printf '\nTailscale is not ready. Library access is unavailable.\n'
  exit 0
fi

book_name=$(sed -n '2p' /mnt/onboard/.adds/self-hosted-kobo/books-host)
case "$book_name" in
  ''|*[!a-z0-9.-]*) printf '\nInvalid library hostname.\n'; exit 1 ;;
esac
case "$book_name" in
  books.*.ts.net) ;;
  *) printf '\nUnexpected library hostname.\n'; exit 1 ;;
esac
if wget -q -T 5 -O /dev/null "https://$book_name/login" 2>/dev/null; then
  printf '\nPrivate library reachable over Tailscale.\n'
else
  printf '\nPrivate library unreachable. Wait for reconnection, then check again.\n'
fi
