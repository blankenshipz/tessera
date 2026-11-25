# First-call readiness (operator runbook)

Contract: [ADR 0109](decisions/0109-reasoning-first-call-readiness.md). The reasoning service reaches
the Anthropic Messages API with a key held only as a systemd credential. `tessera-reasoning-readiness`
checks everything the first receipted call needs except the key value, so the only thing left for the
day the key arrives is the key.

The ordered list of exactly what remains once the key exists is in
[reasoning first call: operator steps](reasoning-first-call-ops.md).

**No live model call has been made.** The first live call is an operator step after the key is
installed. The readiness command is read-only: it never reads the key, never calls the model, and
writes only a transient probe file in the state directory.

## Steps

1. **Deploy the slices.** The control service with the model-spend ledger (ADR 0090) and the reasoning
   service (ADR 0082, ADR 0083). The check reports a missing slice (`control_slices`).
2. **Write the profile and the credential reference.** Either run `tessera reasoning install --plan`
   and then `--apply` ([ADR 0119](decisions/0119-reasoning-service-install-command.md); it creates the
   service account, directories, profile, reference file and unit, writes the spend caps from flags and
   ends with this readiness check), or by hand: `/etc/tessera/reasoning.json` with the model,
   `credential_ref`, `limits`, operator-pinned `pricing_cents_per_mtok` and `model_spend.project_id`;
   the reference file `/etc/tessera/credentials/reasoning-model-credential` holding exactly that
   `credential_ref`. See [the runtime runbook](remote-reasoning-runtime.md).
3. **Place the key.** (After `tessera reasoning install`, `tessera-service-credential install` writes only the key: it keeps the reference file the install wrote when it is exactly the reference you give.) As a root-owned 0600 systemd credential,
   `/etc/tessera/credentials/reasoning-anthropic-api-key`, loaded by the unit as
   `LoadCredential=anthropic-api-key:...`, as the runtime runbook describes. Install it with
   `sudo tessera-service-credential install --service tessera-reasoning --name anthropic-api-key
   --owner OWNER --reference REFERENCE` (secret on standard input only; it also writes the
   `model-credential` reference file; see [service credentials](service-credentials.md) and
   [ADR 0093](decisions/0093-service-credential-install.md)). Never an environment variable,
   argument or shell variable.
4. **A person sets the spend limits.** `PUT /model-spend/limits` for the project and destination
   (see [model spend](model-spend.md)). Nothing is spent until a person does. The grant's project must
   equal the profile's `model_spend.project_id`; if both are set and differ, or neither is, the call
   is refused with `model_spend_not_configured`.
   **Issue the reasoning grant.** The call names an agent authority grant, and an execution grant (which
   needs a running experiment) is not required: a person issues a reasoning-only grant
   ([ADR 0155](decisions/0155-reasoning-only-grant.md)). See
   [reasoning first call: operator steps](reasoning-first-call-ops.md) for the command.
   The profile must carry the issued `grant_id`, `revision` and `epoch`. The readiness `grant` check reads the grant either through the read-only control API
   (`--control-url`, with the service's own `agent-token`; no database access, see
   [ADR 0162](decisions/0162-first-call-db-free-readiness.md)) or from the control database
   (`--control-state-dir`); with neither it reports `unchecked`. It fails closed with a named finding: `grant_profile_incomplete`, `grant_missing`, `grant_malformed`,
   `grant_kind_unsupported`, `grant_revoked`, `grant_expired`, `grant_lineage_inactive`,
   `grant_revision_mismatch`, `grant_scope_mismatch`, `grant_binding_inactive` (execution kind only; through `--control-url` the control plane reports one `bindings_active` answer for the grant's own bound execution grants, and a control plane that does not send it leaves the check `unchecked`),
   `grant_budget_exhausted` or `grant_budget_below_call_cap`. It does not check the subject's scopes (issuance
   does) or the read-only tool gate (the runtime does).
   `grant_binding_inactive` is deliberately stricter than the model-spend ledger. For an execution-kind grant the
   check fails unless the grant lists at least one binding and every bound execution grant exists, is `active`, is at
   the pinned revision and epoch and is unexpired; a malformed binding also fails it. The ledger's reservation check
   does not read those execution grants. The check is conservative: it can report `not_ready` for a grant the
   ledger would still accept.
5. **Run the check** in the unit's own context, as root: `tessera-reasoning-readiness check --in-unit`
   (it runs the check as the service user with the unit's `LoadCredential=` files, where they are
   root-owned 0400/0440, and the unit's listed sandbox properties; a credential whose source file
   is missing is left out and reported missing, so a fresh install ends with exit 10; see the amendment in [ADR 0109](decisions/0109-reasoning-first-call-readiness.md)).
   Or run it as the service user with the manager credential directory laid out as the service sees it:

   ```
   tessera-reasoning-readiness check --profile /etc/tessera/reasoning.json \
     --credentials-directory DIR --state-dir /var/lib/tessera-reasoning \
     [--control-state-dir DIR] [--control-url URL] [--project-id ID]
   ```

   `--in-unit` needs no database path: with `--control-url URL` alone, `spend_caps` and `grant` are
   read through the control API with the unit's `agent-token` credential, so one run as the service
   user can exit 0 (the credential needs `model_spend:record` and `authority:consume`, which the
   reasoning service already has). Give `--control-state-dir` as well, or instead, only where the
   database is readable; it takes precedence. An unreachable, refused or malformed answer is a
   failure, and a missing token leaves the check `unchecked`, never a pass.

   It prints JSON `{overall, checks, key}`. Each check has `name`, `status`
   (`ok`, `missing`, `fail`, `unchecked`), a `detail` and a `fix`. Exit status: **0** ready,
   **10** ready except the key, **11** not fully checked, **1** not ready, **2** usage.
6. **Expect `ready_except_key` before the key is placed and `ready` after.** Anything else names the
   thing to fix. `unchecked` (for example `spend_caps` with neither `--control-url` nor `--control-state-dir`, or `control_slices`
   without `--control-url`) is not a pass: the result is `not_fully_checked` (exit 11), never `ready` or
   `ready_except_key`. Run it with `--control-url` (the database-free read) or `--control-state-dir`, or check the limits
   through `GET /model-spend`.
7. **Start the service and make one call.** First create the admission marker,
   `sudo install -m 0600 /dev/null /etc/tessera/reasoning.admitted`: the unit has
   `ConditionPathExists=/etc/tessera/reasoning.admitted` and is silently skipped without it. Then
   `sudo systemctl start tessera-reasoning.service` (never `enable`; the unit has no `[Install]`
   section). After the call, stop the unit and remove the marker.
8. **Inspect the result.** `tessera-reasoning status` prints one receipt row per call with its
   `spend_ledger` settlement; `GET /model-spend` (or `tessera_model_spend`) shows the debit.

## What is checked

| Check | Passes when |
| --- | --- |
| `environment` | none of `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_API_KEY` is set, even empty |
| `profile` | an Anthropic profile start-up accepts, an admitted tier, pinned prices and limits, a `model_spend.project_id` |
| `credential_reference` | the `model-credential` file equals the profile's `credential_ref` |
| `api_key_file` | present, regular, owned by the credential owner, mode 0600, 1 to 513 bytes (a 512-byte key plus a newline; stat only, never opened) |
| `egress_allowlist` | the model-runtime allowlist is exactly the API host and port |
| `receipts_store` | the state directory is writable, not group/world-writable, with free space |
| `control_slices` | `/health/ready` answers and `/model-spend/limits` exists (probed without any credential) |
| `spend_caps` | (database or `GET /model-spend/limits`) a limit row for the project and provider with caps at least the profile's per-call cap (`unchecked` when no project is known: set `model_spend.project_id` or pass `--project-id`) |

Only a missing key file still reads `ready_except_key`, and only when every other check ran and passed. A
check that could not run makes the result `not_fully_checked`; everything else that is not `ok` is `not_ready`.

## What it does not prove

It does not prove the key is valid with the provider, it reserves no capacity, and `ready` describes
the moment it ran. The Tessera side of the first-call path (one receipt, its ledger settlement, the
debit equal to the receipted cost, no key or prompt in any record, a refusal sending nothing) is shown
by `tests/test_first_call_e2e_stub.py` with a stub transport, not by this command.
