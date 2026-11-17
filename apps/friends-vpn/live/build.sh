#!/bin/sh
set -eu

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
go mod download github.com/sagernet/sing-box
source=$(go list -mod=readonly -m -f '{{.Dir}}' github.com/sagernet/sing-box)
cp -R "$source" "$work/sing-box"
chmod -R u+w "$work/sing-box"
patch -p1 -d "$work/sing-box" < sing-box.patch
cp go.mod go.sum "$work/"
go mod edit -modfile "$work/go.mod" -replace "github.com/sagernet/sing-box=$work/sing-box"
export CGO_ENABLED=0
tags=with_gvisor,badlinkname,tfogo_checklinkname0
go test -mod=readonly -modfile "$work/go.mod" -tags "$tags" ./...
if [ "$#" = 1 ]; then
    go build -mod=readonly -modfile "$work/go.mod" -tags "$tags" -trimpath -ldflags '-s -w' -o "$1" .
fi
