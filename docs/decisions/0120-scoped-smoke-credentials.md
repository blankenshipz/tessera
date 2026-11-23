# ADR 0120: Scoped smoke credentials

## Status

Accepted. Adds a schema migration (the smoke grant table). Builds on
[ADR 0065](0065-operator-enabled-mcp-approval-tool.md) (the approval tool) and on the decision classes of
`approval_trust`.

## Context

The MCP steering smoke (`scripts/mcp_steering_smoke.py`) drives a campaign through submit,
approval, pause, resume and cancel. It needs two credentials: an agent credential enrolled for a
destination, and a person-side credential used only for the approval, which no agent credential
can give. Neither can be hand-issued by an agent or a script, so the smoke could only run in
tests. Handing it a general person session would let a short test act with a person's full
authority, which is the wrong trade.

## Decision

1. **An owner-run issue command.** `tessera dev smoke-credentials issue --state-dir D
   --project-id P --destination X --out-dir DIR [--ttl-minutes N]`, run by whoever owns the state
   directory, creates one grant: an agent credential, an approver credential and a grant row that
   ties them together. `revoke --grant-id` and `list` complete it. There is no HTTP route and no
   MCP tool for it; an agent cannot issue itself a grant.

2. **What the control plane enforces**, not the smoke script:
   - the agent credential may use the `local-docker` provider only and has a budget of 0 cents;
     a submit naming another provider or a cap above zero is refused;
   - the approver credential is not a person session. It may only approve campaigns submitted by
     the paired agent credential and shaped as above, and nothing else;
   - the approver credential is limited, in the store's mutation path and in the HTTP principal
     check, to read requests and `POST /campaigns/{id}/approve` (refused otherwise with the
     closed reason `smoke_grant_approval_only`), and an approval is refused unless the grant is
     live, the campaign is in the grant's project, the campaign was submitted by the paired agent
     credential and its stored spec still passes the local-docker, $0 check;
   - both credentials expire with the grant, at most 60 minutes after issue;
   - revoking the grant disables both at once.

3. **What is recorded.** The audit events `smoke_credentials.issued` and
   `smoke_credentials.revoked`, and, on the approval event of a campaign approved with the
   approver credential, `smoke_grant_id`, which therefore appears on the campaign receipt. The
   smoke copies the id into its own report as `smoke_grant_id`.

4. **Files.** `DIR/agent.token`, `DIR/approver.token` and `DIR/smoke-grant.json` are written 0600
   in a directory the owner alone can access. The grant file holds no token:
   `{"schema": "tessera.smoke-credentials.v1", "grant_id", "project_id", "destination",
   "expires_at", "agent_credential_id", "approver_credential_id", "providers": ["local-docker"],
   "max_budget_cents": 0, "control_url": null}`.

5. **Smoke side.** `--smoke-credentials-dir DIR` validates the schema, the provider list, the zero
   budget, the expiry and the file modes (including the directory), then fills in the project,
   destination and token files unless given explicitly; `--control-url` stays required. The smoke
   campaign itself is $0 (`budget_cap_cents` 0, stage `max_exposure_cents` 0) and the check that
   nothing was settled stays. The smoke's checks are a convenience: the server's limits above are
   what hold if the smoke is bypassed.

## Schema

One table, `smoke_credential_grants` (grant id, project, the two credential ids, destination,
providers, `max_budget_cents` fixed at 0, expiry, revoked_at), append-only apart from a one-time
revoke (triggers). It is added by a new migration; the number in the shared reservation list is
renumbered by the integrator on merge. The receipt's `approval` shows `smoke_grant_id`, looked up
from the grant by the approving principal, so existing receipt digests are unchanged. The check
runs in the control plane: a service must be restarted to pick it up.

## Relationship to other decisions

The approval is still the approval tool of ADR 0065 and is recorded as an ordinary approval; the
approver credential only narrows who may call it for a smoke campaign. In `approval_trust` terms
it is a decision class of its own, bounded to local-docker campaigns of zero budget, so it never
widens what an agent may decide in production and never counts as a general person approval.

## What this does not do

- No remote provider, no spend, no paid call.
- Never a general person session: the approver credential cannot approve anything outside the
  paired agent's smoke-shaped campaigns and cannot do any other person action.
- Not available to agents or over the network: owner-run only, on the state directory.
- Does not prove an experiment ran; the smoke's receipt records steering and a zero settled
  budget only.

## Amendment: synthetic public snapshot

The smoke needs a public snapshot as its stage input, and only a human can declare one public.
`issue` therefore also makes sure the project holds one tiny synthetic public snapshot, so the
smoke needs no human step. This is acceptable because:

- the bytes are fixed constants in `smoke_snapshot.SMOKE_FILES` (two short ASCII files, clearly
  marked synthetic, under 1 KiB); nothing is read from the owner's data or the environment;
- it is owner-run only, on the state directory, with the root owner's authority, through the
  ordinary `adopt_snapshot` path and its atomic public declaration (`CC0-1.0`, source
  `https://tessera.invalid/smoke-synthetic-v1`); the data-class record is immutable, as always;
- the decision is recorded: the data-class provenance carries the decision class
  `owner_automation_labelled` and the automation label `smoke-credentials` (the label grants no
  authority, it marks the decision), and the usual `workspace.snapshot_adopted` event is written;
  the `smoke_credentials.issued` event adds the `snapshot_id`;
- there is still no agent path to declare anything public: the agent credential and the smoke
  approver are unchanged, and [ADR 0039](0039-data-class-guard.md) is untouched.

What it stores and where: two files in the project's snapshot store, in the state directory's
database, like any adopted snapshot. The inventory digest includes the root path, so it is computed
over the fixed logical root `/tessera-smoke-synthetic-v1` instead of the temporary directory the
bytes are written to; the digest is the same on every run and machine. A second `issue` finds the
project's snapshot by digest and reuses it (one row). If the same bytes are already held as a
non-public snapshot, `issue` refuses with `smoke_snapshot_digest_not_public`, because a data class
cannot be changed. The snapshot is adopted before the grant, so a refused adoption creates no grant
or credential; a later failure leaves only the harmless public snapshot, reused next time.
`smoke-grant.json` (still `tessera.smoke-credentials.v1`) gains the optional `snapshot_id` and
`snapshot_digest`; the smoke uses that snapshot unless `--snapshot-id` is given, and fails if the
catalog does not list it as public with that digest. Older grant files without these keys behave
as before.
