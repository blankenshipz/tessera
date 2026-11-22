# Supervised development worker

Examples for running a Tessera development worker unattended. A small supervisor,
`tessera_worker.daemon` (console script `tessera-worker-daemon`), runs one worker
subcommand as a child and restarts it with backoff. A service manager (systemd or
launchd) keeps the supervisor itself running. All paths below are placeholders;
replace them for the host. The operator guide is
[docs/worker-deployment.md](../../docs/worker-deployment.md).

| File | Purpose |
| --- | --- |
| `tessera-worker.service` | systemd unit (Linux) |
| `dev.tessera.worker.plist` | launchd daemon (macOS) |
| `worker.env.example` | environment file read by the systemd unit |

## What runs

The supervisor runs `python -m tessera_worker.cli <subcommand> ...` in its own
process group. It only accepts the subcommands in its allowlist
(`development-run`, `development-poll`) and refuses anything else with exit 64.
It never starts, stops or removes Docker containers. If the worker is killed while
a run is active, the worker's own recovery (durable journal, exact-identity
reconciliation) handles it when it starts again.

Choosing the subcommand:

- `development-run --grant-id ID ...` runs one already issued grant and exits.
  Under a supervisor it is re-run after each exit, so it suits only a deliberate
  single-grant setup; prefer starting it by hand.
- `development-poll` loops, listing pending grants for one worker credential and
  running them. It is provided by the grant poller. The unit files here need
  no change when it is available: the supervisor passes the worker arguments through.

## Linux (systemd)

1. Install the wheel into a dedicated virtual environment, for example
   `/opt/tessera/venv`.
2. Create a dedicated user, for example `useradd --system --home-dir /var/lib/tessera-worker tessera`,
   and allow it to use Docker (see Security).
3. Create the worker credential file (below), `/etc/tessera/worker-credential`,
   owned by `tessera`, mode 0600.
4. Copy `worker.env.example` to `/etc/tessera/worker.env` (root:tessera, 0640) and
   edit the control URL, roots and pinned image. The roots must exist, be owned by
   `tessera` and be private (0700).
5. Copy `tessera-worker.service` to `/etc/systemd/system/`, then
   `systemctl daemon-reload && systemctl enable --now tessera-worker`.

The unit uses `Restart=always` with `StartLimitIntervalSec=0`, so systemd never
gives up; the supervisor owns the backoff. `KillMode=mixed` sends SIGTERM to the
supervisor only, which forwards it to the worker's process group and escalates to
SIGKILL after `--grace-seconds` (30). `TimeoutStopSec` (45) must stay above it.

## macOS (launchd)

1. Install the wheel as above and create a dedicated user and the directories used
   in the plist (state, workspace, output, `/var/log/tessera-worker`).
2. Create the credential file (below), mode 0600, owned by that user.
3. Edit `dev.tessera.worker.plist` (launchd does not expand variables), copy it to
   `/Library/LaunchDaemons/` owned by `root:wheel`, mode 0644, then
   `launchctl bootstrap system /Library/LaunchDaemons/dev.tessera.worker.plist`.

Docker Desktop exposes the host socket only while it is running for a logged-in
user, so the worker has no Docker to drive before that. The worker fails or idles,
the supervisor backs off, and work resumes once Docker is available.

## Worker credential file

Worker credentials are scoped, expiring identities issued by the control owner.
The issuing and registration steps are in
[Local API workflow](../../docs/local-api-workflow.md) (step 2), and renewal and
expiry in [Installation](../../docs/installation.md#credential-expiry-interruption-and-recovery).
Write the issued token to the file without echoing it, with the mode set first:

```sh
install -m 0600 -o tessera -g tessera /dev/null /etc/tessera/worker-credential
# then write the token into it from a private source; never put it in a unit,
# environment file or command line
```

## Boot ordering and control restarts

The unit orders itself after `network-online.target` and `docker.service`, but
neither guarantees that control is reachable. A control that is down or restarting
makes the child fail or idle; the supervisor backs off (2 s doubling to 120 s with
jitter, reset after a run of 60 s) and the worker resumes with no manual session
once control answers. Nothing needs to be started in a particular order.

## Authentication errors

When control refuses the worker credential (expired, revoked), the worker exits 3.
The supervisor then waits 60 s doubling to 900 s with jitter, never a tight loop,
and reports `credential_backoff`. Renew the same credential before it expires, or
enroll a fresh one, then replace the file contents. No restart is needed: the next
attempt reads the file again. Unattended renewal is not implemented.

Other exits: 0 restarts after 1 s; 2 (terminal evidence, an operator decision is
needed) and any other non-zero status or signal death use the ordinary backoff.

## Health check

```sh
python -m tessera_worker.daemon status --state-dir /var/lib/tessera-worker/daemon --max-age 120
```

Prints the status JSON with `fresh` and `age_seconds`; exits 0 only if the file was
updated within `--max-age` seconds and its state is not `stopped` or `failed`. The
status file holds only numbers, states and the subcommand name, never arguments,
paths, control URLs or credentials. Exit status: 0 healthy; 1 stale, `stopped`,
`failed` or no status file; 3 when the daemon is alive but waiting out a rejected
worker credential (`credential_backoff`), so a health check or script can tell a
degraded worker from a working one.

## Security notes

- The worker drives the host Docker daemon, which is root-equivalent. Anyone who can
  use the Docker socket can become root. Run the worker as a dedicated user that
  does nothing else.
- Never run the worker in a container with the Docker socket mounted.
- The credential file is mode 0600 and owned by the worker user. No secret goes in
  the unit file, plist, environment file or command line.
- The supervisor refuses a state directory that is a symlink, not owned by the
  user, or accessible to group/others.

## Why there is no Compose profile for the worker

The worker needs the host Docker socket. A Compose service would have to mount it,
which gives that container root-equivalent control of the host. Run the worker on the
host under a service manager instead. The control service, which needs no such
access, can use Compose; for restart after reboot or crash add to its service:

```yaml
services:
  control:
    restart: unless-stopped
```
