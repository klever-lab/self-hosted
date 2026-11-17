package main

import (
	"context"
	"crypto/ecdh"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"io"
	"net"
	"net/netip"
	"testing"
	"time"

	box "github.com/sagernet/sing-box"
	"github.com/sagernet/sing-box/protocol/wireguard"
	M "github.com/sagernet/sing/common/metadata"
)

func testKey(t *testing.T) (private, public string) {
	t.Helper()
	key, err := ecdh.X25519().GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(key.Bytes()), base64.StdEncoding.EncodeToString(key.PublicKey().Bytes())
}

func testBox(t *testing.T, config string) *box.Box {
	t.Helper()
	instance, err := newBox(t.Context(), []byte(config))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { instance.Close() })
	if err := instance.Start(); err != nil {
		t.Fatal(err)
	}
	return instance
}

func TestLiveWireGuardPeerChangesKeepTCPConnection(t *testing.T) {
	echo, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { echo.Close() })
	go func() {
		for {
			connection, err := echo.Accept()
			if err != nil {
				return
			}
			go func() {
				defer connection.Close()
				io.Copy(connection, connection)
			}()
		}
	}()
	udp, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := udp.LocalAddr().(*net.UDPAddr).Port
	udp.Close()
	private, public := testKey(t)
	server := testBox(t, fmt.Sprintf(`{
		"log":{"level":"error"},
		"endpoints":[{"type":"wireguard","tag":"friends","system":false,
			"address":["10.76.0.1/32","fd76:6672:6965::1/128"],"private_key":%q,"listen_port":%d}],
		"outbounds":[{"type":"direct","tag":"echo"}],
		"route":{"rules":[{"ip_cidr":["192.0.2.1/32"],"action":"route","outbound":"echo",
			"override_address":"127.0.0.1","override_port":%d}],"final":"echo"}
	}`, private, port, echo.Addr().(*net.TCPAddr).Port))
	endpoint, _ := server.Endpoint().Get("friends")
	updater, ok := endpoint.(wireGuardPeers)
	if !ok {
		t.Fatal("build.sh must apply the live WireGuard peer patch")
	}
	apply := func(previous, next map[string]peer) {
		t.Helper()
		if err := updater.ApplyPeerChanges(peerChanges(previous, next)); err != nil {
			t.Fatal(err)
		}
	}
	connect := func(key string, p peer) net.Conn {
		t.Helper()
		client := testBox(t, fmt.Sprintf(`{
			"log":{"level":"error"},
			"endpoints":[{"type":"wireguard","tag":"client","system":false,
				"address":[%q],"private_key":%q,"peers":[{"address":"127.0.0.1","port":%d,
				"public_key":%q,"pre_shared_key":%q,"allowed_ips":["0.0.0.0/0"]}]}]
		}`, p.Addresses[0], key, port, public, p.PresharedKey))
		endpoint, _ := client.Endpoint().Get("client")
		ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
		defer cancel()
		connection, err := endpoint.(*wireguard.Endpoint).DialContext(ctx, "tcp", M.Socksaddr{
			Addr: netip.MustParseAddr("192.0.2.1"), Port: 443,
		})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { connection.Close() })
		return connection
	}
	exchange := func(connection net.Conn, value string) {
		t.Helper()
		connection.SetDeadline(time.Now().Add(3 * time.Second))
		if _, err := io.WriteString(connection, value); err != nil {
			t.Fatal(err)
		}
		response := make([]byte, len(value))
		if _, err := io.ReadFull(connection, response); err != nil || string(response) != value {
			t.Fatalf("TCP connection did not survive peer update: %v", err)
		}
	}
	firstPrivate, firstPublic := testKey(t)
	secondPrivate, secondPublic := testKey(t)
	first := peer{PublicKey: firstPublic, PresharedKey: newPresharedKey(), Addresses: clientAddresses(2)}
	second := peer{PublicKey: secondPublic, PresharedKey: newPresharedKey(), Addresses: clientAddresses(3)}
	original := map[string]peer{"first": first}
	expanded := map[string]peer{"first": first, "second": second}
	apply(nil, original)
	keep := connect(firstPrivate, first)
	exchange(keep, "before enrollment")
	apply(original, expanded)
	drop := connect(secondPrivate, second)
	exchange(drop, "second peer")
	exchange(keep, "after enrollment")
	apply(expanded, original)
	exchange(keep, "after revocation")
	drop.SetDeadline(time.Now().Add(500 * time.Millisecond))
	io.WriteString(drop, "revoked peer")
	if _, err := drop.Read(make([]byte, 16)); err == nil {
		t.Fatal("revoked peer still passes traffic")
	}
}
