package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

const (
	accountPath   = "/var/run/secrets/kubernetes.io/serviceaccount"
	inventoryPath = "/api/v1/namespaces/friends-vpn/secrets/friends-vpn-peers"
	statusPath    = "/api/v1/namespaces/friends-vpn/configmaps/friends-vpn-active"
)

var errConflict = errors.New("the VPN inventory changed concurrently")

type kubeStore struct {
	baseURL string
	token   func() (string, error)
	client  *http.Client
}

func inClusterStore() (*kubeStore, error) {
	host, port := os.Getenv("KUBERNETES_SERVICE_HOST"), os.Getenv("KUBERNETES_SERVICE_PORT")
	if host == "" || port == "" {
		return nil, errors.New("missing Kubernetes service address")
	}
	ca, err := os.ReadFile(accountPath + "/ca.crt")
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return nil, errors.New("invalid Kubernetes CA")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	return &kubeStore{
		baseURL: "https://" + net.JoinHostPort(host, port),
		token: func() (string, error) {
			data, err := os.ReadFile(accountPath + "/token")
			return strings.TrimSpace(string(data)), err
		},
		client: &http.Client{Timeout: 5 * time.Second, Transport: transport,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
	}, nil
}

func (k *kubeStore) call(ctx context.Context, method, path string, body any, output any) error {
	var payload []byte
	if body != nil {
		var err error
		payload, err = json.Marshal(body)
		if err != nil {
			return err
		}
	}
	token, err := k.token()
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, method, k.baseURL+path, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+token)
	if method == http.MethodPatch {
		request.Header.Set("Content-Type", "application/merge-patch+json")
	}
	response, err := k.client.Do(request)
	if err != nil {
		return fmt.Errorf("Kubernetes API request failed: %w", err)
	}
	defer response.Body.Close()
	stream := io.LimitReader(response.Body, 2<<20)
	if response.StatusCode == http.StatusOK && output != nil {
		return json.NewDecoder(stream).Decode(output)
	}
	io.Copy(io.Discard, stream)
	switch response.StatusCode {
	case http.StatusOK:
		return nil
	case http.StatusConflict:
		return errConflict
	default:
		return fmt.Errorf("Kubernetes API returned HTTP %d for %s %s", response.StatusCode, method, path)
	}
}

func (k *kubeStore) read(ctx context.Context) (inventory, string, error) {
	var secret struct {
		Metadata struct {
			ResourceVersion string `json:"resourceVersion"`
		} `json:"metadata"`
		Data map[string][]byte `json:"data"`
	}
	var current inventory
	if err := k.call(ctx, http.MethodGet, inventoryPath, nil, &secret); err != nil {
		return current, "", err
	}
	if err := json.Unmarshal(secret.Data["inventory.json"], &current); err != nil || secret.Metadata.ResourceVersion == "" {
		return current, "", errors.New("invalid inventory Secret")
	}
	return current, secret.Metadata.ResourceVersion, current.validate()
}

func (k *kubeStore) update(ctx context.Context, current inventory, version string) error {
	current.Revision = randRevision()
	if err := current.validate(); err != nil {
		return err
	}
	data, err := json.Marshal(current)
	if err != nil {
		return err
	}
	return k.call(ctx, http.MethodPatch, inventoryPath, map[string]any{
		"metadata": map[string]string{"resourceVersion": version},
		"data":     map[string][]byte{"inventory.json": data},
	}, nil)
}

func (k *kubeStore) active(ctx context.Context) (string, error) {
	var configMap struct {
		Data map[string]string `json:"data"`
	}
	err := k.call(ctx, http.MethodGet, statusPath, nil, &configMap)
	return configMap.Data["revision"], err
}

func (k *kubeStore) acknowledge(ctx context.Context, revision string) error {
	return k.call(ctx, http.MethodPatch, statusPath, map[string]any{
		"data": map[string]string{"revision": revision},
	}, nil)
}
