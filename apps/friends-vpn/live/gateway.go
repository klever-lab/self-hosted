package main

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	box "github.com/sagernet/sing-box"
	"github.com/sagernet/sing-box/adapter/certificate"
	"github.com/sagernet/sing-box/adapter/endpoint"
	"github.com/sagernet/sing-box/adapter/inbound"
	"github.com/sagernet/sing-box/adapter/outbound"
	"github.com/sagernet/sing-box/adapter/service"
	"github.com/sagernet/sing-box/dns"
	"github.com/sagernet/sing-box/dns/transport/local"
	"github.com/sagernet/sing-box/option"
	"github.com/sagernet/sing-box/protocol/direct"
	"github.com/sagernet/sing-box/protocol/wireguard"
	"github.com/sagernet/sing/common/json"
)

const (
	gatewayConfig = "/etc/sing-box/config.json"
	gatewayReady  = "/run/friends-vpn/ready"
)

type wireGuardPeers interface {
	ApplyPeerChanges(string) error
}

func newBox(ctx context.Context, config []byte) (*box.Box, error) {
	endpoints := endpoint.NewRegistry()
	wireguard.RegisterEndpoint(endpoints)
	outbounds := outbound.NewRegistry()
	direct.RegisterOutbound(outbounds)
	dnsTransports := dns.NewTransportRegistry()
	local.RegisterTransport(dnsTransports)
	ctx = box.Context(ctx, inbound.NewRegistry(), outbounds, endpoints,
		dnsTransports, service.NewRegistry(), certificate.NewRegistry())
	options, err := json.UnmarshalExtendedContext[option.Options](ctx, config)
	if err != nil {
		return nil, errors.New("invalid VPN configuration")
	}
	instance, err := box.New(box.Options{Context: ctx, Options: options})
	if err != nil {
		return nil, errors.New("could not initialize VPN configuration")
	}
	return instance, nil
}

func peerChanges(previous, desired map[string]peer) string {
	var changes strings.Builder
	for id, old := range previous {
		if current, exists := desired[id]; !exists || current.PublicKey != old.PublicKey {
			key, _ := wireGuardKey(old.PublicKey)
			fmt.Fprintf(&changes, "public_key=%s\nremove=true\n", hex.EncodeToString(key))
		}
	}
	for id, current := range desired {
		old := previous[id]
		if current.PublicKey == old.PublicKey && current.PresharedKey == old.PresharedKey && current.Addresses == old.Addresses {
			continue
		}
		key, _ := wireGuardKey(current.PublicKey)
		preshared, _ := wireGuardKey(current.PresharedKey)
		fmt.Fprintf(&changes, "public_key=%s\npreshared_key=%s\nreplace_allowed_ips=true\nallowed_ip=%s\nallowed_ip=%s\n",
			hex.EncodeToString(key), hex.EncodeToString(preshared), current.Addresses[0], current.Addresses[1])
	}
	return changes.String()
}

func runGateway(ctx context.Context, store *kubeStore) error {
	if err := os.Remove(gatewayReady); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	config, err := os.ReadFile(gatewayConfig)
	if err != nil {
		return err
	}
	instance, err := newBox(ctx, config)
	if err != nil {
		return err
	}
	defer instance.Close()
	friends, _ := instance.Endpoint().Get("friends")
	device, ok := friends.(wireGuardPeers)
	if !ok {
		return errors.New("the Friends WireGuard endpoint is missing live peer support")
	}
	if err := instance.Start(); err != nil {
		return errors.New("could not start WireGuard endpoints")
	}
	var applied inventory
	var acknowledged string
	ready := false
	reconcile := func() error {
		desired, _, err := store.read(ctx)
		if err != nil {
			return err
		}
		if desired.Revision != applied.Revision {
			if changes := peerChanges(applied.Peers, desired.Peers); changes != "" {
				if err := device.ApplyPeerChanges(changes); err != nil {
					return errors.New("could not apply WireGuard peer changes")
				}
			}
			applied = desired
		}
		if acknowledged != applied.Revision {
			if err := store.acknowledge(ctx, applied.Revision); err != nil {
				return err
			}
			acknowledged = applied.Revision
		}
		if !ready {
			if err := os.WriteFile(gatewayReady, nil, 0600); err != nil {
				return err
			}
			ready = true
		}
		return nil
	}

	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	var lastError string
	for ctx.Err() == nil {
		if err := reconcile(); err == nil {
			lastError = ""
		} else if ctx.Err() == nil && err.Error() != lastError {
			lastError = err.Error()
			log.Printf("peer reconciliation: %s", lastError)
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
		}
	}
	return nil
}
