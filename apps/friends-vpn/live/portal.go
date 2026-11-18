package main

import (
	"context"
	"embed"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
)

const portalOrigin = "https://vpn.klever.lol"

//go:embed web/index.html web/favicon.png web/portal.css web/portal.js web/qrcode.mjs
var site embed.FS

type portal struct {
	store    *kubeStore
	verifier *oidc.IDTokenVerifier
}

func runPortal(ctx context.Context, store *kubeStore) error {
	issuer, audience := os.Getenv("ACCESS_ISSUER"), os.Getenv("ACCESS_AUDIENCE")
	if !regexp.MustCompile(`^https://[a-z0-9-]+\.cloudflareaccess\.com$`).MatchString(issuer) ||
		!regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(audience) {
		return errors.New("invalid Cloudflare Access issuer or application audience")
	}
	verifier := oidc.NewVerifier(issuer,
		oidc.NewRemoteKeySet(oidc.ClientContext(ctx, &http.Client{Timeout: 5 * time.Second}), issuer+"/cdn-cgi/access/certs"),
		&oidc.Config{ClientID: audience, SupportedSigningAlgs: []string{oidc.RS256}})
	server := &http.Server{
		Addr: "127.0.0.1:8080", Handler: (&portal{store, verifier}).handler(),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second,
		WriteTimeout: 20 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 32 << 10,
	}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		server.Shutdown(shutdown)
	}()
	if err := server.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

func (p *portal) authenticate(request *http.Request) string {
	assertion := request.Header.Get("Cf-Access-Jwt-Assertion")
	if assertion == "" {
		return ""
	}
	token, err := p.verifier.Verify(request.Context(), assertion)
	if err != nil || token.Subject == "" {
		return ""
	}
	var claims struct {
		Type  string `json:"type"`
		Email string `json:"email"`
	}
	if err := token.Claims(&claims); err != nil || claims.Type != "app" {
		return ""
	}
	return normalizedEmail(claims.Email)
}

func (p *portal) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusNoContent) })
	mux.HandleFunc("GET /{$}", p.asset("index.html", "text/html; charset=utf-8"))
	mux.HandleFunc("GET /favicon.png", p.asset("favicon.png", "image/png"))
	mux.HandleFunc("GET /portal.css", p.asset("portal.css", "text/css; charset=utf-8"))
	mux.HandleFunc("GET /portal.js", p.asset("portal.js", "text/javascript; charset=utf-8"))
	mux.HandleFunc("GET /qrcode.mjs", p.asset("qrcode.mjs", "text/javascript; charset=utf-8"))
	mux.HandleFunc("GET /api/devices", p.authorized(p.list))
	mux.HandleFunc("PUT /api/devices/{id}", p.authorized(p.enroll))
	mux.HandleFunc("DELETE /api/devices/{id}", p.authorized(p.revoke))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
		mux.ServeHTTP(w, r)
	})
}

func (p *portal) asset(name, contentType string) http.HandlerFunc {
	return p.authorized(func(w http.ResponseWriter, r *http.Request, email string) {
		data, err := site.ReadFile("web/" + name)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", contentType)
		w.Write(data)
	})
}

func (p *portal) authorized(next func(http.ResponseWriter, *http.Request, string)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead && r.Header.Get("Origin") != portalOrigin {
			apiError(w, http.StatusForbidden, "Open Friends VPN to manage devices.")
			return
		}
		email := p.authenticate(r)
		if email == "" {
			apiError(w, http.StatusUnauthorized, "Sign in to Friends VPN again.")
			return
		}
		next(w, r, email)
	}
}

func jsonResponse(w http.ResponseWriter, code int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(value)
}

func apiError(w http.ResponseWriter, code int, message string) {
	jsonResponse(w, code, map[string]string{"error": message})
}

func unavailable(w http.ResponseWriter, err error) {
	log.Print(err)
	apiError(w, http.StatusServiceUnavailable, "Friends VPN is temporarily unavailable. Try again in a moment.")
}

func (p *portal) inventory(w http.ResponseWriter, r *http.Request, email string) (inventory, string, bool) {
	current, version, err := p.store.read(r.Context())
	if err != nil {
		unavailable(w, err)
		return current, "", false
	}
	if current.BlockedEmails[email] {
		apiError(w, http.StatusForbidden, "This email no longer has access to Friends VPN.")
		return current, "", false
	}
	return current, version, true
}

type deviceSummary struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Active bool   `json:"active"`
}

func (p *portal) list(w http.ResponseWriter, r *http.Request, email string) {
	current, _, ok := p.inventory(w, r, email)
	if !ok {
		return
	}
	revision, err := p.store.active(r.Context())
	if err != nil {
		unavailable(w, err)
		return
	}
	devices := make([]deviceSummary, 0)
	for id, device := range current.Peers {
		if device.Email == email {
			devices = append(devices, deviceSummary{id, device.Name, revision == current.Revision})
		}
	}
	sort.Slice(devices, func(i, j int) bool {
		if devices[i].Name != devices[j].Name {
			return devices[i].Name < devices[j].Name
		}
		return devices[i].ID < devices[j].ID
	})
	jsonResponse(w, http.StatusOK, struct {
		Email   string          `json:"email"`
		Devices []deviceSummary `json:"devices"`
	}{email, devices})
}

type enrollment struct {
	Name      string `json:"name"`
	PublicKey string `json:"public_key"`
	Replaces  string `json:"replaces"`
}

func (p *portal) enroll(w http.ResponseWriter, r *http.Request, email string) {
	id := r.PathValue("id")
	if !deviceID.MatchString(id) {
		apiError(w, http.StatusBadRequest, "Invalid device ID.")
		return
	}
	var input enrollment
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2048))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil || decoder.Decode(new(any)) != io.EOF {
		apiError(w, http.StatusBadRequest, "Invalid device request.")
		return
	}
	input.Name = strings.TrimSpace(input.Name)
	if !validName(input.Name) || !validPublicKey(input.PublicKey) || (input.Replaces != "" && !validID(input.Replaces)) {
		apiError(w, http.StatusBadRequest, "Use a device name of up to 40 letters, numbers, spaces, hyphens, dots, underscores or parentheses.")
		return
	}
	for range 5 {
		current, version, ok := p.inventory(w, r, email)
		if !ok {
			return
		}
		if old, exists := current.Peers[id]; exists {
			if old.Email != email || old.PublicKey != input.PublicKey || old.Name != input.Name || old.Replaces != input.Replaces {
				apiError(w, http.StatusConflict, "This device request has already been used.")
				return
			}
			p.profile(w, r, current, old)
			return
		}
		if _, retired := current.Retired[id]; retired {
			apiError(w, http.StatusConflict, "This device request has expired. Create a new profile.")
			return
		}
		if input.Replaces != "" && current.Peers[input.Replaces].Email != email {
			apiError(w, http.StatusNotFound, "Device not found.")
			return
		}
		if current.NextPeer > 254 {
			apiError(w, http.StatusConflict, "Friends VPN has no remaining device addresses. Contact the operator.")
			return
		}
		if current.ServerPublicKey == input.PublicKey {
			apiError(w, http.StatusConflict, "This WireGuard key is already in use.")
			return
		}
		for _, existing := range current.Peers {
			if existing.PublicKey == input.PublicKey {
				apiError(w, http.StatusConflict, "This WireGuard key is already in use.")
				return
			}
		}
		if input.Replaces != "" {
			current.retire(input.Replaces)
		}
		current.Peers[id] = peer{input.Name, email, input.PublicKey, newPresharedKey(), clientAddresses(current.NextPeer), input.Replaces}
		current.NextPeer++
		if err := p.store.update(r.Context(), current, version); errors.Is(err, errConflict) {
			continue
		} else if err != nil {
			unavailable(w, err)
			return
		}
		w.WriteHeader(http.StatusAccepted)
		return
	}
	unavailable(w, errConflict)
}

func (p *portal) profile(w http.ResponseWriter, r *http.Request, current inventory, device peer) {
	revision, err := p.store.active(r.Context())
	if err != nil {
		unavailable(w, err)
		return
	}
	if revision != current.Revision {
		w.WriteHeader(http.StatusAccepted)
		return
	}
	jsonResponse(w, http.StatusOK, struct {
		ServerPublicKey string    `json:"server_public_key"`
		Endpoint        string    `json:"endpoint"`
		PresharedKey    string    `json:"preshared_key"`
		Addresses       [2]string `json:"addresses"`
	}{current.ServerPublicKey, current.Endpoint, device.PresharedKey, device.Addresses})
}

func (p *portal) revoke(w http.ResponseWriter, r *http.Request, email string) {
	id := r.PathValue("id")
	for range 5 {
		current, version, ok := p.inventory(w, r, email)
		if !ok {
			return
		}
		if current.Retired[id] == email {
			revision, err := p.store.active(r.Context())
			if err != nil {
				unavailable(w, err)
				return
			}
			if revision != current.Revision {
				w.WriteHeader(http.StatusAccepted)
				return
			}
			w.WriteHeader(http.StatusNoContent)
			return
		}
		if current.Peers[id].Email != email {
			apiError(w, http.StatusNotFound, "Device not found.")
			return
		}
		current.retire(id)
		if err := p.store.update(r.Context(), current, version); errors.Is(err, errConflict) {
			continue
		} else if err != nil {
			unavailable(w, err)
			return
		}
		w.WriteHeader(http.StatusAccepted)
		return
	}
	unavailable(w, errConflict)
}
