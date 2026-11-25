# Deploy request

`tessera deploy-request` records that a gated release should be deployed and then deploys it by
itself once nothing blocks it, instead of waiting for a person to notice that a remote route
finished. The deploy is the [checked upgrade](deploy-upgrade.md), run once with the plan you
name. Design and limits: [ADR 0165](decisions/0165-deploy-requests-fire-when-quiescent.md).

Nothing runs by itself: you run `tick` (or `watch`) from a timer or a shell on the operator host.

## Commands

```
tessera deploy-request create --plan /srv/tessera/deploy-plan.json \
    --release 0123456789abcdef0123456789abcdef01234567 --requester alice \
    --gate-evidence /srv/tessera/gate/evidence.json [--main-ref main] [--ttl-hours 24]
tessera deploy-request list   --state-dir /srv/tessera/state
tessera deploy-request show   --state-dir /srv/tessera/state dr-<id>
tessera deploy-request cancel --state-dir /srv/tessera/state dr-<id> --by alice
tessera deploy-request tick   --state-dir /srv/tessera/state
tessera deploy-request watch  --state-dir /srv/tessera/state [--interval 60] [--max-seconds 3600]
```

- `create` takes the state directory from the plan. Give exactly one of `--gate-evidence` (an
  absolute file, hashed now and again at fire time) or `--gate-evidence-sha256` (a digest only,
  not rechecked). `--ttl-hours` is 1 to 168 (default 24). `--plan` must be an absolute path.
  Repeating the same release, plan and evidence returns the open request; anything else is refused
  while one is open.
- `tick` evaluates the open request once and prints `idle`, `busy`, `waiting` (with blockers),
  `fired`, `failed`, `refused` or `expired`. Exit status 0 for idle, busy, waiting and fired; 1 for
  failed, refused and expired; 2 for bad usage.
- `watch` ticks every `--interval` seconds (10 to 3600) until no request is open or it ends in an
  outcome other than waiting or busy; `--max-seconds` stops it earlier with status 0.
- `show` prints the request and its receipts. `list` prints up to 50, newest first. Both omit the
  host-local plan and gate evidence paths.
- `cancel` works on a `pending` request only; a `firing` one cannot be cancelled.

## Plan requirements

The plan is the [deploy-upgrade plan](deploy-upgrade.md) with three conditions, checked at
`create` and again at every tick:

- `source_revision` equals the requested release;
- `repo_root` is set, so the request can read the head of the main ref there;
- `state_dir` is the state directory the request lives in.

The plan file must also be unchanged since `create` (its sha256 is recorded).

## Statuses

| Status | Meaning |
| --- | --- |
| `pending` | Open; each tick re-checks it and may wait. |
| `firing` | The upgrade was started. A tick that finds this later ends the request `failed`. |
| `fired` | The upgrade finished with result `ok` (reason `deployed`). |
| `failed` | `fire_interrupted`, `upgrade_error` (the upgrade raised) or `upgrade_failed` (a step failed; see the deploy receipt). Never retried. |
| `refused` | An input changed; see the reasons below. |
| `expired` | Past `expires_at`. |
| `cancelled` | Cancelled with `cancel`. |

## Refusals and blockers

A refusal ends the request. A blocker only makes it wait, and is listed in `blockers`.

| Reason | Ends the request when |
| --- | --- |
| `expired` | the expiry passed |
| `main_moved` | the main ref no longer points at the release; make a new request with new gate evidence |
| `plan_changed` | the plan file's digest differs or it cannot be read |
| `plan_invalid` | the plan no longer loads |
| `release_mismatch` | the plan's `source_revision` or `state_dir` no longer match |
| `gate_evidence_changed` | the gate evidence file's digest differs or it cannot be read |
| `fire_interrupted` | a `firing` request was found by a later tick |
| `upgrade_error` / `upgrade_failed` | the upgrade raised, or a step failed |

| Blocker id | Waits while |
| --- | --- |
| `route.in_flight:<provider>:<status>` | campaign stage attempts of that provider are `granted`, `admitted`, `running`, `exited`, `collected` or `unknown` |
| `state.unreadable` | the control database cannot be read |
| `main.unreadable` | git cannot read the head of the main ref |
| `probe.error` | a blocker probe raised |
| `preflight:<check>` | the upgrade's own preflight check fails |

An attempt whose state is `unknown` blocks until it is resolved or the request expires.

## Receipts

Each request has owner-only receipts in `<state-dir>/deploy-requests/receipts/`, named
`<request-id>-<seq>-<event>-<digest>.json`. Events: `requested`, `waiting` (written when the
blocker set changes, not every tick), `fire`, `fired`, `fire_failed`, `refused`, `cancelled`. Each
carries `seq` and `previous_receipt_sha256`, the `receipt_sha256` of the one before, so a gap or
reordering is visible. The upgrade also writes its own deploy receipt, named in the `fired` or
`fire_failed` receipt.

## Reading requests

- `GET /admin/deploy-requests`: the newest requests with receipts and the ids of open ones.
  Read-only, same audience as `GET /admin/deploy-check`.
- MCP tool `tessera_deploy_requests`: the same, read-only.

Neither creates, evaluates or cancels anything.

## Running tick from a timer

Run it outside the services the upgrade restarts, as its own unit:

```
# /etc/systemd/system/tessera-deploy-request.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/tessera deploy-request tick --state-dir /srv/tessera/state

# /etc/systemd/system/tessera-deploy-request.timer
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
```

A tick that overlaps another is reported `busy` and changes nothing. Between the last check and the
upgrade stopping services a route can still start; the window is seconds, not zero.
