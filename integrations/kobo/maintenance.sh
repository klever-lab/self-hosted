#!/bin/sh
# The reviewed tailnet policy must restrict reader SSH to the operator laptop.
set -eu
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
tailscale=/usr/local/tailscale/bin/tailscale

case "${1:-}" in
  enable)
    if timeout 5 "$tailscale" set --ssh=true --shields-up=false 2>&1; then
      printf '\nLaptop access enabled over Tailscale.\nKeep Wi-Fi connected for remote updates.\n'
    else
      timeout 2 "$tailscale" set --ssh=false --shields-up=true >/dev/null 2>&1 || true
      printf '\nCould not enable laptop access. Check Tailscale status before retrying.\n'
    fi
    ;;
  disable)
    if timeout 5 "$tailscale" set --ssh=false --shields-up=true 2>&1; then
      printf '\nLaptop access disabled. Private library access is unchanged.\n'
    else
      printf '\nCould not disable laptop access. Incoming access may still be enabled.\n'
    fi
    ;;
  *) printf 'Choose enable or disable.\n'; exit 1 ;;
esac
