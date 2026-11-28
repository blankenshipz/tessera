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
| `tessera-reasoning.service` | not here: it lives in `deploy/remote-agent-platform/` (manual start only, admission marker required, no `[Install]`) |
| `tessera-stuck-loop.service` | `tessera-stuck-loop`: offers stuck-campaign findings to a standing loop; reads findings only unless `ack_on_offer` is on (disabled by default, manual start, not in the target) |
| `tessera-research-review-runner.service` | `python -m tessera_control.research_review_runner`: runs queued research review jobs (plan, dataset and technique scouting, pre-gate review; never code review) through the model-spend ledger (manual start, admission marker required, not in the target) |
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
the guardian prints an `unavailable` state and exits 2 for a refused configuration;
neither is restarted on that exit code. Review the spend-safety documents before
enabling them. The guardian
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

`development-poll`, the executor and the guardian accept `--health-file`; the units write it
to `/var/lib/tessera/health/<name>.json` (schema `tessera.service-health.v1`,
fields `state`, `last_loop_at`, `loops`, `consecutive_errors`). The file is
written between steps, never during one, so a long Salad route step leaves it
stale. The probe is therefore report-only: `tessera-health-check FILE MAX_AGE`
exits 1 when the state is not `running` or the file is older than `MAX_AGE`
(the unit uses 3600 seconds; set it above your longest step plus one poll
interval). A failed probe appears in `systemctl --failed`; it never restarts a
service. Enable with
`systemctl enable --now tessera-health-check-development-poll.timer
tessera-health-check-salad-executor.timer tessera-health-check-salad-guardian.timer`.

The guardian writes its file from its serve loop at most every 5 seconds, so a
stale file means a stuck loop. Its authenticated endpoint (see
`docs/salad-safety-service.md`) remains the check of record; `Restart=on-failure`
covers crashes. Exit code 2 means the operator's own configuration was refused
and is not retried (`RestartPreventExitStatus=2`); every other fatal error
(including a JWKS fetch that failed at boot) exits 1 and is restarted.

## Submit runner (starts with the target, only if admitted)

`tessera-submit-runner.service` runs the campaign-submit action of the agent
loop ([ADR 0143](../../docs/decisions/0143-agent-loop-submits-owner-approved-campaigns.md))
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

## Stuck-loop runner (manual start)

`tessera-stuck-loop.service` runs a standing agent loop whose only work source
is the stuck-campaign findings
([ADR 0132](../../docs/decisions/0132-stuck-loop-runner.md)). It runs no model
and only reads findings unless the policy enables `ack_on_offer`, in which case it
also acknowledges an offered finding (a write that needs the `campaign:cancel`
scope). It offers at most one new open finding per tick and appends a
`stuck_offered` receipt for it to its journal (with `tick`, `backoff`, `halted`
and `runner_*` lines). Options: `--policy`, `--journal`, `--ledger`,
`--kill-switch` (required; without it the program exits 2), `--interval-seconds`,
`--once` and `--max-ticks`. It never resumes, cancels, approves, submits or changes settings.
It is not part of `tessera.target` and has no `[Install]` section, so
`systemctl enable` does nothing for it. Three gates keep it disabled by
default:

- Policy: `/etc/tessera/stuck-loop.json` (owner-only, strict JSON) must set
  `"enabled": true`; otherwise the program prints `disabled` and exits 0.
- Admission: it starts only if the marker `/etc/tessera/stuck-loop.admitted`
  exists (`ConditionPathExists=`).
- Kill switch: while `/etc/tessera/stuck-loop.kill` exists (or its state is
  ambiguous, for example `/etc/tessera` is missing) it pauses and offers
  nothing.

The credential file (`/etc/tessera/credentials/stuck-loop-agent-token`, passed
with `LoadCredential=`) must be enrolled with `campaign:read` only, plus
`campaign:cancel` only when the policy sets `ack_on_offer` to true (the
acknowledgement endpoint uses that scope; `ack_on_offer` defaults to false).
The program refuses to start if `ANTHROPIC_API_KEY` is set (exit 2); exit 3
means halted after repeated failures, and the unit restarts on neither. It
writes only `/var/lib/tessera/stuck-loop` (journal and ledger).

Admit and start: write the policy, `touch /etc/tessera/stuck-loop.admitted`,
then `systemctl start tessera-stuck-loop.service`. Pause at once:
`touch /etc/tessera/stuck-loop.kill`; remove it to resume. Withdraw: stop the
service and remove the admission marker. Raising a cap or approval stays a
human edit of the policy.

### Recovering from exit 3 or an uncertain offer

Stop the service first (recovery refuses with exit 2 while the loop holds the
ledger lock). Then run the same program with the same `--policy`, `--journal`,
`--ledger` and `--kill-switch`, plus one of:

- `--reset` clears the halt and the failure count after you have fixed the cause.
- `--resolve ITEM_ID --outcome succeeded` closes an uncertain offer for good
  (you checked that the acknowledgement landed); `--outcome not_started` makes it
  retryable once (you checked that it did not).

Each prints one line, exits 0, makes no call and needs no network, and appends
an `operator_reset` or `uncertain_resolved` line to the journal. The per-finding
ledger rows are kept, so an offered finding is never offered again. Do not
delete the ledger: that would forget what was already offered.

## Research review runner (manual start)

`tessera-research-review-runner.service` claims queued research review jobs of
the kinds `plan_review`, `dataset_scout`, `technique_scout` and
`pre_gate_review` (never `code_review`, which has its own runner), makes one
model call per job and records the findings. It runs on the control host as
the control service's user, because it reads each job's verified input content
from the control state directory. It is not part of `tessera.target` and has no
`[Install]` section. Gates:

- Admission: it starts only if `/etc/tessera/research-review-runner.admitted`
  exists (`ConditionPathExists=`).
- Kill switch: while `/etc/tessera/research-review-runner.kill` exists it claims
  nothing.
- Operator policy (`--policy module:attribute`, required) is re-checked at
  launch; prompts must be pinned: a job's `prompt_digest` must be the sha256 of
  a file in `--prompts-dir`, or the job is `not_started` (`prompt_not_pinned`).
- Spend: every call reserves the job's `budget_cents` in the model-spend ledger
  first, so the project's per-call and daily limits refuse it (no limit set is a
  refusal); the actual cost is settled with the receipt digest.

The agent credential (`/etc/tessera/credentials/research-review-runner-agent-token`)
must be enrolled with exactly `review:read`, `review:findings` and
`model_spend:record`, for a different person than the job submitters (or run
with `--review-single-owner` to match `tessera serve --review-single-owner`).
The model key file is named `anthropic-api-key` and is read only by the model
client. The program refuses to start (exit 2) without a policy, a valid model
profile, both credentials, or when an API-key environment variable is set. It
writes the control state (through the store) and
`/var/lib/tessera/research-review-runner` (journal and receipts).
