# Provider trust setting

**Status: implemented.** This page is the operator side of
[ADR 0040](decisions/0040-multi-stage-multi-vendor-campaigns.md) §4. `tessera serve
--provider-trust FILE` loads the file; without it the built-in default applies and only
public data reaches any provider (the [ADR 0039](decisions/0039-data-class-guard.md)
rules otherwise remain). Other validation checks (`docs/platform-validation.md`) start
`serve` without `--provider-trust` and so use public inputs only.

The provider trust setting is a deployment-owned file that says which data classes each
compute provider instance may receive: `public`, `private_code_approved` and
`private_derived` (every non-public record). Agents and campaign specs can read it and
narrow it, never widen it. There is no write API; host file permissions are the boundary.

## Rules an operator must know

- **Keyed on the provider instance.** A provider receives a private class only for the
  worker principals listed under it. A principal listed nowhere, or under a different
  provider than the stage names, receives `public` only. A principal may be listed under
  at most one provider.
- **Fail-closed default.** Without a file, every provider, including `local-docker`,
  receives `public` only. Tessera cannot verify that a worker principal runs on the
  control host, so the default binds none.
- **Approved code needs its own class.** A snapshot carrying an owner code approval is
  admitted only where `private_code_approved` is allowed, even when its data files are
  recorded `public`. It is never counted as data.
- **File checks.** The file must not be a symlink, must be owned by the service user and
  must not be group- or world-writable. A malformed or refused file stops startup with
  exit 2; a refused reload keeps the previous setting and records
  `provider_trust.reload_refused`.
- **Shared across processes.** `serve` records the file at start and on reload; every
  process that shares the state database (ticket issue, preflight, lease, receipts) reads
  the newest recorded setting from it, so a reload reaches all of them. With nothing
  recorded the built-in public-only default applies. Run receipts record the setting digest,
  provider and admission basis when the lease is issued; older receipts report
  `not_recorded`.
- **Audit.** Each new digest is stored immutably and records
  `provider_trust.policy_changed` with the per-provider difference and the actor.

## Refusals name the entry

When a campaign stage is refused because a provider's worker is not trusted for the stage's data,
the 409 says what to merge into that provider's entry under `"providers"`: the class to add to
`data_classes` and the worker principal to add to `worker_principal_ids`. Merge it into the existing
entry; replacing the entry would drop the other workers listed there. Nothing lists a worker
automatically: the file stays the operator's policy, and a refusal is the only thing the platform
adds.

## Limiting a provider by licence label

A provider entry may also carry `allowed_license_labels`: a list of 1 to 16 unique label slugs (the
same slugs a person adds to snapshots, see [licence labels](license-labels.md)). A provider the file lists
without the key is unrestricted and the setting digest is unchanged; a provider the file does not list
(a registered provider under `all_registered_providers`, say) is refused anything labelled and may receive
only `unlabelled` data. Present, the provider receives a stage only if every
effective label of every input, inherited through lineage, is in the list; unlabelled data needs
`unlabelled` in it, and data whose lineage cannot be resolved (`unknown-lineage`) is always refused. It
applies in every mode, `all_registered_providers` included, and only narrows what the data class rules
admit.

```json
"salad": {"data_classes": ["public", "private_derived"],
          "worker_principal_ids": ["<worker-principal-id>"],
          "allowed_license_labels": ["cc-by", "public-domain"]}
```

## Upgrade impact

On upgrade without a file, local CPU grants on `private_derived` releases (including
campaign stages fed by upstream outputs) and any snapshot carrying a code approval are
refused with a 409 naming the provider and worker principal; the runner blocks the
stage. Public-only local campaigns keep working. Write a file before upgrading if you
use derived inputs or approved code.

## Minimal file: restore local behaviour

Replace the placeholder with the worker principal that runs `tessera-worker
development-poll` for local campaigns.

```json
{
  "schema": "tessera.provider-trust.v1",
  "providers": {
    "local-docker": {
      "data_classes": ["public", "private_code_approved", "private_derived"],
      "worker_principal_ids": ["<local-worker-principal-id>"]
    }
  }
}
```

## Owner mode: agents route data across registered providers

[ADR 0047](decisions/0047-agent-routed-data-destinations.md). Adding `"mode":
"all_registered_providers"` gives every principal listed under a provider that is
registered on this platform (built in, or named in the `--campaign-providers` file) every
data class; agents and specs then decide where data goes among those providers. The
`data_classes` lists are ignored in this mode (keep them for when the mode is removed). A
principal listed nowhere, or under an unregistered provider id, still receives public only,
and specs and the approval-time setting still narrow. Data never goes to the public
internet in any mode: non-public inputs cannot use egress or workload fetch hosts, and every
receipt records provider, input class, digests and the setting digest.

```json
{
  "schema": "tessera.provider-trust.v1",
  "mode": "all_registered_providers",
  "providers": {
    "local-docker": {"data_classes": [], "worker_principal_ids": ["<local-worker-principal-id>"]},
    "salad": {"data_classes": [], "worker_principal_ids": ["<salad-executor-principal-id>"]}
  }
}
```

## Example: self-hosted server plus Salad

An example configuration, not a shipped default. The deployment runs the control
service, the Salad guardian and the Salad executor on a self-hosted server that runs
no experiment containers, runs CPU stages on a second self-hosted server, and approves Salad for
all three classes. Each id is a placeholder for an enrolled principal.

```json
{
  "schema": "tessera.provider-trust.v1",
  "providers": {
    "local-docker": {
      "data_classes": ["public", "private_code_approved", "private_derived"],
      "worker_principal_ids": ["<local-worker-principal-id>"]
    },
    "self-hosted": {
      "data_classes": ["public", "private_code_approved", "private_derived"],
      "worker_principal_ids": ["<self-hosted-worker-principal-id>"]
    },
    "salad": {
      "data_classes": ["public", "private_code_approved", "private_derived"],
      "worker_principal_ids": ["<salad-executor-principal-id>"]
    }
  }
}
```

Approving a third-party provider for `private_derived` means outputs of earlier stages
and any non-public input may leave the deployment for that vendor. Decide it per
provider, and narrow per campaign with a stage's `send_classes` or the spec's
`forbid_providers` when a campaign needs less. A stage's `send_classes` and the licence-label
rule apply at release and again at ticket issue and at every pull; they only narrow this
setting, never widen it.

## Stable executor (route setups that mint principals)

This is how the Salad executor principal above (`<salad-executor-principal-id>`) is
created and kept: one enrollment provides the single worker principal listed under
`salad`. Both Salad paths use it. Single-route setups pass `--executor-dir`, and the
campaign Salad stage executor (`python -m tessera_control.campaign_salad_executor`, see
[campaigns](campaigns.md#salad-gpu-stage-executor)) names the enrollment's
`DIR/worker-token` as its worker credential file. List one principal, not two. A
long-running campaign executor keeps working across renewals (the token does not change);
if a renewal is missed the credential lapses, the executor exits 2 (credentials refused,
do not restart), and the enrollment must be rotated and the config pointed at the new
directory.

A trust entry names worker principals, so a setup that mints a new worker principal for
every route (as `scripts/salad_live_setup.py` does by default) can never receive
`private_code_approved` or `private_derived`. Enroll one long-lived executor per provider
instance instead, list it once, and let each route reuse it:

```sh
# Once, as the local owner. Prints identifiers and times only, never the token.
.venv/bin/python scripts/executor_enrollment.py enroll --state-dir STATE \
    --owner-header STATE/owner.header --executor-dir NEW_0700_DIR \
    --provider salad --region salad-us [--days 7]
# Put the printed worker_principal_id under "salad" in the trust file, then reload
# (or restart serve with --provider-trust). Each Salad route then passes:
.venv/bin/python scripts/salad_live_setup.py ... --executor-dir DIR
```

The same tool enrolls the principal behind a CPU poller: `--provider local-docker` (the
control host's own `tessera-worker development-poll`) or `--provider self-hosted`, with
any `--region` label such as `local`. Point the poller's `credential_file` at
`DIR/worker-token` and list the principal under that provider. The lifetime and renewal
rules below apply unchanged, so schedule `renew` for the poller too. A Salad route setup
refuses an enrollment made for another provider.

To run one campaign across providers (CPU stages and Salad stages together), enroll the
second executor into the project the first one made, with
`--existing-project PROJECT_ID` (the `project_id` in the first `executor.json`):

```sh
.venv/bin/python scripts/executor_enrollment.py enroll --state-dir STATE \
    --owner-header STATE/owner.header --executor-dir NEW_0700_DIR \
    --provider salad --region salad-us --existing-project PROJECT_ID
```

Only the local owner can do this. An unknown or unavailable project is refused before
anything is issued, and repeating the identical command in the same directory prints the
same record. Joining a project grants no data class: list the principal under its provider
as usual ([ADR 0056](decisions/0056-executor-enrollment-into-an-existing-project.md)).

- **What it holds.** `DIR` (0700) keeps the 0600 `worker-token`, the enrollment request
  journal, one journal per renew/revoke under `operations/`, and `executor.json`
  (provider, project, principal, credential and registration ids, registration labels,
  `enrolled_at`, `not_after`, `credential_expires_at`, `revoked_at`). Routes share the
  executor's project; a route's config points at the executor's token file instead of
  writing its own. Setup checks the directory like every other private path (absolute,
  0700/0600, no symlinks) and, before any write, that control still authenticates the
  token as exactly the recorded principal, project and credential with enough time left.
- **Lifetime, and where it is enforced.** Control issues worker credentials at most
  24 hours ahead and refuses any renewal (from this tool, a script or the API) whose
  expiry is more than seven days after the credential was issued, never authenticates a
  worker credential past that point, and never revives an expired credential. `renew` extends the same principal and token by up to 24 hours and
  never past the record's `not_after` (at most seven days after enrollment). A route
  setup also refuses, against control's own credential row, a credential issued more than
  seven days ago or expiring after `not_after`. A missed daily renewal therefore means
  rotation; schedule `renew` well inside each 24 hours (example units:
  [`deploy/executor/`](../deploy/executor/README.md)). The token is long-lived: while live it can lease any
  active grant issued to the executor principal, so keep the directory on the
  guardian host under the service user only. A route setup refuses an
  executor whose credential lapses before the route's authority window ends, a revoked
  one, one past `not_after`, one enrolled for another provider, and one whose Salad
  registration labels do not match the route's country.
- **Rotation.** Enroll a new directory, put its principal in the trust file (alongside
  the old one while routes still run), reload, then `revoke` the old directory (revokes
  the credential at control and marks the record) and remove the old principal from the
  trust file with another reload. Rotate before `not_after`, and whenever the shared
  project reaches its cap below. Revoke the old directory only after its routes have
  ended: once its worker credential is revoked, those routes can be cancelled only by a
  human, because the executor's worker credential is their only non-human cancel authority
  ([ADR 0075](decisions/0075-executor-worker-credential-cancels-own-route.md)).
- **No cancel credential.** A route no longer mints a cancel credential (the former
  human session, which reached the whole shared project). The live check cancels its own
  route with the executor's worker credential, which can stop only routes it dispatches and
  is renewed by `executor_enrollment renew`
  ([ADR 0075](decisions/0075-executor-worker-credential-cancels-own-route.md)).
- **Shared-project bounds.** Live routes still end with their execution grant
  `cancel_requested` (nothing reconciles them until ADR 0041 settlement), so each keeps
  holding its cap. Each route raises the project limit by exactly its own cap over the
  held and settled exposure and admits one more grant than are unresolved; the grant's
  own cap still bounds the route and the Salad guardian's single-operation orphan fence
  remains the concurrency fence. Control caps project concurrency at 8, so one
  enrollment serves at most 7 unreconciled routes, then setup asks for rotation. A setup
  that fails after issuing its grant leaves that grant active in the shared project;
  once it expires unattempted the owner can clear it with
  `POST /execution-grants/{id}/reconcile-expired-unattempted` (or the related
  never-claimed/never-submitted routes), which frees its hold and its concurrency slot.
- **Revocation.** `revoke` at any time; a revoked executor's token no longer
  authenticates and every later route setup refuses it. If `enroll` stops after the
  credential was issued (no `executor.json` yet), revoke it with `POST
  /credentials/{id}/revoke` using the id in `DIR/journal/*executor-credential.response.json`,
  then delete the directory. Re-running `revoke` is safe: control's revoke is idempotent.
- **Build digest.** The registration's `approved_build_digest` (descriptive metadata) is
  recorded at enrollment; route summaries flag when the installed code differs. Rotate if
  the registration should describe the new build.

## Provider registration reports admission, never changes it

`tessera providers add|update` and the matching API and MCP tools
([ADR 0081](decisions/0081-validated-provider-registration.md)) validate and write the
`--campaign-providers` file only. With `--provider-trust` (or the running service) a result lists the
data classes this setting admits for the entry's worker and, when `private_derived` is not admitted,
says what to merge into the entry here. A `self-hosted` instance is trusted under its own `id`, so the
readout and hint use that id. The readout is evaluated against the providers registered now; a provider
added by registration registers at the next start, and under `all_registered_providers` the result adds
an `after_restart` note (every class if the worker is already listed under that id, otherwise public
only). Nothing in registration edits this file. Because registering a provider id can make it a
destination under that mode, the API lets only the deployment's root owner change deployment-wide
entries (`local-docker`, `self-hosted`, plugins).

## Reading the setting

Reads, each returning the setting, its digest and its load time: `GET
/admin/provider-trust` (a human, or an external agent with the `provider_trust:read`
scope), `tessera admin provider-trust show` and the MCP tool `tessera_get_provider_trust`
(registered only when the live scopes include `provider_trust:read`; never give that scope to an
OpenCode harness credential, whose pinned catalog would block with `catalog_inventory_changed`). A human reloads the
configured file with `POST /admin/provider-trust/reload` or `tessera admin provider-trust
reload`; no write route exists. A changed digest records `provider_trust.policy_changed`
and a refused reload `provider_trust.reload_refused`, both in `operator_audit_events`.
Campaign receipts and run receipts carry `trust_policy_digest`, `admission_basis` and
`trust_provider_id` for each admission.
