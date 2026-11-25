# ADR 0081: validated registration of providers and profiles

Status: accepted and implemented. No schema change. Builds on ADR 0040 (providers, plugins and the
operator file), 0047 (agent-routed data destinations), 0062 (CPU-only Salad profiles) and 0065
(operator-enabled MCP tool). The Salad profile section describes whatever fields the profile model
has.

## Context

Registering a provider instance or a Salad profile meant editing the `--campaign-providers` file by
hand on the server: no schema check before `serve` refused to start, no record of who changed what,
and nothing stopping a secret or an unknown plugin from landing in it.

## Decision

1. **One validated operation.** Add, update and list over the operator file live in one module
   (`provider_registration`), shared by the `tessera providers` command, the control API
   (`GET` and `POST /admin/campaign-providers`) and the MCP tools. They differ only in who may call
   them and where the audit goes.
2. **Vendor-neutral.** A provider module, built in or a plugin, returns `ConfigSection` objects from an
   optional `config_sections()`: where its entries live in the file, the key field, its closed model, a
   whole-document check, a non-secret summary and the worker principal an entry grants to. A plugin
   may also expose `validate_config(section)`. Adding a vendor edits neither the registration module nor
   the runner core. A generic `plugin` section configures a plugin that exposes no section; a plugin
   that is not installed is refused.
3. **Validation before any write.** The entry must (a) belong to a section a loaded provider declares
   (an unknown section, provider or plugin is refused: nothing is registered for a destination Tessera
   cannot name); (b) pass that provider's closed model: for a Salad profile, an image allowlist of
   `sha256:` digests, the resource class (a GPU class or CPU rates), resource shape, cent ceilings and a
   credential *reference*; (c) leave the whole resulting document valid (unique names, reserved ids,
   schema); (d) hold no secret: a field name containing a secret word (`key`, `token`, `secret`,
   `password`, ...) or a credential-shaped or high-entropy value is refused, and `credential_reference`
   is a name, not a value. In the API path the entry is also checked against the control database, as
   item 8 says.
4. **Safe write and audit.** The change is made under an exclusive lock, refused if the file no longer
   has the `expected_digest` the caller listed, and written atomically with owner-only (0600)
   permissions. A hash-chained audit file (`<file>.audit.jsonl`, each record carrying the previous
   hash) is appended after the write. The API path also records an operator audit event
   (`provider_config.changed`, or `provider_config.refused`); writes are human-only and idempotent.
   Which human may write what is item 8.
5. **No live reload.** `serve` reads the file at start, so a change applies at the next start. Nothing
   re-registers a provider in a running process.
6. **Provider trust stays operator policy.** A result reports what the current provider trust setting
   admits for the entry's worker and, when `private_derived` is not admitted, the exact trust-file entry
   to add. Registration never edits the trust file (ADR 0040).
7. **Non-goals.** Removing an entry, building images, editing the trust file, and re-registering
   providers in a running service.

8. **Authority over the API write path.** Reads (`GET`) are for a human or an agent with
   `provider_trust:read`. Writes are split by the section's `scope`:
   - *Organization-scoped* entries (`salad-profile`) belong to the organization named in them. A human
     of that organization may add one only for its own organization and project and a worker principal
     registered in that project, and may change only a profile its organization already owns. Another
     organization's profile name answers "That profile name is not available." for add and update alike,
     so a profile cannot be taken over. Its existence can still be inferred: profile names share one
     namespace across the deployment, so a free name validates (a dry-run `add` says so) and a taken one
     is "not available", and an `update` of a name nobody holds gets the missing-entry refusal. Hiding
     that would need names namespaced per organization, which this does not do.
   - *Deployment-wide* entries (`local-docker`, `self-hosted`, `plugin`, and any section a plugin
     declares) decide which worker receives every organization's grants. Under ADR 0047's
     `all_registered_providers` mode, registering a provider id also makes it a destination that can
     receive every data class from the next start. They can be changed through the API only by the
     deployment's root owner (the `local_owner` principal with no parent of the deployment's
     bootstrap organization, `LOCAL_ORG_ID`; another organization's root owner is not one). The
     offline command is for whoever owns the file.
   - Every worker principal an entry names is checked against the database on the entry as it would be
     stored: organization-scoped entries must name a worker of the entry's project in the caller's
     organization; deployment-wide entries must name a registered worker principal of the owner's
     organization. A plugin section names no worker, so it has no such check.
   - `GET` hides worker principal ids on deployment-wide entries from callers other than the root owner.
9. **A self-hosted instance is its own provider.** It registers, and is trusted, under its own `id`, not
   under `self-hosted`. The list shows that id (and each entry's `scope`), and the trust readout and hint
   use it. The readout is evaluated against the providers registered now; a provider added here
   registers at the next start, so under `all_registered_providers` the result adds an `after_restart`
   note saying what the worker would receive then (every class if it is listed under that id, otherwise
   public only).

### Hardening (from review)

- **Plugins.** The API registers only a plugin that is an installed entry point or already imported
  in the service; it never imports an arbitrary module for a caller. The offline command, run by the
  file's owner, may also name a dotted module, as `serve` would. Either way the plugin must expose
  `register_from_config` (the check `serve` makes), so a written file always loads; a plugin that
  declares its own sections uses those, not the generic `plugin` section.
- **Paths.** A new file gets the checks `serve` applies on read before it is created: absolute, no `..`,
  no symlink in any ancestor, a parent only the owner can write. The lock and audit files are opened
  without following a symlink and forced to 0600; the lock wait is bounded (a busy file is a refusal,
  not a stalled service); a failed write leaves no temporary file; the result must fit what `serve` reads.
- **Audit.** The chain is verified before a change and a broken or corrupt chain refuses it, so a changed
  file is never left without a record. If appending fails after the write, the result says `written` with
  an `audit_error` rather than reporting a refusal for a change that happened.
- **Tenants.** The API checks the entry as it would be stored (an update's patch merged over the stored
  entry), refuses non-string ids, and lists only the caller's organization's entries plus those naming
  no organization (item 8 says which write each caller may make).
- **Retries.** Re-adding an identical entry is a no-op success, so a retry after a partial failure is safe.
- **Secrets.** Field names are matched as words (camel case split), a field about an amount
  (`max_tokens`) is not a secret, bearer tokens, JWTs and URLs with credentials are refused, and
  readable lowercase-hyphen names are not mistaken for tokens. A user-supplied field name is never echoed
  in a refusal. A 64-hex value is treated as a digest, so a hex secret is not caught: use a credential
  reference.
- **MCP.** A 409 on the registration route relays its reason to the model (operator text, not protected
  content); the write tool is not declared idempotent.

### Ownership is decided under the file lock

The API passes its organization and owner checks to `apply_change` as an `authorize` callback, which
runs on the document read under the file lock, so an offline edit cannot change who owns an entry
between the check and the write. The callback runs database queries while the file lock is held;
the order is always database transaction first, file lock second, and the lock is local and short,
so no other path waits on the database while holding it.

## Consequences

- A hand edit is no longer needed, and a malformed, secret-bearing or unknown-destination entry never
  reaches the file `serve` reads.
- The MCP tools (`tessera_list_providers`, `tessera_register_provider`) exist only when the operator
  enables `provider_registration_tools` (command line `--provider-registration-tools`); the API still requires a human for every write.
- A vendor ships its own section and validation.

## Limits

- The file must be one the operator already protects (owner-only, single link, no symlink): the same
  rule `serve` applies.
- Salad profiles supplied through `--salad-campaign-profiles` are not editable here; only entries in the
  `--campaign-providers` file are.
- Nothing is verified against a provider: validation is shape, policy and database consistency, not a
  live call.
- The database checks cover Salad profiles (organization, project, worker) and the worker principal of
  `local-docker` and `self-hosted` entries; a plugin section is checked only by its own
  `validate_config` and the secret scan.
- The file write and the database audit event are not one transaction. If the database step fails after
  the file is written, the change stands with no `provider_config.changed` event and no stored idempotency
  result. The hash-chained file audit always records the change, and a retry is a no-op.
- The service user must be able to write the `--campaign-providers` file and its directory (for the
  temporary file, the lock and the audit file). Otherwise an API write is refused cleanly. To keep the
  service read-only, leave it so and edit the file with `tessera providers` as its owner.

## Amendment: a launch refuses a stale operator config

"A change applies at the next start" stays true, and is now enforced rather than assumed. At start
`tessera serve` fingerprints the operator files it read (`--campaign-providers`,
`--salad-campaign-profiles`, `--provider-trust`, and the executor config named by
`--salad-executor-config`). Before a Salad stage is scaffolded and again before its route is approved
and started, the driver refuses (`operator_config_stale`) when any of them changed, was removed or
cannot be read. The executor keeps its own copy of the profiles, which can diverge from the served
file, so when the executor config is named, the profile control is about to use must also equal the
executor file's entry of the same name (`operator_profile_mismatch`). Nothing is re-read or
re-registered mid-process, so a run that already started is untouched; the operator restarts the
service to apply a change. Without `--salad-executor-config`, only the served files are checked.
Placement, trust, licence, budget and allowlist checks are unchanged.

## Tests

`tests/test_provider_registration.py` (sections, refusals, secrets, digest guard, atomic write, audit
chain, CLI), `tests/test_provider_registration_api.py` (human-only writes, idempotency, audit event,
database checks), `tests/test_mcp_provider_registration.py` (the operator-enabled tools).
