# Local safeguards and production prerequisites

Tessera remains a **source-only, single-owner developer alpha**, not a production
service. This page distinguishes implemented safeguards from launch prerequisites.
No configuration value, passing unit test, `/health/ready` response, approval, or
synthetic receipt promotes a workflow to live support.

This implements a narrow part of DESIGN.md §§5 and 6.2: bound intake before
authority admission and reject output plans the existing adapters cannot honor.
It does not change identity authority, provider activation or recovery semantics.

## What can be evaluated now

| Workflow | Available boundary | Not established |
|---|---|---|
| Local research ledger | Authenticated API, immutable plans, human decisions, study/experiment exports and correction history; [quickstart](quickstart.md) | Scientific validity or execution from a human record |
| Baseline/intervention integration | [Local API comparison](local-api-workflow.md) drives admission/dispatch with **fake external effects**; [ordinary collection](ordinary-worker-collection.md) describes bounded owned-target collection | General target isolation, provider/model use or unattended production execution |
| Local CPU development | Separate opt-in Docker/native-worker fixtures described in CONTRIBUTING.md | Pilot-grade persistent outputs or unattended production execution |
| Remote lab packaging | Explicit host/origin configuration and single-active control-plane manifests; separate opt-in target fixtures | Production identity, supported remote deployment, HA or laptop-independent acceptance |
| Provider/Astra seams | Deterministic provider-disabled paths, context/status contracts and fake receipts | Active model/provider, known billing, credential delivery or real provider termination |
| Private evidence retrieval | Explicit human custody release and bounded local reproduction bundle | Public publication, external transfer approval or scientific acceptance |

The [compatibility matrix](compatibility-matrix.md) records version/profile evidence.
Fixture availability is not a fresh passing live result. Desktop and recovery work
have their own acceptance gates; this slice does not change or validate them.

## Locally enforced intake/authentication prerequisites

- Remote configuration requires concrete trusted hosts and a matching HTTPS origin.
  Hosts cannot contain ports, userinfo, wildcards, paths or whitespace; origins reject
  userinfo (even empty), invalid/zero ports, query/fragment delimiters and control
  characters. A valid origin **does not install TLS or verify an ingress**.
- Existing credential checks retain separate control/worker audiences, tenant/project
  scope, expiry, parent revocation and live transactional authority checks. Human
  approval remains possession of a human bearer credential, not independent proof
  that a human was present. Bootstrap-owner rotation/session verification is absent.
- HTTP intake rejects duplicate Authorization headers and oversized bearer headers.
  Newly issued tokens must be 43–4096 URL-safe characters; the API rejects bearer
  values above 4096. Existing unusually long tokens must be replaced through the
  existing authorized credential process before upgrading; no credentials are
  automatically rotated or changed by this slice.
- Every HTTP request body is capped at **24 MiB**, including chunked/missing-length
  requests, before JSON parsing or command admission. This accommodates base64 for
  a 16 MiB artifact plus metadata. Declared overflow returns 413 without reading;
  actual overflow returns 413, malformed/duplicate/mismatched Content-Length 400,
  and exceeding the **30-second body-read deadline** 408. Disconnect admits nothing.
  Streaming responses are not buffered. These are per-request limits, not global
  memory, connection, request-rate, disk, or operation-duration limits.

Before production authentication is claimed, select and implement verified human
sessions (including reauthentication for privileged actions), organization membership
and revocation, owner/bootstrap credential rotation and recovery, and scoped workload
enrollment/renewal. Test issuer/audience/expiry and cross-tenant denial at every data
and event boundary. An identity-provider selection or a proxy authentication header
alone must not confer Tessera authority. No IdP, credentials or external service is
added here.

## Spending and provider limits

Current execution admission atomically reserves integer cents against both the
project limit and cumulative approved task budget. Currency must match; concurrency
is bounded; replay does not reserve twice. Held plus settled exposure cannot be
erased by lowering a limit, revising a plan, restarting, cancelling or expiring a
grant. Unknown termination remains unresolved. Attributed reconciliation may record
an overage rather than hide real costs; it is not a provider usage receipt.

These are **Tessera admission reservations, not a hard provider spending cap**.
The existing infrastructure-exposure acknowledgement explicitly discloses unmeasured
costs; zero reserved cents does not mean free compute. It does not authorize a model
call, credential delivery or a billing fallback. Provider defaults remain disabled,
usage/billing unknown, and no API-key/account/billing-mode fallback is enabled.

Before enabling one provider, require a pinned adapter/model/target, owner-bound
credential custody, a durable reservation before submission, stable lookup after a
lost reply, explicit limits on calls/tokens/retries/elapsed time, and attributable
usage and cancellation reconciliation. Bound minimum charges, cancellation lag,
storage and egress; reject a promised hard cap if the provider cannot enforce it,
or separately approve the disclosed unknown exposure. Interactive model calls need
their own metering/admission path: the compute-grant ledger does not cover them.
The [activation gates](provider-activation-and-collaboration.md) remain prerequisites,
not boolean switches to enable a provider.

## Storage limits and remaining capacity risk

- New executable plans for either current profile accept **1–16 MiB total declared
  output**, matching the existing package/quarantine ceiling. Oversized plans now
  fail validation before approval/reservation, rather than only at package creation.
  Historical records are not rewritten; previously oversized plans were already
  unpackageable and need a new revision and approval. No schema migration is added.
- Existing artifact intake enforces the lesser of the plan budget and 16 MiB across
  retained artifacts and upload reservations for a lease; chunks are at most 256 KiB.
  Input snapshots are separately capped at 16 MiB/1,000 files per capture.
- These limits **do not cap aggregate project/tenant/state-directory usage**. SQLite
  BLOBs, upload chunks, project-object copies, journals, exports, backups and history
  need capacity accounting; do not infer deduplication or deletion from placement.
  No automatic retention cleanup or project-wide disk quota is introduced here.

Production storage requires a capacity/reservation policy covering all copies,
OS/backend byte and inode enforcement, disk-full and interrupted-write evidence,
low-capacity alerts, and supported retention/deletion/recovery behavior. A declared
PVC size or a request cap is not hard-quota evidence. Until then operators must size
and monitor private storage externally; this is an unresolved launch blocker.

## External and release-owner prerequisites

The [release checklist](release-checklist.md) requires candidate-bound proof. Before
public/production release, provide dated evidence tied to one revision/profile for:

1. Preserve MIT release terms and third-party notices, and verify the enabled
   GitHub private reporting channel remains available; see [SECURITY.md](../SECURITY.md).
2. TLS certificates/termination, inaccessible direct backend, explicit ingress host
   policy, no trust of client-supplied forwarded identity, connection/header/rate and
   slow-client limits, secret-safe logs and private/no-store authenticated caching.
3. Supported platform installation/upgrade/rollback, reproducible build provenance,
   dependency/security review, artifact signatures and signed update verification.
   Local tests cannot prove an artifact was signed or distributed safely.
4. Named operational ownership, monitoring, retention/privacy commitments and the
   selected-target execution, storage, credential and recovery acceptance evidence.

Local prerequisites reduce risk; they do not complete any of these external gates.
No service needs to remain running for this slice's tests. Live/provider fixtures
remain opt-in outside default pytest/CI and require separately authorized effects.

## Developer verification

`tests/test_launch_boundaries.py` and `tests/test_launch_spending.py` exercise
request/admission limits with isolated inputs. They are not live identity,
provider or capacity acceptance. Follow [contributing](../CONTRIBUTING.md#verification-gates)
for current checks; keep exact results outside shipped guidance.
