FROM golang:1.26.10-alpine@sha256:87c65a688975c0b1d4585d204874c45ca78354670e3a5f11368c5a492303d696 AS build
ENV GOTOOLCHAIN=local
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN sh build.sh /friends-vpn

FROM scratch
LABEL org.opencontainers.image.source="https://github.com/klever-lab/self-hosted"
LABEL org.opencontainers.image.licenses="GPL-3.0-or-later"
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /friends-vpn /friends-vpn
COPY LICENSE /LICENSE
COPY web/LICENSE.qrcode-generator /licenses/qrcode-generator
USER 1000:1000
ENTRYPOINT ["/friends-vpn"]
