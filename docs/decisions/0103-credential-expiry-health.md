# ADR 0103: credential expiry is visible and refused up front

Status: accepted and implemented. No schema change. Builds on ADR 0040 (providers), 0047 (data
destinations), 0060 (placement), 0062 (CPU profiles) and the private credential backend of the Salad
guardian stack.

## Context

A Salad management credential is enrolled with a fixed lifetime. One expired without notice, and the
next Salad runs failed deep inside the run, when the guardian tried to resolve it. Nothing in the
control plane said the credential was expired, nothing refused a stage that needed it, and rotating it
had no supported command (the enrollment tool created version 1 only).

## Decision

1. **Metadata-only credential health.** A provider may expose a `credential_health` hook returning,
   for each credential it depends on, its reference, version, expiry, revocation, seconds remaining
   and a state: `ok`, `warning` (inside a 24 hour window before expiry), `expired`, `revoked`,
   `version_mismatch` (the profile pins a version the store no longer holds) or `unknown`. It is
   read from the credential store's metadata; the value is never read into a response, log or receipt.
   It is reported by `GET /credential-health`, in `/operator/health`, and by the read-only MCP tool
   `tessera_credential_health`. The control service needs read access to the private credential
   directory (`serve --salad-credential-directory`); without it the state is `unknown`, which is
   reported and never blocks (the guardian remains the authority at run time; this is an early guard).
2. **Admission.** Submit and approval refuse a stage whose provider credential is in a blocking state
   (`expired`, `revoked`, `version_mismatch`) with a message that names the reference, the version, the
   expiry and how to rotate. Placement skips such a candidate as `credential_unavailable`, so a stage
   with another approved candidate runs there and the skip is on the placement receipt. A running
   single-provider stage fails up front, before any hold, with stop reason `credential_expired`,
   `credential_revoked` or `credential_version_mismatch` and the event
   `campaign.stage_credential_blocked`; a `warning` state emits `campaign.stage_credential_warning`
   and proceeds.
3. **Rotation.** The enrollment tool (`python -m tessera_operator.salad_enroll`) gains two
   owner-run subcommands. `rotate` enrolls the next version of an existing, unrevoked reference; the new
   secret comes from the hidden terminal prompt, or from stdin with `--secret-stdin` (one pipe or file,
   never a terminal, at most 4096 printable ASCII bytes). `renew` extends the same key's expiry by at
   most 24 hours and takes no secret. Both run as the uid that owns the private 0700 credential
   directory (the backend enforces it); there is no API or MCP route. They print only reference,
   previous version, version and expiry, and remind the operator that the profile's `credential_version`
   must be set to the new version (through the provider registration command) and the services
   restarted. Refusals name a closed category, never input.
4. **The stuck detector** (a separate change, not on this base) can read the same health records to
   report a blocked reason `credential_expired` for campaigns waiting on such a provider. That
   integration is a follow-up, not part of this change.

## Consequences

- An expired or revoked credential is visible before a campaign needs it, and a stage that needs it
  is refused or failed with a receipt-recorded reason instead of failing mid-run.
- Rotation has a supported, audited-by-ownership path that never exposes the value.
- `unknown` never blocks: a deployment whose control service cannot read the credential directory keeps
  today's behaviour, with the guardian refusing at run time.

## Limits

- Expiry is a property of the stored reference, not of the provider's own key: the provider can revoke a
  key earlier, and nothing here calls the provider.
- `rotate` and `renew` run on the host that holds the credential directory, by its owner; they are
  not remotely invocable by design.
- The health check reads the directory on each request; it adds no caching.
- Nothing was run against Salad; tests use synthetic markers only.

## Tests

`tests/test_credential_health.py` (states and the provider hook),
`tests/test_campaign_credential_admission.py` (submit, approval, placement and the running stage),
`tests/test_salad_enroll_rotation.py` (rotate and renew from stdin, refusals, no secret in any output),
`tests/test_mcp_credential_health.py` (the read-only tool).
