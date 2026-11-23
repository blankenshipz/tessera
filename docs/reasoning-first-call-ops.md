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

3. **Check the project matches (operator).** The project in the agent's authority grant must equal
   the profile's `model_spend.project_id`. If both are set and differ, or neither is set, the
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

   Expected exit status: **0** ready. The key-file check requires a regular file owned by the user
   running the check with no group or other access (0600 as installed), 1 to 512 bytes. Exit **10**
   means everything but the key file is fine, **11** means a check could not run (pass both
   `--control-state-dir` and `--control-url`), **1** not ready (the output names the fix), **2** usage.
   Do not go on until it is 0. It does not open the key and does not prove the provider accepts it.

5. **Create the admission marker (root).** The unit carries
   `ConditionPathExists=/etc/tessera/reasoning.admitted`; without the marker it is silently skipped.
   The marker is an operator assertion, not a grant or billing proof:

   ```sh
   sudo install -m 0600 /dev/null /etc/tessera/reasoning.admitted
   ```

6. **Make the one call (root).** `sudo systemctl start tessera-reasoning.service`. Do **not**
   `enable` it: the unit has no `[Install]` section on purpose. A start-up refusal exits 78 and is
   not restarted; read the sanitized reason with `sudo journalctl -u tessera-reasoning.service`.

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

10. **Stop (root).** `sudo systemctl stop tessera-reasoning.service` and
    `sudo rm /etc/tessera/reasoning.admitted`.

## Rollback

- Stop the unit and remove the marker (step 10). With no marker the unit cannot start.
- Revoke the agent authority grant (person): `POST /agent-authority-grants/{id}/revoke`. Revoke or
  expire the agent credential as well. A person can set `per_call_cents` and `daily_cents` low
  again with `PUT /model-spend/limits`.
- Rotate the key with the same `install` command plus `--replace`. The installer has no remove
  subcommand (only `install`, `check`, `list`, `receipts`); to withdraw the key, delete the two
  files under `/etc/tessera/credentials/` as root and revoke the key at the provider.

## Not proven offline

- That the key is valid with the provider and has access to the pinned model.
- Real 429 timing: the backoff is shown with a stub, not with the provider's actual headers and delays.
- Real pricing: costs come from your pinned `pricing_cents_per_mtok`, not from an invoice.
- That the host's egress allowlist reaches the provider in the deployed network.
