# Salad operator MCP configuration

This separate **human/operator-only** stdio MCP records Salad setup and optionally
checks read-only account/project access. Never add it to the research-agent MCP
configuration. The MCP does not deploy services, enroll credentials, approve research,
enable dispatch, create/start workloads, provision GPUs, or purchase credits.
The existing Kubernetes control and research MCP are unchanged.

## One-time setup

### Zero-project account setup

For an organization with no project, call `salad_configure(organization="example-org",
credential_reference="credential-ref:salad-management", credential_version=1)`.
Project and management host are optional at this stage. The CAS-protected private
record survives reconnect and credential expiry. It reports
`project_creation_requires_portal`, proposed slug `tessera-research`, and
`project_created:false`; **a proposed name is not an existing project**.

Follow the returned [official account setup instructions](https://docs.salad.com/general/tutorials/account-setup):
open the Salad Portal, select `example-org`, create the project (proposed name
`tessera-research`), and do not create/deploy any workload or change billing.
The documented public API/SDK has no project-create/list operation; Tessera cannot
truthfully automate this Portal step through an invented endpoint. Resume with
the actual name via `salad_discover_projects(candidate_project=..., ...)`, then
save it with `salad_configure(..., project=..., expected_revision=...)` after a
successful documented container-collection read. No project is asserted created
by merely recording its name.

`salad_readiness()` separates account/project, deployment/custody, billing and
control integration; it is offline unless explicitly asked to validate provider
access. Zero-cash enforcement is unverified, so execution stays disabled even if
account/project reads pass. The local operator record is **not consumed by the
running Kubernetes control**. Its default target policies remain empty; an
authenticated integration bridge is separate work. Never duplicate authority
SQLite state or restart the fenced source as a shortcut. Guardian deployment and
management-host selection are not prerequisites to this nonbillable account setup.

Install the existing `mcp` optional dependency. Select the existing Salad
organization/project and the trusted Linux management host; the host is recorded
metadata, not an SSH destination or proof of a deployment. No target is inferred.
Provision an absolute owner-private mode-0700 operator state directory and a
separate mode-0700 credential directory. Symlinks in either path are forbidden;
use canonical paths (on macOS, `/private/...`, not the `/var` alias).

The trusted runtime uses the existing `PrivateFileSecretResolver` backend:
version-pinned `credential-ref:<opaque-name>`, `salad_management` purpose,
expiry/revocation checks, owner-private mode-0600 regular single-link files.
An authorized administrator must supply the existing secure backend reference.
This MCP never accepts or enrolls a raw key. Do not put keys in chat, command
arguments, environment, configuration, MCP arguments, logs or Git. A reference is
not inherited by a child process unless the actual private backend is available.
The secret remains in the trusted operator process during explicit validation;
this is not a workload credential or remote deployment custody mechanism.

### Private local enrollment (human terminal only)

The separate command below accepts the existing management key through a hidden
terminal prompt, **not chat**. No key may be supplied as an argument, environment
variable, stdin pipe or MCP request. It refuses non-terminals and echo fallback,
validates private paths, and creates version 1 only. Existing references are never
overwritten, even by concurrent entry. It prints only reference/version/expiry.
Directory preparation is separate; local setup already prepared these mode-0700
directories. From this checkout run this exact command in a local terminal:

```sh
$REPO/.venv/bin/python -m tessera_operator.salad_enroll \
  --credential-directory $REPO/.tessera/private/salad-operator/credentials \
  --reference credential-ref:salad-management --expires-in-hours 24
```

This is one-time private entry of the already-authorized key, not a request to
paste it into chat again. There is no chat-to-runtime secret handoff. The process
must run with the checkout installed (the local `.venv` is editable); elsewhere
install the package first. The command has no network effects and does not validate
the key. After 24 hours metadata inspection still works, but provider validation
requires a fresh private reference. Expiry is not provider key revocation. Existing
runtime workload/safety credentials are unchanged. Rotation uses the same module's
`rotate` and `renew` subcommands, below. Python/getpass creates
immutable string copies; hidden entry and buffer wiping do not guarantee process
memory erasure. Keep terminal/session recording, tracing and dumps disabled.


### Rotating or renewing an enrolled key (owner terminal only)

The management credential has a fixed lifetime; an expired one makes Salad runs fail (see
[credential health](credential-health.md), which reports and refuses it up front). Run these as the user
that owns the private 0700 credential directory; they have no API or MCP route.

```sh
# the next version of an existing, unrevoked reference (hidden prompt; add --secret-stdin to pipe it)
$REPO/.venv/bin/python -m tessera_operator.salad_enroll rotate \
  --credential-directory DIR --reference credential-ref:salad-management --expires-in-hours 24
# the same key, a later expiry (at most 24 hours per call)
$REPO/.venv/bin/python -m tessera_operator.salad_enroll renew \
  --credential-directory DIR --reference credential-ref:salad-management --expires-in-hours 24
```

`--secret-stdin` takes one pipe or file on stdin (never a terminal): at most 4096 printable ASCII bytes,
one trailing newline stripped. The key is never an argument or environment variable and is never printed;
output is the reference, previous version, new version and expiry. Afterwards set the profile's
`credential_version` to the new version and restart the control, guardian and executor services.
### example-org scope and project selection

`salad_discover_projects(credential_reference="credential-ref:salad-management",
credential_version=1)` makes exactly one `GET /organizations/example-org/quotas` to
validate organization read access. Despite the tool's discoverability name,
`project_discovery` truthfully reports `unsupported_by_public_api`: the official
Salad public OpenAPI has **no organization/project enumeration or project-get
operation**. Do not guess names or call undocumented `/projects` endpoints.
Obtain the existing project name from trusted configuration or the example-org Portal;
then pass `candidate_project` to the same tool. It makes one documented container
collection GET for that exact candidate and returns only read-access status,
never quota/container bodies. It does not save configuration or select a host.
Failures return closed categories only: credential rejection, access denial,
scope-not-found, rate limit, TLS/DNS/timeout or generic provider failure. No raw
error bodies, headers or exception messages are exposed. An access denial is not
permission to try another identity or guess alternate organization/project names.
TLS uses the installed `certifi` Mozilla CA bundle (already required by `httpx`)
with certificate and hostname verification enabled, including Python installations
whose default system CA paths are absent. No caller-supplied CA or TLS bypass exists.
See [official scope guidance](https://docs.salad.com/agents/container-engine/discover-scope-and-preflight).

Start the human operator server using explicit **paths only**:

```sh
.venv/bin/python -m tessera_operator.salad \
  --state-directory /PRIVATE/operator-state \
  --credential-directory /PRIVATE/salad-credentials
```

The packaged entrypoint is `tessera-salad-operator`. Keep this process separate
from the research MCP. Stdio process access is operator authority; there is no
network listener and no additional human-approval mechanism invented here.

### Explicit same-key private-reference renewal

The trusted backend administrator may call `PrivateFileSecretResolver.renew`
under current explicit human authorization, with `expected_version` and an
absolute expiry no more than 24 hours ahead. This is a server-only administrative
method, not an MCP tool, renderer action, access-lease refresh or provider key
rotation. It preserves the private key and `salad_management` purpose atomically,
increments the version, rejects stale versions and revoked records, and returns
metadata only. Ordinary `resolve` still rejects expired references. Persist the
non-secret before/after version and authorization receipt, then CAS-update the
operator configuration's version pin; inspect metadata before retrying an uncertain
renewal. Renewal grants no control credential, delegation approval or spending. The `renew` subcommand of `tessera_operator.salad_enroll` (above) is the supported way to call it.

## Normal use, exit and return

### Billing declarations are not execution admission

Both local configuration and authoritative Salad configuration accept optional
`billing_policy` metadata. Omission preserves an existing policy during operator
revision; explicitly send `{"mode":"credits_only"}` to replace it with the legacy
mode. Legacy records with no policy retain their serialized configuration shape.
`aggregate_capped` requires a strict integer `ceiling_cents` from 1 to 5000.
Nullable strict integer `prior_cost_cents`, `reserved_cents`, `ancillary_cents` and
`shutdown_cents` distinguish unknown amounts from declared zero; their known sum
must not exceed the ceiling. An optional timezone-aware `expires_at` is projected
as current or expired, not renewed automatically. Unknown or even fully declared
accounting remains unverified. The proposed 1000-cent shutdown and 500-cent
ancillary reserves are declarations, not measured liability guarantees.

Readiness reports the policy and its declaration/expiry status, but always leaves
ledger/enforcement verification and execution disabled. It does not claim a
provider hard cap. Atomic experiment-wide reservations/settlements, native Salad
execution grants, guardian/custody evidence and exact protocol acceptance remain
separate prerequisites; policy metadata cannot substitute for any of them.

Schema-57 source implements a separate control-DB experiment parent, not an upgrade
of those declarations. An existing human credential can record a USD authorization
at `POST /experiments/{id}/budget/authorization`; unknown prior costs, pending
exposure or ancillary/shutdown bounds remain unknown and block admission. Once
created, that authorization cannot be reset by a new design or billing policy.
Authorization is refused while an earlier attempt is non-terminal or prior settled/pending
costs are undeclared; terminal earlier attempts (e.g. a CPU baseline) are carried in
those declared prior bounds. Budgeted attempt admission reserves the native grant's full exposure in the same
transaction as its attempt, event and idempotency record, without a second project
debit. `GET /experiments/{id}/budget` reports the retained parent accounting.

Explicit human charge assertions use `POST /experiments/{id}/budget/charges` with
current revision, delivery identity and evidence reference. Duplicate delivery is
deduplicated, distinct real charges count separately, and late over-cap charges are
recorded and fence further admission. References are not independently verified
invoices. Settlement moves held amounts to settled; it does not release remaining
liability. Cancellation, expiry, stopped status and reconnect never refund holds.
This slice has no residual-release or authorization-renewal command.

Authorization is one-shot: `POST /experiments/{id}/budget/authorization` creates the
parent once and cannot be repeated or reset. Earlier CPU attempts must all be terminal
(succeeded, failed or cancelled) with declared prior settled and pending costs before a
budget can be authorized; finish them first. Any bound left unknown at authorization
keeps admission blocked until a human resolves it with
`POST /experiments/{id}/budget/bounds`, sending the current `expected_revision`, an
`evidence_digest`, a `source_reference` and one or more of `prior_settled_cents`,
`prior_pending_cents`, `ancillary_cents`, `shutdown_cents` as non-negative integers.
Only a currently unknown bound may be set, once; a known bound is never changed or
lowered (409). The resolution increments the revision, is recorded in the append-only
`experiment_budget_bound_resolutions` table, and fences the parent if known exposure
then exceeds the ceiling. Unknown costs are never inferred as zero.

Salad create/start and proxy claims require a current retained reservation. The
trusted server derives its identity and sends it on the authenticated safety-admin
channel. The safety service stores only an immutable recovery constraint, not a
copy of parent balances or authority to mint reservations. Stop-only operations
survive a fenced parent. Local tests exercise isolated HTTP/TLS services and fake
provider responses; these are OpenCode development checks, not actual research
agents, Tessera research records, deployed schema acceptance or paid GPU execution.

### Authoritative control metadata mode

The separate control-backed mode uses an existing Salad configuration record in
the active control database, not the private JSON file as authority:

```sh
.venv/bin/python -m tessera_operator.salad \
  --control-url http://127.0.0.1:18765 \
  --control-project-id TESSERA_PROJECT_ID --control-record-id ADMIN_RECORD_ID \
  --control-token-file /PRIVATE/delegated-operator-token
```

Do not combine these options with `--state-directory`/`--credential-directory`.
This mode exposes only inspect/configure/readiness metadata tools; it cannot read
Salad credentials or call the provider. It never falls back to local JSON, owner
authority, the research MCP credential, or an HTTP redirect. Non-loopback origins
require verified HTTPS. Token files are explicit private regular files; token
replacement is reopened for each independent operation. Configure pins that one
verified delegated credential across inspect, POST and readback, so mid-operation
file replacement cannot widen the write to owner authority. Server-side expiry,
revocation and delegation checks still run on every request; there is no global
credential cache. The client refuses a root-authority
projection: use a separate delegated operator credential, not an owner token.

One-time authority bootstrap is separate from normal use: an eligible human uses
existing current-control authority to issue a separate project-scoped control
credential (not an external-research credential); the operator requests its own
exact-record delegation for `configuration.read`, `configuration.propose` and
`readiness.read`; a different eligible root reviewer approves under existing rules.
Never create another identity to bypass self-approval. No bootstrap/approval is
performed by this MCP. Existing credential/delegation expiry is at most 24 hours;
expiry/revocation denies access but does not erase the record. A fresh authorized
credential/delegation handoff is necessary after expiry, not re-enrollment of the
Salad key. Existing agent credentials are not globally setup-only tokens; the admin
delegation restricts this record's administration surface.

### Desktop human-only issuance

Connect using your privately held **current root local-owner** credential; a
`human_session` cannot issue credentials under the current contract. Select the
Tessera project, open Administration, and choose **Issue and save private
credential…**. Choose 1–1440 minutes and a new bundle directory outside the
repository. The native confirmation names the endpoint, root, project and expiry.
Cancel sends no credential request. Tokens never appear in renderer forms or logs.

The bundle contains private `operator.control-token` and `intent.json` files.
Keep both: if the request outcome is unknown, reconnect with the same root identity
and select **Resume exact private bundle…**. It reuses the same token, command key
and expiry; never create a replacement blindly. An expired request requires human
inspection/revocation, not silent token regeneration. Do not commit or share the bundle.

Disconnect the root and Connect by explicitly choosing `operator.control-token`
(legacy `.header` files remain supported). As this operator, propose a Salad
configuration for `example-org` and its own delegation for the exact configuration record:
choose subject role **Operator / agent (control agent credential)**, all three
actions `configuration.read`, `configuration.propose`, `readiness.read`, and an
expiry no later than the child credential. The server enforces subject lifetime.
Record the returned principal ID and exact admin record ID; no proposal is automatic.

A human reviewer separately reconnects with a current root credential, inspects
the exact request and explicitly approves. The reviewer must differ from proposer
and subject. This principal-level check is not proof of independent real-world
human identity. Do not create another root or reuse a research credential to pass it.
Then use the separate operator MCP command above with the private token-file path
and exact Tessera project/record IDs; inspect, configure, read back, exit and reconnect.
The Salad provider key stays enrolled privately and is not entered again.

**Scope warning:** the issued credential is an ordinary project-scoped CONTROL
AGENT credential, with existing broader control-agent routes. Exact-record
delegation restricts administration only; it is not a universal action sandbox.
Issuance neither approves research nor enables provider execution or spending.

`salad_inspect()` reads `GET /admin/projects/PROJECT/records/RECORD/salad-setup`.
`salad_configure(organization="example-org", expected_revision=REV,
idempotency_key=KEY, proposed_project="tessera-research", project=null)` uses the
existing revision-checked admin `revise` command. Persist the non-secret key and
exact arguments before calling; reuse them on retry, never silently allocate a new
key or revision. The response is a fresh authoritative HTTP readback; no cached
success is returned when control is unavailable. Exit/reconnect rereads the same
record. Only organization/proposed/selected project metadata changes; existing
other configuration fields are preserved. No provider key or `credential-ref:` is
sent to control, and no fake admin secret binding substitutes for private custody.

The control projection requires both read scopes and is explicitly metadata-only:
Portal-required or selected-unverified project, provider validation not performed
by control, deployment/custody/billing unverified, execution disabled. A selected
project name is not a creation or provider-health receipt. This mode connects setup
metadata only; it does not inject runtime target policies or implement the trusted
execution/guardian bridge. The running Kubernetes image must contain these routes
before live use; local isolated HTTP tests are not live cluster activation proof.

### Private JSON mode

1. Call `salad_inspect`: returns current non-secret operator metadata and revision.
2. Call `salad_configure(organization, project, management_host,
   credential_reference, credential_version, expected_revision)` using only the
   selected metadata and existing opaque reference. Initial revision is null;
   updates require the exact inspected revision. Invalid/stale input is blocked.
   This writes only private `salad.json`; no credentials or provider calls occur.
3. Read back with `salad_inspect`. `salad_readiness()` is metadata-only and reports
   provider checks unrun, deployment/guardian custody unverified, execution disabled.
4. Only when provider validation is authorized, call
   `salad_readiness(validate_provider=true)`. It resolves the pinned private key
   and makes exactly one bounded GET of the selected project at
   `https://api.salad.com/api/public/organizations/ORG/projects/PROJECT/containers`.
   No endpoint overrides, redirects, retries or environment proxies are supported.
   Provider errors/bodies/headers and credentials are never returned. A successful
   `project_readable` means read access only—not independent deployment, quota,
   billing, GPU availability, guardian readiness or execution acceptance.
5. Exit normally. Reconnect to the same state directory and inspect the same
   configuration. No daemon, workload or supervision service is started/stopped.
   Credential expiry/revocation blocks explicit validation, not metadata readback.

## Boundary and reusable convention

This adds administrator-owned configuration, not a control-plane administration
approval or provider enablement switch. Existing
[Salad proxy admission](salad-proxy.md) and
[independent safety/custody](salad-safety-service.md) requirements still apply.
Deployment and genuinely independent service readiness remain separate prerequisites.
The cash ceiling remains zero: Salad documents overrun invoices even with manual
recharge. Existing credits are not a verified financial hard cap; execution stays disabled.

Other integrations can reuse the explicit startup paths, strict versioned typed
configuration, private fixed filenames, atomic compare-and-swap persistence and
separate default/offline versus explicit/live readiness pattern. Only Salad is
implemented; other provider schemas and operations need their own reviewed scope.
The directory is a single trusted operator identity, not a multi-tenant API.
SHA-256 revisions are stale-input guards, not grants or secret authentication.
Configuration values are non-secret operator metadata, not research context.

Targeted tests include isolated synthetic credentials/provider responses and a
real local stdio MCP handshake/configure/reconnect. They do not contact Salad or
establish deployed/live readiness. Python HTTP libraries copy header strings;
mutable-buffer wiping does not guarantee whole-process secret erasure. Protect
the operator process from dumps, debugging and request tracing.

Campaign Salad stages ([ADR 0038](decisions/0038-campaign-specs-and-platform-campaign-runner.md)
amendment) additionally need an owner-only campaign profile file
(`tessera.salad.campaign-profiles.v1`, no secrets) passed as
`tessera serve --salad-campaign-profiles FILE`. Each profile must list
`allowed_image_digests` (required, non-empty `sha256:` digests); a campaign stage whose
image is not on that list is refused at submit, approval and dispatch. See
[campaigns](campaigns.md).

### Route window arithmetic (`route_max_seconds`)

A stage is admitted only when its declared run plus the 1500 s
provisioning allowance (600 s allocation + 900 s image pull and input fetch) fits the profile's
`route_max_seconds`. With `route_max_seconds: 3600` the longest admitted run is 2100 s; a 3600 s
run needs `route_max_seconds` of at least 5100 (7200 is the schema ceiling and leaves 5700 s).
Raising a profile's `route_max_seconds` is a separate reviewed operator step: it changes the
profile's plan digest and any pre-approval bound to it. The platform's own 3600 s cap on
declared adapted runs is unchanged.

### CPU capacity fallback (`fallback_profiles`)

A CPU profile may name up to three other profiles of the same file that a stage running on it may
move to when the provider cannot place it, in order, after the stage's own approved
`alternate_profiles` are used up. It is operator configuration: no spec, approval or spec digest
changes. Every fallback must be listed as a profile of its own (a cheaper shape or another country
is a separate entry with its own `name`, `country`, shape, `cpu_rate` and ceilings), and must share
the listing profile's organization, project, worker, `salad_organization`, capability environment,
artifact and checkpoint destinations, monitor, watchdog, custody host and `stage_adapter` section.
Example entries (placeholders only; the credential is a reference, never a value):

```json
{"schema": "tessera.salad.campaign-profiles.v1", "profiles": [
 {"name": "cpu-2c-4g", "country": "US", "cpu_millicores": 2000, "memory_mb": 4096,
  "cpu_rate": {"vcpu_hour_usd": "<rate>", "memory_gb_hour_usd": "<rate>"},
  "fallback_profiles": ["cpu-2c-4g-alt", "cpu-4c-8g"],
  "max_run_cents": 50, "max_task_cents": 50, "...": "the shared fields below"},
 {"name": "cpu-2c-4g-alt", "country": "CA", "cpu_millicores": 2000, "memory_mb": 4096,
  "cpu_rate": {"vcpu_hour_usd": "<rate>", "memory_gb_hour_usd": "<rate>"},
  "max_run_cents": 50, "max_task_cents": 50, "...": "the shared fields below"},
 {"name": "cpu-4c-8g", "country": "US", "cpu_millicores": 4000, "memory_mb": 8192,
  "cpu_rate": {"vcpu_hour_usd": "<rate>", "memory_gb_hour_usd": "<rate>"},
  "max_run_cents": 50, "max_task_cents": 50, "...": "the shared fields below"}]}
```

"The shared fields" are every other required profile field. The load check requires these to be
identical to the listing profile's: `org_id`, `project_id`, `worker_principal_id`,
`salad_organization`, `capability_environment_id`, `artifact_destination`,
`checkpoint_destination`, `monitor_profile`, `watchdog_profile`, `custody_host` and
`stage_adapter`. The rest (`salad_project`, `disk_mb`, `route_max_seconds`, the credential
reference, `allowed_image_digests`) may differ; the stage's image must still be allowlisted on the
fallback and its credential usable, which is checked at move time like every other submit check.

The file is refused at load when a name is unknown, repeated, the profile itself, more than three,
of another kind (a GPU profile cannot list fallbacks), differs in a shared field, or when the
fallbacks form a cycle. A stage moves to a fallback only under the strict pre-start rule (an
attempt proven never started that settled at exactly zero; never after a possible start or any
spend), only within the stage's approved `retry.max_class_fallbacks` (a stage approved with 0 never
uses one), and only when the fallback's CPU rate over the stage's whole `max_duration_seconds` is
within the stage's approved `max_exposure_cents`, that hold is within the fallback's
`max_run_cents`, and every allowed attempt's hold within its `max_task_cents`. A fallback that
fails a check is skipped (named on the move); when none passes, the stage is held
(`profile_not_approved`, detail such as `fallback_over_hold`). Each move is a
`campaign.salad_profile_switched` event with `source: operator_fallback`.
