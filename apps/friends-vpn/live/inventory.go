package main

import (
	"crypto/ecdh"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"net/netip"
	"regexp"
	"strings"
	"unicode"
)

var (
	emailPattern = regexp.MustCompile(`^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z0-9\-]{2,63}$`)
	legacyID     = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)
	deviceID     = regexp.MustCompile(`^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$`)
)

type peer struct {
	Name         string    `json:"name"`
	Email        string    `json:"email,omitempty"`
	PublicKey    string    `json:"public_key"`
	PresharedKey string    `json:"preshared_key"`
	Addresses    [2]string `json:"addresses"`
	Replaces     string    `json:"replaces,omitempty"`
}

type inventory struct {
	Revision        string            `json:"revision"`
	Endpoint        string            `json:"endpoint"`
	ServerPublicKey string            `json:"server_public_key"`
	NextPeer        int               `json:"next_peer"`
	Peers           map[string]peer   `json:"peers"`
	Retired         map[string]string `json:"retired"`
	BlockedEmails   map[string]bool   `json:"blocked_emails"`
}

func normalizedEmail(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if len(value) > 254 || !emailPattern.MatchString(value) {
		return ""
	}
	return value
}

func validID(id string) bool { return legacyID.MatchString(id) || deviceID.MatchString(id) }

func validName(name string) bool {
	if name == "" || len([]rune(name)) > 40 || strings.TrimSpace(name) != name {
		return false
	}
	for _, r := range name {
		if !unicode.IsLetter(r) && !unicode.IsNumber(r) && !strings.ContainsRune(" -_.()", r) {
			return false
		}
	}
	return true
}

func wireGuardKey(value string) ([]byte, error) {
	decoded, err := base64.StdEncoding.DecodeString(value)
	if err != nil || len(decoded) != 32 || base64.StdEncoding.EncodeToString(decoded) != value {
		return nil, errors.New("invalid WireGuard key")
	}
	return decoded, nil
}

func validPublicKey(value string) bool {
	bytes, err := wireGuardKey(value)
	if err != nil {
		return false
	}
	curve := ecdh.X25519()
	key, err := curve.NewPublicKey(bytes)
	if err != nil {
		return false
	}
	// ECDH rejects low-order points. The fixed scalar is only a validation input.
	probe, err := curve.NewPrivateKey(make([]byte, 32))
	if err != nil {
		return false
	}
	_, err = probe.ECDH(key)
	return err == nil
}

func clientAddresses(number int) [2]string {
	return [2]string{fmt.Sprintf("10.76.0.%d/32", number), fmt.Sprintf("fd76:6672:6965::%x/128", number)}
}

func (i inventory) validate() error {
	endpoint, err := netip.ParseAddrPort(i.Endpoint)
	if err != nil || !endpoint.Addr().Is4() || !endpoint.Addr().IsGlobalUnicast() ||
		endpoint.Port() != 31820 || !validPublicKey(i.ServerPublicKey) ||
		i.Revision == "" || i.NextPeer < 2 || i.NextPeer > 255 ||
		i.Peers == nil || i.Retired == nil || i.BlockedEmails == nil {
		return errors.New("invalid VPN inventory")
	}
	addresses := make(map[[2]string]bool, i.NextPeer-2)
	for n := 2; n < i.NextPeer; n++ {
		addresses[clientAddresses(n)] = true
	}
	keys := map[string]bool{i.ServerPublicKey: true}
	for id, peer := range i.Peers {
		_, retired := i.Retired[id]
		if !validID(id) || !validName(peer.Name) || (peer.Email != "" && normalizedEmail(peer.Email) != peer.Email) ||
			retired || i.BlockedEmails[peer.Email] || !addresses[peer.Addresses] || keys[peer.PublicKey] ||
			!validPublicKey(peer.PublicKey) {
			return errors.New("invalid VPN peer inventory")
		}
		if _, err := wireGuardKey(peer.PresharedKey); err != nil {
			return err
		}
		delete(addresses, peer.Addresses)
		keys[peer.PublicKey] = true
	}
	return nil
}

func (i *inventory) retire(id string) {
	i.Retired[id] = i.Peers[id].Email
	delete(i.Peers, id)
}

func newPresharedKey() string {
	var value [32]byte
	rand.Read(value[:])
	return base64.StdEncoding.EncodeToString(value[:])
}
