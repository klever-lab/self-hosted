#!/bin/sh
# Run from NickelMenu after the operator has staged a reviewed installer and key.
set -eu
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
onboard=/mnt/onboard/.adds/self-hosted-kobo
install_root=/usr/local/tailscale
mkdir -p "$onboard"
status() { printf '%s\n' "$1" > "$onboard/status.log"; }
backend_check() {
  grep -E 'iptables not found|falling back to nftables|netfilter running in|wgengine.NewUserspaceEngine|Engine creation' \
    "$install_root/state/daemon.log" > "$onboard/backend-check.log" 2>/dev/null || true
}
trap backend_check EXIT
trap 'status "Setup stopped. Reconnect USB for the recorded installation result."; exit 1' HUP INT TERM
status 'Installing the reviewed Tailscale package...'
if [ ! -x "$install_root/bin/tailscale" ]; then
  if ! sh /mnt/onboard/kobo-tailscale/clara-bw/install-tailscale.sh > "$onboard/install.log" 2>&1; then
    status 'Installation stopped before enrollment. Reconnect USB to inspect install.log.'
    exit 1
  fi
fi
"$install_root/boot.sh"
attempt=0
while [ ! -S /var/run/tailscale/tailscaled.sock ]; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 20 ] || { status 'Tailscale did not start. No enrollment was attempted.'; exit 1; }
  sleep 1
done

# Consume the temporary USB key into private storage before authenticating.
key="$install_root/state/enroll.key"
if [ -f "$onboard/auth.key" ]; then
  cp "$onboard/auth.key" "$key"
  chmod 600 "$key"
  rm -f "$onboard/auth.key"
fi
if [ -f "$key" ]; then
  if ! "$install_root/on-wlan-up.sh" --auth-key="file:$key" --hostname=kobo-clara-bw > "$install_root/state/enroll.log" 2>&1; then
    status 'Enrollment did not complete. The key is retained only in private system storage.'
    exit 1
  fi
  rm -f "$key"
else
  if ! "$install_root/on-wlan-up.sh" > "$install_root/state/enroll.log" 2>&1; then
    status 'Reconnect USB to provide a fresh restricted enrollment key.'
    exit 1
  fi
fi

book_ip=$(sed -n '1p' "$onboard/books-host")
book_name=$(sed -n '2p' "$onboard/books-host")
printf '%s\n' "$book_ip" | awk -F. 'NF == 4 && $1 == 100 && $2 >= 64 && $2 <= 127 && $3 ~ /^[0-9]+$/ && $3 <= 255 && $4 ~ /^[0-9]+$/ && $4 <= 255 {ok=1} END {exit !ok}'
case "$book_name" in
  ''|*[!a-z0-9.-]*) status 'Invalid private Books hostname.'; exit 1 ;;
esac
case "$book_name" in
  books.*.ts.net) ;;
  *) status 'Unexpected private Books hostname.'; exit 1 ;;
esac
awk 'index($0, "# self-hosted-books") == 0' /etc/hosts > "$install_root/state/hosts.new"
printf '%s %s # self-hosted-books\n' "$book_ip" "$book_name" >> "$install_root/state/hosts.new"
cat "$install_root/state/hosts.new" > /etc/hosts
rm -f "$install_root/state/hosts.new"
if wget -q -T 15 -O /dev/null "https://$book_name/login" > "$onboard/books-check.log" 2>&1; then
  status 'Tailscale connected. Private Books HTTPS is reachable.'
else
  status 'Tailscale connected; the private Books HTTPS check needs investigation.'
fi
/usr/local/tailscale/bin/tailscale ip -4 > "$onboard/tailscale-ip.log"
