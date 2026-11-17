#!/bin/sh
# Configure only this Pod's network, then exit before any application starts.
set -eu

vpn=$1
address=$(cat "$vpn/endpoint-address")
port=$(cat "$vpn/endpoint-port")
forwarded=$(cat "$vpn/forwarded-port")
gateway=$(ip -4 route get "$address" | awk '{for(i=1;i<=NF;i++) if($i=="via") {print $(i+1); exit}}')
test -n "$gateway"

incoming=''
if [ -n "$forwarded" ]; then
    incoming="iifname wg0 meta l4proto { tcp, udp } th dport $forwarded accept"
fi
# Replace only our table, atomically, before setting up the tunnel.
nft -f - <<EOF
add table inet qbittorrent_vpn
flush table inet qbittorrent_vpn
table inet qbittorrent_vpn {
    chain input {
        type filter hook input priority 0; policy drop;
        iifname lo accept
        meta nfproto ipv6 drop
        ct state established,related accept
        iifname eth0 tcp dport 8080 accept
        $incoming
    }
    chain output {
        type filter hook output priority 0; policy drop;
        oifname lo accept
        meta nfproto ipv6 drop
        oifname eth0 ip daddr $address udp dport $port accept
        oifname eth0 tcp sport 8080 ct direction reply ct state established accept
        oifname wg0 accept
    }
    chain forward { type filter hook forward priority 0; policy drop; }
}
EOF

# A retry replaces only our interface. No userspace VPN daemon remains running.
ip link del wg0 2>/dev/null || true
ip link add wg0 type wireguard
wg setconf wg0 "$vpn/wg.conf"
ip address add "$(cat "$vpn/address")" dev wg0
ip link set wg0 mtu 1120 up
ip route replace "$address/32" via "$gateway" dev eth0
ip route replace 10.42.0.0/16 via "$gateway" dev eth0
ip route replace default dev wg0

attempt=0
while [ "$attempt" -lt 30 ]; do
    attempt=$((attempt + 1))
    if wg show wg0 latest-handshakes | awk '$2 > 0 {ok=1} END {exit !ok}'; then
        exit 0
    fi
    sleep 1
done
echo 'WireGuard handshake did not complete; applications remain stopped.' >&2
exit 1
