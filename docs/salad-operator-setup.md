# Salad operator MCP configuration

This separate **human/operator-only** stdio MCP records Salad setup and optionally
checks read-only account/project access. Never add it to the research-agent MCP
configuration. The MCP does not deploy services, enroll credentials, approve research,
enable dispatch, create/start workloads, provision GPUs, or purchase credits.
The existing Kubernetes control and research MCP are unchanged.

## One-time setup

### Zero-project account setup

For an organization with no project, call `salad_configure(organization="luxai",
credential_reference="credential-ref:salad-management", credential_version=1)`.
Project and management host are optional at this stage. The CAS-protected private
record survives reconnect and credential expiry. It reports
`project_creation_requires_portal`, proposed slug `tessera-research`, and
`project_created:false`; **a proposed name is not an existing project**.

Follow the returned [official account setup instructions](https://docs.salad.com/general/tutorials/account-setup):
open the Salad Portal, select `luxai`, create the project (proposed name
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
/Users/zach/Code/tessera/.venv/bin/python -m tessera_operator.salad_enroll \
  --credential-directory /Users/zach/Code/tessera/.tessera/private/salad-operator/credentials \
  --reference credential-ref:salad-management --expires-in-hours 24
```

This is one-time private entry of the already-authorized key, not a request to
paste it into chat again. There is no chat-to-runtime secret handoff. The process
must run with the checkout installed (the local `.venv` is editable); elsewhere
install the package first. The command has no network effects and does not validate
the key. After 24 hours metadata inspection still works, but provider validation
requires a fresh private reference. Expiry is not provider key revocation. Existing
runtime workload/safety credentials are unchanged. Rotation of existing backend
entries remains a separately administered resolver operation. Python/getpass creates
immutable string copies; hidden entry and buffer wiping do not guarantee process
memory erasure. Keep terminal/session recording, tracing and dumps disabled.

### luxai scope and project selection

`salad_discover_projects(credential_reference="credential-ref:salad-management",
credential_version=1)` makes exactly one `GET /organizations/luxai/quotas` to
validate organization read access. Despite the tool's discoverability name,
`project_discovery` truthfully reports `unsupported_by_public_api`: the official
Salad public OpenAPI has **no organization/project enumeration or project-get
operation**. Do not guess names or call undocumented `/projects` endpoints.
Obtain the existing project name from trusted configuration or the luxai Portal;
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

## Normal use, exit and return

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

`salad_inspect()` reads `GET /admin/projects/PROJECT/records/RECORD/salad-setup`.
`salad_configure(organization="luxai", expected_revision=REV,
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
