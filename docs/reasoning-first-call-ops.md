# Reasoning first call: what is left once the API key exists

This is the short, ordered list for the operator. Everything on the Tessera side of the first
Anthropic call has been exercised offline; what remains is the key, a person's spend limits and one
supervised start. Background pages: [first-call readiness](first-call-readiness.md) (the
read-only check), [the runtime runbook](remote-reasoning-runtime.md), [service
credentials](service-credentials.md) (the installer) and [model spend](model-spend.md) (the ledger).

**No live model call has been made yet.** Nothing below has been run against the provider.

## What is already shown offline (stub transport, no network)

| Behaviour | Where it is shown |
| --- | --- |
| One call end to end: one receipt, its ledger settlement, the debit equal to the receipted cost, no key, prompt or completion text in any durable record, a call refused before the transport until a person sets a limit | `tests/test_first_call_e2e_stub.py` |
| The runtime driving the Messages client: caps, receipts, rate-limit (429) backoff without changing the model, refusal paths | `tests/test_anthropic_runtime_integration.py`, `tests/test_anthropic_messages.py` |
| A 429 backs off and the retry uses the same model and an identical body; the request's `max_tokens` is the profile's output cap; the daily cost cap refuses the next call before any Messages request (profile cap and control-plane ledger); a missing key file fails closed with no network and a decoy `ANTHROPIC_API_KEY` never used; `main` settling on the control-plane ledger | `tests/test_first_call_gaps.py` |
| Credential reference lookup, the forbidden API-key environment variables, a missing or wrong reference file or key file failing closed at start-up (exit 78), the secret never in stderr | `tests/test_reasoning_credential_references.py` |
| Per-call and daily caps through the model-spend ledger, reserve then settle or release, fail closed when the ledger is unconfigured or unreachable | `tests/test_reasoning_model_spend.py` |
| The read-only readiness command | `tests/test_first_call_readiness.py` |

## Steps once the key is available

Roles: **root** is the host operator; **person** is a human credential on the control plane; the
**service user** is `tessera-reasoning`. The key is only ever a systemd credential
(`LoadCredential=`). Never put it in an environment variable, the profile, a unit file or a command line.

1. **Install the key (root).** The secret goes on standard input only (a pipe from your secret
   provisioner, or the no-echo prompt). `OWNER` and `REFERENCE` are labels, not secrets:

   ```sh
   sudo tessera-service-credential install --service tessera-reasoning --name anthropic-api-key \
     --owner OWNER --reference REFERENCE --profile /etc/tessera/reasoning.json
   ```

   Expected: it writes `/etc/tessera/credentials/reasoning-anthropic-api-key` and
   `/etc/tessera/credentials/reasoning-model-credential`, both root:root 0600, and a receipt with a
   fingerprint (never the value). It refuses with `reference_mismatch` unless the profile's
   `credential_ref` is exactly `anthropic-api-key://OWNER/REFERENCE`, with `exists` if a key is
   already installed (add `--replace` to rotate), and when `ANTHROPIC_API_KEY`,
   `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_API_KEY` is set. Then:

   ```sh
   sudo tessera-service-credential check --service tessera-reasoning   # non-zero on any problem
   ```

2. **Set the spend limits (person).** There is no command line for this and the MCP tool
   `tessera_model_spend` is read-only. With a human credential:

   ```
   PUT /model-spend/limits
   {"project_id": "PROJECT", "provider_id": "anthropic", "per_call_cents": 5, "daily_cents": 100}
   ```

   Rules enforced by the control plane: `per_call_cents` must not exceed `daily_cents`; the check
   also needs `per_call_cents` at least the profile's `limits.max_cost_cents_per_call`
   (`spend_caps` in the readiness check). Until a person does this, nothing is spent: the call is
   refused before anything is sent (`model_spend_not_configured`).

   **Issue a reasoning-only grant (a person).** No experiment or execution grant is needed
   ([ADR 0155](decisions/0155-reasoning-only-grant.md)). The grant authorizes model calls only, has its
   own budget (at least 1 cent) and an expiry of at most 24 hours, and is refused with
   `model_spend_not_configured` until the limits above exist. Issue it with the normal route,
   `POST /projects/{id}/agent-authority-grants` with `bounds.kind` set to `reasoning` (human session, or the
   owner token with an automation label), with the CLI, or with the person-only MCP tool
   (`tessera-mcp --reasoning-grant-tool`, absent by default). The CLI is:

   ```sh
   tessera authority issue-reasoning --project ID --subject-credential-id ID \
     --destination DEST --provider anthropic --model MODEL --budget-cents N \
     --expires-in SECONDS --owner-header PATH [--automation-label LABEL]
   ```

   `--expires-in` is 1 to 86400 seconds. `--destination` is the agent's identity destination, not the model
   host. To replay a lost answer, re-run with the printed `--idempotency-key` and the printed
   `--expires-at` (a relative `--expires-in` produces a different request each time). The command prints
   the grant id, revision, epoch and the runtime profile fields (`grant_id`, `revision`, `epoch`,
   `subject_credential_id`, `destination`); it never prints a credential. The agent profile must carry those
   `grant_id`, `revision` and `epoch` values.    The exact flags are in `tessera authority issue-reasoning --help`. The grant limits authority and
   spending; the egress to the model host is limited by the model destination and the egress
   allowlist, not by the grant.

3. **Check the project matches (operator).** The spend project is the agent's authority
   grant project, or the profile's `model_spend.project_id` when the grant carries none; when
   both are set they must be the same. If both are set and differ, or neither is set, the
   supervisor refuses with `model_spend_not_configured`. The agent token is delivered as
   `LoadCredential=agent-token:/etc/tessera/credentials/reasoning-agent-token`; it is a scoped agent
   credential, never an owner token, and is not the model key.

4. **Run the readiness check (root, or the service user).** The credentials directory must be laid
   out as the service sees it (the files named `profile`, `prompt`, `agent-token`, `model-credential`,
   `anthropic-api-key`):

   ```sh
   tessera-reasoning-readiness check --profile /etc/tessera/reasoning.json \
     --credentials-directory DIR --state-dir /var/lib/tessera-reasoning \
     --control-state-dir DIR --control-url URL --project-id PROJECT
   ```

   `--control-state-dir` is optional: as the service user (or with `--in-unit`) pass only
   `--control-url URL` and the spend caps and the grant are read through the read-only control API
   with the service's own agent token ([ADR 0162](decisions/0162-first-call-db-free-readiness.md)); no
   database access is needed.

   Expected exit status: **0** ready. The key-file check requires a regular file owned by the user
   running the check with no group or other access (0600 as installed), 1 to 512 bytes. Exit **10**
   means everything but the key file is fine, **11** means a check could not run (pass `--control-url`, or `--control-state-dir` where the database is
   readable), **1** not ready (the output names the fix), **2** usage.
   Do not go on until it is 0. It does not open the key and does not prove the provider accepts it.

5. **Admit the service (a person, root).** The unit carries
   `ConditionPathExists=/etc/tessera/reasoning.admitted`; without the marker it is silently skipped.
   Admit it with your own session credential (a root-owned 0600 header file
   `Authorization: Bearer ...` holding a `human_session` token; the owner token is refused, labelled
   or not). The command records who admitted the service, the caps (defaulting to the profile's
   limits) and the expiry on receipts, then writes the marker with a reference to that record
   ([ADR 0163](decisions/0163-reasoning-admission-delegation-record.md)):

   ```sh
   sudo tessera reasoning admit --session-header SESSION_HEADER_FILE --expires-in 86400
   ```

   The marker is an admission, not a grant or billing proof. Run the readiness check again: its
   `admission` check is `ok` for an active record that covers the profile and `fail` for a revoked,
   expired, altered or exceeded one. A hand-made empty marker
   (`sudo install -m 0600 /dev/null /etc/tessera/reasoning.admitted`) still lets the unit start, but
   readiness reports it as `unrecorded`; `tessera reasoning admit --replace` records it.

6. **Make the one call (root).** `sudo systemctl start tessera-reasoning.service`. Do **not**
   `enable` it: the unit has no `[Install]` section on purpose. A start-up refusal exits 78 and is
   not restarted; read the sanitized reason with `sudo journalctl -u tessera-reasoning.service`.
   The unit is one-shot. A completed run exits 0 and the unit goes inactive, not failed. Starting
   it again with the same run identity runs nothing: it logs `run <id> already closed (state
   completed); not rerun, no model call` and exits 0. Exit codes:

   | Exit | Meaning | Unit restarts it |
   | --- | --- | --- |
   | 0 | The run completed, or this run identity had already completed before this start (logged `run <id> already closed (state completed); not rerun, no model call`). | No |
   | 1 | Unexpected crash (Python's default exit). The journal lets the restart resume without replaying a call. | Yes (`Restart=on-failure`) |
   | 2 | The run ended without completing: a fence or block halted it (the receipt's `reason`, e.g. `budget_exhausted`, `grant_revoked`, `tool_catalog_changed`) or it was stopped. Also a start of a run identity that had already ended that way. | No |
   | 77 | Refused: another supervisor holds the state directory (`supervisor_already_running`), or the run identity is already bound to another profile or prompt (`run_identity_rebind_refused`). | No |
   | 78 | Configuration error: unreadable or invalid profile or prompt, invalid `--run-id`, missing manager credential directory or agent-token variable; also every refusal of `service_startup` before the runtime starts (API-key variable present, missing or mismatched credential files). | No |

   Only exit 1 (a crash) is restarted (`Restart=on-failure`, `RestartPreventExitStatus=2 77 78`,
   at most 3 starts in 120 seconds). For a second call, request a new run:
   `sudo tessera reasoning install --apply --new-run` writes a fresh generated `run_id` (and the
   installed prompt's digest) into the profile; then start the unit again.

7. **Read the receipt (operator).**

   ```sh
   tessera-reasoning status --profile /etc/tessera/reasoning.json --state-dir /var/lib/tessera-reasoning
   ```

   Expect one row per model call with the configured and reported model, token counts, the computed
   cost (`computed_from_pinned_prices`), the limits in force, and `spend_ledger` with
   `"status": "settled"`, a `call_id` and `settled_micro_cents`. `unsettled`, `unreserved` or
   `reserved` means the ledger did not close the call; resolve it as in
   [model spend](model-spend.md).

8. **Cross-check the debit (person or MCP read).** `GET /model-spend?project_id=PROJECT&days=1`
   (or `tessera_model_spend`): `today.calls` is 1, `today.settled_cents` equals the receipted
   cost, `today.reserved_cents` is 0 and `open_reservations` is empty. Then compare the cost with
   the provider's usage page; the cost is computed from your pinned prices and is an estimate, not
   an invoice.

9. **Check nothing leaked.** The journal and `status` output hold no prompt or completion text and
   no key; the completion text is only in the private output file in the state directory.

10. **Stop (root).** `sudo systemctl stop tessera-reasoning.service`, then
    `sudo tessera reasoning revoke-admission --session-header SESSION_HEADER_FILE --project-id
    PROJECT --admission-id ID` (revokes the record and removes the marker that references it), or
    `sudo rm /etc/tessera/reasoning.admitted` for a bare marker.

## Rollback

- Stop the unit and remove the marker (step 10). With no marker the unit cannot start.
- Revoke the agent authority grant (person): `POST /agent-authority-grants/{id}/revoke`. Revoke or
  expire the agent credential as well. A person can set `per_call_cents` and `daily_cents` low
  again with `PUT /model-spend/limits`.
- Rotate the key with the same `install` command plus `--replace`. To withdraw it, stop the unit
  first, then run as root:

  ```sh
  sudo tessera-service-credential withdraw --service tessera-reasoning --name anthropic-api-key \
    --owner-label WHO [--reason TEXT]
  ```

  It removes the key file and the `model-credential` reference file under
  `/etc/tessera/credentials/` together (both or neither), refuses symlinks, hard links and paths
  outside the directory, never reads the key, and appends a hash-chained receipt (who, the
  credential reference and fingerprint from the install receipt, when, outcome). A second run records
  `already_absent`. `check` then reports the credential as not configured, and the unit will not
  start until a key is installed again. Then revoke the key at the provider.

## Start-up refusal of a bad admission

The unit loads the admission marker as `LoadCredential=admission:` (a reference, never a secret).
At start-up the supervisor reads it offline and exits 78 (configuration refusal, never restarted) with
`admission_marker_malformed` or `admission_expired` on stderr when a recorded marker is damaged or
past its expiry. A bare marker, or a unit without the credential, starts as before. Revocation needs
the record: run the readiness check. See [ADR 0170](decisions/0170-reasoning-startup-admission-refusal.md).

## Not proven offline

- That the key is valid with the provider and has access to the pinned model.
- Real 429 timing: the backoff is shown with a stub, not with the provider's actual headers and delays.
- Real pricing: costs come from your pinned `pricing_cents_per_mtok`, not from an invoice.
- That the host's egress allowlist reaches the provider in the deployed network.
