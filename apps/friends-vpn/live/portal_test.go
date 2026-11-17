package main

import (
	"bytes"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

func TestPortalChecksIdentityAndOnlyReturnsConfirmedProfile(t *testing.T) {
	_, serverPublic := testKey(t)
	_, clientPublic := testKey(t)
	current := inventory{Revision: "initial", Endpoint: "192.0.2.1:31820", ServerPublicKey: serverPublic,
		NextPeer: 2, Peers: map[string]peer{}, Retired: map[string]string{}, BlockedEmails: map[string]bool{}}
	active := current.Revision
	var lock sync.Mutex
	version := 1
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lock.Lock()
		defer lock.Unlock()
		if r.URL.Path == statusPath {
			json.NewEncoder(w).Encode(map[string]any{"data": map[string]string{"revision": active}})
			return
		}
		if r.URL.Path != inventoryPath {
			http.NotFound(w, r)
			return
		}
		if r.Method == http.MethodPatch {
			var patch struct {
				Metadata map[string]string
				Data     map[string][]byte
			}
			if json.NewDecoder(r.Body).Decode(&patch) != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			if patch.Metadata["resourceVersion"] != fmt.Sprint(version) {
				w.WriteHeader(http.StatusConflict)
				return
			}
			var updated inventory
			if json.Unmarshal(patch.Data["inventory.json"], &updated) != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			current = updated
			version++
			return
		}
		data, _ := json.Marshal(current)
		json.NewEncoder(w).Encode(map[string]any{
			"metadata": map[string]string{"resourceVersion": fmt.Sprint(version)},
			"data":     map[string][]byte{"inventory.json": data},
		})
	}))
	defer api.Close()
	store := &kubeStore{baseURL: api.URL, client: api.Client(), token: func() (string, error) { return "test", nil }}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key}, nil)
	if err != nil {
		t.Fatal(err)
	}
	const issuer = "https://test.cloudflareaccess.com"
	const audience = "friends-vpn-test"
	verifier := oidc.NewVerifier(issuer, &oidc.StaticKeySet{PublicKeys: []crypto.PublicKey{&key.PublicKey}},
		&oidc.Config{ClientID: audience, SupportedSigningAlgs: []string{oidc.RS256}})
	app := (&portal{store: store, verifier: verifier}).handler()
	assertion := func(email, aud string) string {
		t.Helper()
		token, err := jwt.Signed(signer).Claims(jwt.Claims{Issuer: issuer, Audience: jwt.Audience{aud}, Subject: email,
			Expiry: jwt.NewNumericDate(time.Now().Add(time.Hour))}).Claims(map[string]any{
			"type": "app", "email": email,
		}).Serialize()
		if err != nil {
			t.Fatal(err)
		}
		return token
	}
	call := func(method, path, token, origin string, value any, status int) string {
		t.Helper()
		var body bytes.Buffer
		if value != nil {
			json.NewEncoder(&body).Encode(value)
		}
		request := httptest.NewRequest(method, path, &body)
		request.Header.Set("Cf-Access-Jwt-Assertion", token)
		request.Header.Set("Cf-Access-Authenticated-User-Email", "one@example.com")
		request.Header.Set("Origin", origin)
		response := httptest.NewRecorder()
		app.ServeHTTP(response, request)
		if response.Code != status || response.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("%s %s got HTTP %d, expected %d", method, path, response.Code, status)
		}
		return response.Body.String()
	}
	const path = "/api/devices/7792a781-4912-4e16-b87f-6a07fa3ece55"
	first := assertion("one@example.com", audience)
	second := assertion("two@example.com", audience)
	input := enrollment{Name: "My phone", PublicKey: clientPublic}
	if page := call("GET", "/", first, "", nil, 200); !strings.Contains(page, `href="/favicon.png"`) {
		t.Fatal("the portal did not link to its favicon")
	}
	call("GET", "/favicon.png", "", "", nil, 401)
	if icon := call("GET", "/favicon.png", first, "", nil, 200); !strings.HasPrefix(icon, "\x89PNG\r\n\x1a\n") {
		t.Fatal("the favicon response was not a PNG")
	}
	call("GET", "/api/devices", "", "", nil, 401)
	call("GET", "/api/devices", assertion("one@example.com", "other-app"), "", nil, 401)
	call("PUT", path, first, "https://example.com", input, 403)
	call("PUT", path, first, portalOrigin, input, 202)
	call("PUT", path, first, portalOrigin, input, 202)
	call("PUT", path, second, portalOrigin, input, 409)
	call("DELETE", path, second, portalOrigin, nil, 404)
	if own := call("GET", "/api/devices", first, "", nil, 200); !strings.Contains(own, "My phone") {
		t.Fatal("the owner did not see their device")
	}
	if other := call("GET", "/api/devices", second, "", nil, 200); strings.Contains(other, "My phone") {
		t.Fatal("a different person saw the device")
	}
	ack := func(nextAddress int) {
		t.Helper()
		lock.Lock()
		defer lock.Unlock()
		if current.NextPeer != nextAddress {
			t.Fatalf("duplicate replay allocated another address: %d", current.NextPeer)
		}
		active = current.Revision
	}
	ack(3)
	ready := call("PUT", path, first, portalOrigin, input, 200)
	if !strings.Contains(ready, "10.76.0.2/32") || strings.Contains(ready, "private_key") {
		t.Fatal("activation returned an invalid import response")
	}
	call("DELETE", path, first, portalOrigin, nil, 202)
	ack(3)
	call("DELETE", path, first, portalOrigin, nil, 204)
	call("PUT", path, first, portalOrigin, input, 409)
}
