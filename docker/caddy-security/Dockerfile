# syntax=docker/dockerfile:1
# Intentional infrastructure security patch, separate from the six app images.
# Supply a verified official golang:1.26.8-alpine digest; the build rejects tags alone.
ARG GO_IMAGE=golang:1.26.8-alpine@sha256:ce864e7223ac17b1775e6fd0b4c0db580c2eb50e7953a427916379e4b92a1628
ARG CADDY_IMAGE=caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

FROM --platform=$BUILDPLATFORM ${GO_IMAGE} AS builder
ARG GO_IMAGE
ARG TARGETOS
ARG TARGETARCH
ENV GOTOOLCHAIN=local CGO_ENABLED=0 GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org
WORKDIR /build
RUN set -eu; \
    case "$GO_IMAGE" in golang:1.26.8-alpine*@sha256:*) ;; *) echo 'pinned official Go 1.26.8 builder required' >&2; exit 1 ;; esac; \
    test "$(go env GOVERSION)" = go1.26.8; \
    test "$TARGETOS/$TARGETARCH" = linux/amd64; \
    go mod init map.local/caddy-security; \
    go mod download -json github.com/caddyserver/caddy/v2@v2.11.4 > /tmp/caddy-source.json; \
    cp "$(go env GOMODCACHE)/github.com/caddyserver/caddy/v2@v2.11.4/cmd/caddy/main.go" ./main.go; \
    go get github.com/caddyserver/caddy/v2@v2.11.4 \
        golang.org/x/crypto@v0.55.0 golang.org/x/net@v0.57.0 \
        golang.org/x/text@v0.41.0 google.golang.org/grpc@v1.83.1; \
    go mod tidy; \
    go mod verify; \
    test "$(go list -m -f '{{.Version}}' github.com/caddyserver/caddy/v2)" = v2.11.4; \
    mkdir /out; \
    GOOS="$TARGETOS" GOARCH="$TARGETARCH" go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/caddy .; \
    go version -m /out/caddy > /out/buildinfo.txt; \
    go list -m all > /out/modules.txt; \
    cp go.mod go.sum main.go /out/; \
    cp /tmp/caddy-source.json /out/source-module.json; \
    printf '%s\n' "$GO_IMAGE" > /out/builder-image.txt; \
    sha256sum /out/caddy > /out/binary.sha256

FROM ${CADDY_IMAGE}
ARG CADDY_IMAGE
ARG GO_IMAGE
LABEL org.opencontainers.image.title="Map Caddy security rebuild" \
      org.opencontainers.image.version="2.11.4-map-security-20260906" \
      org.opencontainers.image.source="https://github.com/we-meet-trip/map-service-infra" \
      org.opencontainers.image.description="Caddy v2.11.4 standard modules, rebuilt with Go 1.26.8 and explicit security dependency updates" \
      io.map.caddy.upstream-version="v2.11.4" \
      io.map.caddy.go-version="go1.26.8" \
      io.map.caddy.builder-image="${GO_IMAGE}" \
      io.map.caddy.runtime-base="${CADDY_IMAGE}"
# Preserve official entrypoint/CMD, paths, trust store and capability behavior.
# Updating APK packages alone does NOT replace the Go standard library in Caddy.
RUN set -eu; \
    test "$(apk --print-arch)" = x86_64; \
    apk upgrade --no-cache; \
    mkdir -p /usr/share/map-caddy-build; \
    apk info -v | sort > /usr/share/map-caddy-build/apk-packages.txt
COPY --from=builder /out/caddy /usr/bin/caddy
COPY --from=builder /out/*.txt /out/*.json /out/*.sha256 /out/go.mod /out/go.sum /out/main.go /usr/share/map-caddy-build/
RUN set -eu; \
    chmod 755 /usr/bin/caddy; \
    setcap cap_net_bind_service=+ep /usr/bin/caddy; \
    caddy version | grep '^v2\.11\.4 '; \
    caddy list-modules --packages --versions > /usr/share/map-caddy-build/caddy-modules.txt; \
    sha256sum /usr/bin/caddy > /usr/share/map-caddy-build/installed-binary.sha256
