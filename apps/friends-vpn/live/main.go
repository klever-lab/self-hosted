package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func randRevision() string {
	var revision [16]byte
	rand.Read(revision[:])
	return hex.EncodeToString(revision[:])
}

func main() {
	if len(os.Args) != 2 {
		log.Fatal("expected gateway, gateway-ready, portal, or portal-ready")
	}
	mode := os.Args[1]
	if mode == "gateway-ready" {
		if _, err := os.Stat(gatewayReady); err != nil {
			os.Exit(1)
		}
		return
	}
	if mode == "portal-ready" {
		client := &http.Client{Timeout: time.Second}
		response, err := client.Get("http://127.0.0.1:8080/healthz")
		if err != nil {
			os.Exit(1)
		}
		response.Body.Close()
		if response.StatusCode != http.StatusNoContent {
			os.Exit(1)
		}
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	store, err := inClusterStore()
	if err != nil {
		log.Fatal(err)
	}
	switch mode {
	case "gateway":
		err = runGateway(ctx, store)
	case "portal":
		err = runPortal(ctx, store)
	default:
		log.Fatal("unknown Friends VPN process")
	}
	if err != nil {
		log.Fatal(err)
	}
}
