# Runtime setup (explicitly configured trusted-owner composition)

The ordinary control CLI accepts an explicit runtime-owner configuration and its
independently approved digest. The ordinary research service accepts the same
owner-composition integration with separately configured planner/critic references.
Use the supported `tessera serve` CLI and `python -m tessera_agent.research_service`
module invocation shown below; there is no `tessera-research` console script. No
ambient entrypoint discovery, default plugin loading, or `tessera_mcp` service
entrypoint is implied by `python -m tessera_mcp.runtime_setup --help`.

These integrations do not start a model provider, install OpenCode, discover/copy
credentials, approve a grant, or turn diagnostics into execution permits. Strict
legacy v1 remains the default. Runtime admission v2 requires explicit configuration
and an independently approved owner/observer; it represents local enforcement, not
provider billing or entitlement.

For control bootstrap and migration, see [runtime operator setup](runtime-operator-setup.md).
Schema 55 was installed and checked in the historical `0.1.0rc2` candidate, but
that candidate predates the current CLI/factory composition changes. The wiring and
targeted source tests do not establish a fresh installed artifact or a production
owner implementation. Re-run applicable installed checks against the next frozen
candidate; do not infer support from source discovery alone.

## Prerequisites and owner contract

The following command forms are supported; every path and digest is a placeholder,
not a ready-to-run credential/configuration. Use the installed environment and only
owner-approved private files:

```sh
tessera serve --state-dir /path/to/private/control-state --port 8123 \
  --runtime-owner-config /path/to/private/control-owner.json \
  --runtime-owner-sha256 <independently-approved-64-lowercase-hex>

python -m tessera_agent.research_service \
  --config /path/to/private/research-references.json \
  --state-dir /path/to/private/research-state \
  --runtime-owner-config /path/to/private/research-owners.json \
  --runtime-owner-sha256 <independently-approved-64-lowercase-hex>
```

The angle-bracket digest placeholder is intentionally not a valid SHA-256 value.
Do not run these examples as-is.

Use the public versioned OpenCode observer source/package and its release inventory
to reconstruct an installation. Its independently approved signed public manifest
must describe full installed build, SDK, harness schema, effective config, loaded
skill, complete typed catalog/tool mapping, binding and terminal implementation.
Obtain approval for its exact SHA256 and an opaque approval reference OUTSIDE the
agent. Merely hashing the release files is not loaded-process proof.

For the harness binding, call `generate_binding` then `bind_runtime_profile` with
the actual issued policy reference, admission policy digest and profile digest.
This adds nested `runtime_admission.version:tessera-runtime-admission-v2`; the
enclosing manifest version stays `tessera-opencode-v2-binding-v1`. The owner must
independently approve and measure the final manifest and loaded configuration,
skill, full catalog/tool mapping and process linkage. Static generation is not
installation or admission, and a later profile change requires fresh approval
and enrollment for the changed pins. `prepare-profile` validates/copies setup
references; it does not generate or install this harness binding.

Both commands require the paired owner-config and owner-digest flags. The digest
must be independently reviewed and approved; computing a digest from a
model-generated file is not a trust decision. Use owner-private regular files and
replace every angle-bracket value in the examples with real, independently approved
references before invoking a command. The JSON sketches below are documentary and
are not valid configurations.

For control, provide a `tessera-runtime-owner-composition-v1` file with exactly
`version`, `owner`, and `expected_readback` keys. `owner` binds the adapter file and
its approved SHA-256, private resource-reference file, signed-manifest file and
approved manifest digest/reference. `expected_readback` binds the same approved
manifest and approval, signature/process references, subject/publisher credential
IDs, opaque owner/session references, and the full instance/build/SDK/schema/config/
skill/catalog/tool-mapping/binding/terminal pin set. Unknown/duplicate fields,
unapproved or changed pins, symlinked/non-private owner files, and one-sided flags
fail closed before service start. Digest equality alone does not establish signature
authenticity.

For research, provide a `tessera-research-owner-composition-v1` file containing
separate planner and critic config-file references and independently approved
digests. Each role file uses the control-owner schema. Role credentials and private
resources remain distinct; role matching uses the existing role references, not list
order. Existing role references must explicitly opt into admission v2 with approved
policy reference/digest and no legacy guard. With no flags, legacy v1 remains the
strict default. V2 without an approved observer refuses before role-secret reads.

Control-owner JSON shape (all values are placeholders and the file is not valid for
admission until populated and approved):

```json
{
  "version": "tessera-runtime-owner-composition-v1",
  "owner": {
    "adapter_file": "/path/to/private/approved-owner.py",
    "adapter_sha256": "<reviewed-source-digest>",
    "resource_reference_file": "/path/to/private/owner-resources.json",
    "signed_manifest_file": "/path/to/public/signed-manifest.json",
    "approved_manifest_sha256": "<approved-manifest-digest>",
    "approval_reference": "<operator-approval-reference>"
  },
  "expected_readback": {
    "manifest_sha256": "<approved-manifest-digest>",
    "signature_reference": "<signature-trust-reference>",
    "approval_reference": "<operator-approval-reference>",
    "process_link_reference": "<approved-process-reference>",
    "publisher_ref": "<public-reference-digest>",
    "opaque_owner_ref": "<opaque-reference-digest>",
    "session_ref": "<session-reference-digest>",
    "subject_credential_id": "<subject-reference>",
    "publisher_credential_id": "<publisher-reference>",
    "pins": {
      "instance": "<instance-reference>", "build": "<digest>",
      "sdk": "<digest>", "harness_schema": "<digest>",
      "effective_config": "<digest>", "loaded_skill": "<digest>",
      "typed_catalog": "<digest>", "tool_mapping": "<digest>",
      "binding": "<digest>", "terminal_implementation": "<digest>"
    }
  }
}
```

Research-owner root shape:

```json
{
  "version": "tessera-research-owner-composition-v1",
  "planner": {"config_file": "/path/to/private/planner-owner.json",
              "approved_sha256": "<approved-planner-config-digest>"},
  "critic": {"config_file": "/path/to/private/critic-owner.json",
             "approved_sha256": "<approved-critic-config-digest>"}
}
```

The supported owner-factory API is
`open_runtime_owner(config: RuntimeOwnerConfig, *, state_dir: Path) -> RuntimeOwnerRegistration`
from `tessera_control.runtime_composition`. An authentic owner implementation must
use `InstalledOwnerCapability`/`InstalledReadback`, verify the real approved
signature, loaded process, full pins, and synchronous private-resource linkage, and
re-check them before/after control reads and observer creation. It must enforce by
raising on unknown or stale evidence, not return a Boolean or echo expected JSON.
The repository supplies the composition contract and wiring, not an authentic
private owner adapter, signing trust root, live process proof, or production
credential handles. Synthetic test fixtures are not production enrollment evidence.

The owner implementation consumes human, subject and publisher credentials only
through three distinct owner-private file references (regular files, current UID,
mode 0600, no symlinks or discovery). Do not copy provider credentials. The human
credential must already have real server authority; no `--role human` bootstrap
exists. Subject and publisher identities are enrolled separately through existing
authenticated server administration. Missing the authentic owner adapter, its
trusted signature/process evidence, or private resource handles remains a production
prerequisite; config alone does not create or authorize them.

## Private reference profile and typed operator inputs

Use a mode0600 JSON file with the exact `SetupProfile` schema. Fields:
`version:tessera-runtime-setup-v1`, HTTPS `origin`, `audience:control|worker`,
`destination`, `project_id`, `human_credential_file`, `subject_credential_file`,
`publisher_credential_file`, exact `ca_file`, `owner` containing `adapter_file`,
`adapter_sha256`, `resource_reference_file`, `signed_manifest_file`,
`approved_manifest_sha256`, `approval_reference`. Store references only, never
account IDs, credential bytes, prompts or provider response bodies in these records.

Start with `policy`, `enrollment_request`, `resolve_request` omitted. Independently
approve the installed owner first. A policy input is the full existing
`IssueRuntimePolicy` wire schema: explicit model, allowed actions/native phases,
expiry, call/exposure bounds, per-call exposure, financial/evidence/operation age,
v2 assurance and explicitly-approved unknown provider facts. There are no financial
defaults. Choose these bounds as a human; generating JSON is not approval.

```sh
python -m tessera_mcp.runtime_setup --profile /absolute/setup.json issue-policy \
  --input /absolute/human-policy.json --key original-policy-command
```

Save the public returned policy in `policy`. Fill `enrollment_request` using
`IssueRuntimeEnrollment`: returned policy ref/revision, distinct server credential
IDs, publisher audience/ref, opaque owner/session refs, destination/role/context
ref/digest, existing bounded parent authority grant ref/revision/epoch, measured full
pins, expiry. Owner readback MUST match pins and opaque refs. No invented owner IDs.

```sh
python -m tessera_mcp.runtime_setup --profile /absolute/setup.json prepare-profile \
  --output /absolute/prepared.json
python -m tessera_mcp.runtime_setup --profile /absolute/prepared.json enroll \
  --key original-enrollment-command
python -m tessera_mcp.runtime_setup --profile /absolute/prepared.json operator-financial-ref \
  --input /absolute/operator-financial.json --key original-financial-command
```

Financial input is exactly `IssueRuntimeFinancialAssertion`: returned enrollment
ref/revision, policy ref/revision, ORIGINAL actual observed_at, valid_until, explicitly
observed purchased_credit_balance0, auto_top_up_off true, automatic_reload_off true.
Never refresh the observation timestamp merely to pass max-age, infer balances,
copy an issuer or treat unknown settings as true. Server derives authenticated
issuer/owner/project/destination and immutable issuance time. Unknown local facts
deny; provider entitlement/billing remain unknown, never included_oauth_verified.

Fill `resolve_request` with enrolled ref/revision, financial ref and current parent
grant revision/epoch. All stored records are public projections, not capabilities.

```sh
python -m tessera_mcp.runtime_setup --profile /absolute/prepared.json doctor --key doctor-1
python -m tessera_mcp.runtime_setup --profile /absolute/prepared.json current-resolve --key resolve-1
python -m tessera_mcp.runtime_setup --profile /absolute/prepared.json revoke \
  --kind financial --ref ACTUAL_REF --revision 1 --key revoke-1
python -m tessera_mcp.runtime_setup --profile /absolute/prepared.json reconcile --ref ACTUAL_OPERATION_REF
```

Human issuance/revocation uses actual `/projects/{project}/runtime-*` and revoke
routes; current resolution/operation readback use actual publisher routes and its
enrolled control or `/worker` audience. No redirects, retries, raw server error/header
printing or owner-token proxy. `reconcile` is ONLY durable operation readback and
explicit human-review guidance; there is no reconcile endpoint, forced settlement,
refund, new continuation or retry admission. `doctor` current resolution is not an
invocation and cannot prove private resource enforcement at a future primitive.

Denial exits2 with a fixed diagnostic. Inspect approved references and obtain the
missing server/private owner binding; do not disable admission or repeat secrets.
Successful local tests exercise actual temporary HTTPS/auth/SQLite/Store with a
labelled synthetic installed-owner/signature callback and synthetic upstream facts.
They are not production installed/provider/off-host acceptance. Release clean-install,
full loaded runtime/catalog and genuine secure owner handoff remain separate gates.
