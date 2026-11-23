# Example systemd units

Example units for the long-running Tessera services. Every path here is a
generic placeholder: `/etc/tessera` (configuration), `/var/lib/tessera` (state),
`/opt/tessera/.venv` (virtualenv with Tessera installed) and a dedicated
`tessera` user. Adjust them for your host. Nothing here contains a secret.

| Unit | Runs |
| --- | --- |
| `tessera-development-poll.service` | `python -m tessera_worker.cli development-poll` (same as `tessera-worker development-poll`) |
| `tessera-salad-executor.service` | `python -m tessera_control.campaign_salad_executor` |
| `tessera-salad-guardian.service` | `python -m tessera_control.salad_safety_service --run` |
| `tessera-submit-runner.service` | `tessera-submit-runner`: submits owner-approved campaign records on a schedule (in the target, started only on an admitted host) |
| `tessera.target` | groups the four services |
| `tessera-health-check@.service` + `tessera-health-check-*.timer` | optional freshness probes |

The executor and guardian are also installed as the console scripts
`tessera-salad-executor` and `tessera-salad-guardian` (the guardian still needs
`--run`); the units use `python -m` so they pin the virtualenv interpreter. macOS
hosts can use the launchd templates in `deploy/launchd/` (the poller has
`deploy/worker/dev.tessera.worker.plist`). The services do not read these paths themselves: each
`--config` file is a JSON settings file that you write (see
`docs/campaign-providers.md`, `docs/salad-safety-service.md` and
`docs/worker-deployment.md` for the fields), and credentials are separate
owner-only files that the config names by path.

## Spending money

The Salad units spend money only when you supply Salad profiles, credentials
and an approved campaign. Without them the executor refuses setup (exit 2) and
the guardian prints an `unavailable` state; neither is restarted on that exit
code. Review the spend-safety documents before enabling them. The guardian
(`--run` is required; without it the program prints `disabled` and exits 0)
should be running before the executor.

## Install

1. Create a dedicated user: `useradd --system --home-dir /var/lib/tessera tessera`.
   The poller drives the host Docker daemon (root-equivalent); the unit adds the
   `docker` group, so keep the user dedicated.
2. Install Tessera into `/opt/tessera/.venv` and copy `tessera-health-check`
   to `/opt/tessera/deploy/systemd/` if you use the probes.
3. Create `/etc/tessera/*.json` configs and credential files, owner `tessera`,
   mode `0600` (the services refuse group/world-readable private files).
   Optional `/etc/tessera/<name>.env` files (`development-poll`,
   `salad-executor`, `salad-guardian`; also `0600`) may hold non-secret
   environment; the leading `-` in `EnvironmentFile=-` makes them optional.
   Prefer `LoadCredential=` over environment variables for anything sensitive.
4. Copy the `.service`, `.timer` and `.target` files to `/etc/systemd/system/`,
   then `systemctl daemon-reload`.
5. `systemctl enable --now tessera.target`. To run only part of it, start the
   individual services instead.

## Health and restarts

`development-poll` and the executor accept `--health-file`; the units write it
to `/var/lib/tessera/health/<name>.json` (schema `tessera.service-health.v1`,
fields `state`, `last_loop_at`, `loops`, `consecutive_errors`). The file is
written between steps, never during one, so a long Salad route step leaves it
stale. The probe is therefore report-only: `tessera-health-check FILE MAX_AGE`
exits 1 when the state is not `running` or the file is older than `MAX_AGE`
(the unit uses 3600 seconds; set it above your longest step plus one poll
interval). A failed probe appears in `systemctl --failed`; it never restarts a
service. Enable with
`systemctl enable --now tessera-health-check-development-poll.timer
tessera-health-check-salad-executor.timer`.

The guardian has no health file. Check it through its own authenticated
endpoint as described in `docs/salad-safety-service.md`; `Restart=on-failure`
covers crashes. Exit code 2 (configuration or prerequisite refused) is not
retried (`RestartPreventExitStatus=2`).

## Submit runner (starts with the target, only if admitted)

`tessera-submit-runner.service` runs the campaign-submit action of the agent
loop ([ADR 0093](../../docs/decisions/0093-agent-loop-submits-owner-approved-campaigns.md))
on a schedule. It only turns records a person already approved into campaigns;
it runs no model and never approves anything. It is wanted by
`tessera.target`, so enabling the target starts it, but only on a host whose
operator created the admission marker below (without it systemd skips the
start). Its settings:

- Admission: it starts only if the marker `/etc/tessera/submit-runner.admitted`
  exists (`ConditionPathExists=`; without it systemd skips the start).
- Credential: the agent token file is passed with `LoadCredential=` and the unit
  gives the program only its path (`TESSERA_AGENT_TOKEN_FILE`). The other
  `Environment=` lines (`TESSERA_CONTROL_URL`, `TESSERA_PROJECT_ID`,
  `TESSERA_AGENT_DESTINATION`) are non-secret references; edit them for your
  host. The program refuses to start if `ANTHROPIC_API_KEY` is set.
- Writes: only `/var/lib/tessera/submit-runner` (the append-only journal of
  receipts). `StateDirectory=tessera/submit-runner` makes systemd create that
  directory (mode 0700) and nothing else under `/var/lib/tessera` is writable.
  The kill-switch marker `/etc/tessera/submit-runner.kill` is only read, so
  `/etc/tessera` must exist.
- Exit 0 means stopped, 2 a configuration refusal and 3 halted; the unit does
  not restart on 2 or 3.

Control must also run with `tessera serve --agent-submissions`; otherwise every
submit is refused `feature_disabled`.

Admit and start: `touch /etc/tessera/submit-runner.admitted`, then
`systemctl start tessera-submit-runner.service` (or restart the target; after
that it starts with the target). Control is started with
`tessera serve --agent-submissions --agent-submission-kill-switch /etc/tessera/submit-runner.kill`.

Stop new submissions at once: `touch /etc/tessera/submit-runner.kill`. While
the marker exists (or its state is ambiguous, for example its parent directory
is missing) the runner pauses without exiting and calls neither the action nor
the control plane; it resumes by itself when the marker is removed. It exits 0
only on a stop request (SIGTERM) and halts with exit 3 after repeated failed
ticks.
Campaigns that were already submitted keep running and are untouched. To stop
the process: `systemctl stop tessera-submit-runner.service`; to withdraw
admission, remove the marker file.

## Stage watcher (on in the poller)

`tessera-development-poll.service` starts the poller with `--stage-watcher
--stage-watcher-kill-switch /var/lib/tessera/stage-watch.kill`
([ADR 0111](../../docs/decisions/0111-stage-watcher-decision-requests-and-remote-wiring.md)).
The watcher only observes and reports (it never stops, retries or approves). It
runs while the kill-switch marker is absent; `touch /var/lib/tessera/stage-watch.kill`
turns it off at once, and a poller started without a marker path refuses to run
the watcher (it fails closed). Remove both flags from `ExecStart` to disable it.

## Credential health

`GET /credential-health` and the `credentials` section of `/operator/health` report
`unknown` until control is started with `tessera serve --salad-credential-directory DIR`
(the absolute, owner-only directory that holds the Salad management credentials; it
reads their metadata, never the secret). Add the flag to the `serve` command you run.
