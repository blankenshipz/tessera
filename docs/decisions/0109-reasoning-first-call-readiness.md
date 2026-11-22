# ADR 0109: first-call readiness for the reasoning service

## Status

Accepted and implemented. No schema change. Builds on [ADR 0082](0082-agent-loop-anthropic-destination.md)
(the Anthropic destination and key by reference), [ADR 0083](0083-model-call-receipts.md) (one receipt
per model call) and [ADR 0090](0090-model-spend-ledger.md) (the model-spend ledger).

## Context

The on-platform agent loop reaches the Anthropic Messages API through the reasoning service. Every
slice it needs is separate: the service itself, the credential reference, the spend limits that a person
sets, the model-runtime egress allowlist, a place to write receipts, and the control-plane routes that
settle each call's cost. An operator who installs the API key and starts the service finds a missing
piece only when the first call is refused, and the first call is the one a person is watching.

The key is the one thing an operator cannot check without exposing it: Tessera must never read it
before a call, print it or log it. Everything else can be checked ahead of time.

## Decision

1. **One read-only readiness command.** `tessera-reasoning-readiness check --profile FILE
   --credentials-directory DIR --state-dir DIR [--control-state-dir DIR] [--control-url URL]` checks
   everything the first receipted call needs except the key value, and prints one JSON report:
   `{overall, checks: [{name, status, detail, fix}], key: {present, size_ok}}`. `status` is `ok`,
   `missing`, `fail` or `unchecked`. `overall` is `ready`, `ready_except_key`, `not_fully_checked` or
   `not_ready`. Exit status is 0, 10, 11 or 1 respectively (2 for a usage error), so a script can tell
   "only the key is missing" from "something could not be verified" from "something else is wrong".

2. **The checks.**
   - `environment`: none of `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_API_KEY` is set
     (even empty).
   - `profile`: an Anthropic profile that start-up validation accepts, an admitted tier, operator-pinned
     prices and limits, and the `model_spend.project_id` the ledger needs.
   - `credential_reference`: the manager `model-credential` file equals the profile's `credential_ref`,
     with the same logic service start-up applies.
   - `api_key_file`: a stat-level check of the key file (present, regular, owned by the service's
     credential owner, mode 0600, 1 to 513 bytes: 512 for the key plus an optional trailing newline). The file is never opened or read. Its absence is the
     only gap that still reads `ready_except_key`.
   - `egress_allowlist`: the model-runtime allowlist contains exactly the Anthropic API host and port
     and nothing else.
   - `receipts_store`: the state directory is writable by the service user, is not group- or
     world-writable, and has free space; a transient probe file proves it. The real journal is not
     opened.
   - `control_slices`: unauthenticated probes that `/health/ready` answers and that
     `/model-spend/limits` exists (an authentication or method refusal, not a 404). No credential is
     ever sent, so a missing slice is found without any authority.
   - `spend_caps`: with `--control-state-dir`, a read-only look at the control database for a
     model-spend limit row for the project and provider whose per-call and daily caps are at least the
     profile's own per-call cap; `unchecked` without it, and also `unchecked` when no project is known
     (the profile declares no `model_spend.project_id` and `--project-id` is not given: at run time the
     project comes from the grant). An `unchecked` check is NOT a pass: it keeps `overall` from being `ready` or `ready_except_key`
     (the result is `not_fully_checked`, exit 11, with the unchecked names in the summary and in
     `unchecked`), because an unverified spend limit is what makes the first call refuse after the key is
     installed. A failing check still reports `not_ready`, ahead of anything unverified.
     The profile file itself must be a private (0600, owned) regular file, as the service requires.

3. **The command never reads or prints a key value, never calls a model, and writes nothing but a
   transient probe file in the state directory.** Any refusal text is a fixed category plus the name of
   the thing to fix.

4. **The first-call path is proved by a test, not by the command.** `tests/test_first_call_e2e_stub.py`
   drives the real supervisor, journal and control routes with a stub (recorded) transport in place of
   the network: one call writes one model-call receipt carrying its `spend_ledger` settlement and
   debits the ledger by exactly the receipted cost; neither the key marker nor the prompt or completion
   appears in any record; and a call refused for a missing limit makes no billed request (the client
   first asks the free token-count endpoint to price the worst case, then the ledger refuses) and a new
   run succeeds after a person sets the limit (the ledger replays a refusal for the same run and
   sequence).

5. **A runbook.** [`docs/first-call-readiness.md`](../first-call-readiness.md) orders the steps: deploy
   the slices, write the profile and the credential reference, place the key, have a person set the
   limits, run the check (give `--control-state-dir` and `--control-url`; expect `ready_except_key` before the key and `ready`
   after), start the
   service, make one call, then inspect the receipt row and the ledger.

## Consequences

- An operator learns what is missing before the first paid call, and the only thing left for the
  moment the key arrives is the key itself.
- The check adds no new authority: it reads files the service already reads, probes routes without a
  credential, and reads the control database read-only on the host that owns it.
- A deployment where the reasoning host cannot reach the control database reports `spend_caps` as
  `unchecked` rather than guessing, and the command then reports `not_fully_checked` (never `ready`):
  run it where the control state is readable, or check the limits through `GET /model-spend`.

## Limits

- No live model call has been made. The first live call is an operator step after the key is installed.
- `ready` means the checked preconditions hold now; it does not prove the key is valid with the provider
  (nothing verifies a key before it is used) and does not reserve any capacity.
- The key check is stat-level: a file with the right shape but a wrong key reads as `ready` and fails at
  the first call, which the receipt and the ledger then record.
- The check cannot see a unit's `Environment=` lines or a drop-in that sets a forbidden variable for the
  service; it checks the environment of the process that runs it. Start-up refuses a forbidden variable
  in the service's own environment regardless.
- The stub transport proves the Tessera side of the path (receipt, settlement, ledger); it does not
  prove anything about the provider's responses.

## Tests

`tests/test_first_call_readiness.py` (each check's states, the exit codes, that the key file is never
opened and no key marker or secret reaches any output) and `tests/test_first_call_e2e_stub.py` (the
end-to-end stub path above).
