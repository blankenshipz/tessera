# 0215 — Salad stages default to two infrastructure retries; an unreadable guardian accounting is its own retryable cause; the guardian config is a closed key set

## Status

Accepted. Amends [ADR 0058](0058-infrastructure-retry-of-stages.md) (the `infra_retries`
default) and the safety-stop table of [ADR 0207](0207-salad-runtime-retry-and-lease-window.md)
item 3. No schema change and no migration. Guardian change: the safety service upgrades before
control.

## Context

- `RetryPolicy.infra_retries` defaults to 0. A Salad route can be stopped by its own safety
  plane (a guardian restart, an unreadable rate, a lost heartbeat) or moved by the provider:
  infrastructure, never evidence about the stage. With no infrastructure retry every such stop
  fails the stage permanently, so a guardian restart stopped every live route for good.
- The guardian's aggregate backstop (`_enforce_total_cap`) fenced every frame as `total_cap`
  when the total could not be *read*, which control (correctly) never retries. A read error was
  indistinguishable from a real cap hit.
- The guardian read its runtime config as an open dict: a misspelt `total_cap_cents` silently
  meant "no aggregate cap", a misspelt `max_open_billable_frames` silently meant 1.

## Decision

1. **Submit-time default.** A freshly submitted stage that may run on Salad (a v1
   `salad-gpu-v1` stage, or a v2 stage whose provider or any alternate candidate is `salad`)
   and does not write `retry.infra_retries` gets `infra_retries: 2` written into its spec
   before the digest (`campaign_salad_retry_default`). It is applied identically by submit,
   validate, a submission approval and a standing approval, so their digests agree.
   - It is explicit in the stored spec: in the digest the approver approves, in
     `worst_case_attempts` / `hold_attempts`, and in every exposure check, exactly as if the
     author had written it. No cap, ceiling or admission check is changed.
   - An explicit value, 0 included, is always kept (0 serializes as absent, so a spec that
     writes 0 keeps the digest it had before).
   - A stored spec is never re-defaulted: one stored without the key still means 0 and keeps
     its digest, and a re-run (ADR 0074) derived from a stored spec keeps the stored value.
   - The default is not applied where the stage would no longer be admitted with it (a
     one-time holdout claim, a worst-case hold over the experiment ceiling, or over a named
     operator profile's per-task ceiling): that stage keeps 0 and is admitted as before.
   - Every retry stays a new attempt with its own hold inside the campaign cap and the re-run
     tree budget.
2. **`accounting_unavailable`.** When the guardian cannot compute its aggregate total, it
   still fences every frame that may bill (fail-closed, unchanged), but under the new service
   stop cause `accounting_unavailable` (event and frame note of that kind, detail the exception
   type name only). It is in `SAFETY_STOP_CAUSES`, so control classes the attempt
   `infra_salad_safety_stop` and retries it within `retry.infra_retries`. A total read and
   found over the cap stays `total_cap` and is never retried.
3. **Closed guardian config.** The guardian config is the closed key set of
   [ADR 0214](0214-deploy-gate-startup-equal-check.md): one parser,
   `salad_safety_service.validate_guardian_config` (called through `prepare_guardian_startup`),
   serves both a real start (`--run`) and `--check-config`, so the two can never disagree. An
   unknown key (its name cut to 64 characters), a missing required key, a wrong type, an
   out-of-range setting, `proxy_uid: null`, an empty `organization_ids`, a `credential_version`
   outside 1..2^31-1, an empty, over-long (4096) or NUL-carrying string, a malformed
   `control_origin`, or `unix_socket` without `proxy_uid` (or the reverse) refuses startup
   before any key fetch or network work, with exit 2, the stdout line
   `{"state":"unavailable","reason":"config_refused"}` and a stderr message naming the key or
   check, never a value. (An earlier draft of this ADR had a second, looser model,
   `salad_guardian_config`; it was dropped for the single parser.)
4. **Operator fallback chain.** The default is also withheld when the per-task ceiling of a
   profile on the operator `fallback_profiles` chain of a named profile would refuse the
   stage's worst-case hold with it, so the default never silently removes an operator CPU
   fallback the stage would have run on at 0 (`operator_fallback_refusal` applies the same
   bound at a move). The cost is conservative: such a stage keeps 0 on every profile, its own
   included. A review pre-approval's `max_total_cents` is not known at submit, so the
   preapproval coverage check of a fallback (`campaign_salad_fallback`) can still decline a
   fallback for the extra two attempts; the stage is then held for a person, never over-admitted.

## Consequences

- Upgrade order: the safety service first. An older control does not know
  `accounting_unavailable` and classes such a stop `salad_not_passed` (as it classed
  `total_cap` before), so nothing worse than today happens in between.
- A guardian config with a key the closed set does not know now refuses to start; fix the key.
- **Guardian upgrade procedure.** Before restarting the guardian on the new release, run that
  release's `python -m tessera_control.salad_safety_service --check-config --config FILE` (or
  `tessera deploy-check --guardian-args ...`) against the deployed config. A refused config
  exits 2 and the service unit does not restart on exit 2, so the guardian would stay down.
  Upgrade the guardian with no live Salad routes: the restart stops every live frame as
  `guardian_restart`, and a stage already running keeps the `infra_retries` it was approved
  with (0 for every stage submitted before this change), so such a stop fails it for good.
- A standing approval recorded before this change for a Salad stage that leaves
  `infra_retries` unset no longer matches a fresh submission of the same document (its digest
  now carries the default); write `infra_retries: 0` to keep the old digest, or re-approve.
