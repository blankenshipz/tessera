# Worker deployment

The development worker (`tessera-worker development-run`) is a one-shot process
that drives the host Docker CLI. To keep it running unattended, run it under the
worker supervisor, `tessera-worker-daemon` (`python -m tessera_worker.daemon`),
which is itself kept alive by systemd or launchd. Ready-to-edit unit files with
placeholder paths and the full install steps are in
[deploy/worker](../deploy/worker/README.md). Deployment does not enroll or register
a worker, issue grants, or enable dispatch; those remain owner actions described in
[Local API workflow](local-api-workflow.md) and [Worker lifecycle](worker-lifecycle.md).

## Supervisor

```sh
tessera-worker-daemon run --state-dir /var/lib/tessera-worker/daemon -- \
  development-poll --control-url https://control.example.invalid \
  --credential-file /etc/tessera/worker-credential ...
tessera-worker-daemon status --state-dir /var/lib/tessera-worker/daemon --max-age 120
```

Everything after `--` is passed to `python -m tessera_worker.cli`. The first word
must be in the supervisor's allowlist (`development-run`, `development-poll`);
anything else is refused with exit 64. The child runs in its own process group with
the inherited environment. Only one supervisor can use a state directory (a
`flock` on `daemon.lock`); the directory must be private (0700), owned by the user
and not a symlink.

| Child exit | Meaning | Supervisor action |
| --- | --- | --- |
| 0 | finished | restart after `--restart-delay` (1 s) |
| 3 | control refused the worker credential | `credential_backoff`: 60 s doubling to 900 s, with jitter |
| 2 | terminal evidence, operator decision needed | ordinary backoff |
| other non-zero, signal | unexpected failure | ordinary backoff: 2 s doubling to 120 s, with jitter |

A child that ran for at least `--stable-seconds` (60) resets the backoff. After a
failure two starts are never closer than the minimum, so a broken configuration
cannot crash-loop. SIGTERM or SIGINT stops respawning, sends SIGTERM to the child's
process group, waits `--grace-seconds` (30), then SIGKILLs the group, records state
`stopped` and exits 0. The supervisor never starts, stops or removes Docker
containers; the worker's own recovery handles an interrupted run on the next start.

`status.json` in the state directory (mode 0600, replaced atomically, refreshed at
every transition and at least every 10 s) records the supervisor pid, `state`
(`starting`, `running`, `backoff`, `credential_backoff`, `stopping`, `stopped`,
`failed`), child pid, restart and failure counts, last exit code and kind
(`ok`, `credential_rejected`, `terminal`, `failure`, `signal`), start times and the
subcommand name. It never holds arguments, paths, control URLs or credentials, and
the one-line stderr transition logs follow the same rule. `status` exits 0 when the
file is fresh and the state is not `stopped`, `failed` or `credential_backoff`; it
exits 3 when the file is fresh but the state is `credential_backoff` (alive, yet the
worker credential is being rejected), and 1 otherwise.

## Behaviour to expect

- **Boot ordering and control restarts:** a control that is not yet reachable makes
  the child fail or idle; the supervisor backs off and work resumes without a manual
  session.
- **Credential errors:** exit 3 produces long backoff and `credential_backoff`.
  Renew the same credential before expiry or enroll a fresh one and replace the
  0600 file ([expiry and renewal](installation.md#credential-expiry-interruption-and-recovery)).
  Unattended renewal is not implemented.
- **Choosing a subcommand:** `development-run` handles one grant and is not suited
  to a restarting service. `development-poll` loops over pending grants for one
  worker credential; it is provided by the grant poller, and the unit files need
  no change when it is available.

## Security

The worker drives the host Docker daemon, which is root-equivalent. Run it as a
dedicated user that is allowed to use Docker, never in a container with the Docker
socket mounted, with the credential in a 0600 file and no secrets in a unit, plist or
environment file. There is deliberately no Compose profile for the worker for the
same reason; the control service may use `restart: unless-stopped`.
