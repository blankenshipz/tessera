# The builder is pinned and used only at image build time.  The runtime image is
# scratch: it has no package manager, shell, network client, provider, or credentials.
FROM golang:1.13@sha256:6a693fbaba7dd8d816f6afce049fb92b280c588e0a677c4c8db26645e613fc15 AS build
WORKDIR /src
COPY main.go .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags='-s -w -buildid=' -o /hello main.go

FROM scratch
ARG TESSERA_E2E_NONCE
LABEL io.tessera.e2e-nonce=$TESSERA_E2E_NONCE
COPY --from=build /hello /hello
USER 65532:65532
ENTRYPOINT ["/hello"]
