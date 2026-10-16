# Salad operator MCP configuration

This separate **human/operator-only** stdio MCP records Salad setup and optionally
checks read-only account/project access. Never add it to the research-agent MCP
configuration. The MCP does not deploy services, enroll credentials, approve research,
enable dispatch, create/start workloads, provision GPUs, or purchase credits.
The existing Kubernetes control and research MCP are unchanged.

## One-time setup

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
