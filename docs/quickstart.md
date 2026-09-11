# Local ledger quickstart

This is a local, source-only developer-alpha walkthrough. It creates a local
control ledger and records an API project; it does **not** dispatch work, contact
a provider, start OpenCode, attach Kubernetes, or establish a production setup.

## Prerequisites

Use Python 3.11 or newer and the committed lockfile. The only tested development
paths are the native locked Python workflow and Docker Compose described in
CONTRIBUTING.md; no production OS or deployment support matrix is declared.

```sh
uv sync --locked
uv run --locked tessera init --state-dir .tessera
uv run --locked tessera serve --state-dir .tessera --port 8765
```

In a second terminal, keep the owner header private and create one project:

```sh
credential_dir=$(mktemp -d)
cp .tessera/owner.header "$credential_dir/owner.header"
chmod 600 "$credential_dir/owner.header"
curl -H @"$credential_dir/owner.header" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: local-ledger-project' \
  -d '{"name":"Synthetic local ledger"}' http://127.0.0.1:8765/projects
rm "$credential_dir/owner.header"
rmdir "$credential_dir"
```

Stop the service with Ctrl-C. Back up before an explicit schema upgrade:

```sh
uv run --locked tessera backup --state-dir .tessera --output /safe/place/tessera-backup
uv run --locked tessera migrate --state-dir .tessera
```

Schema v35 is current. Startup refuses older schemas; older binaries refuse newer
state. Backup and held restore have important object-custody and original-service
limits—read [recovery](recovery.md) before relying on either command.

For a deliberately non-executable, synthetic public workflow narrative, see
[examples/local-ledger](../examples/local-ledger/README.md).
