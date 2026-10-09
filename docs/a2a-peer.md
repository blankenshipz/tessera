# Governed A2A reference peers

Implemented boundary: A2A **1.0.0**, official Python **`a2a-sdk[http-server]==1.2.1`**,
JSON-RPC transport. Sources inspected on 2026-10-04:
[official specification](https://a2a-protocol.org/v1.0.0/specification/),
[official SDK](https://github.com/a2aproject/a2a-python), and published PyPI metadata.
This is not the older internal agent-thread protocol, nor a claim of complete
A2A conformance. SDK discovery, SendMessage, GetTask and CancelTask were exercised.
Streaming, push, extended cards and ListTasks explicitly return unsupported.

## Install and operate

The packaging owner must add optional extra `a2a = ["a2a-sdk[http-server]==1.2.1"]`
and update the lock. The A2A lane intentionally does not edit shared packaging.
Until then, install Tessera and that pinned SDK into an isolated environment:

```sh
python -m pip install /path/to/tessera 'a2a-sdk[http-server]==1.2.1'
python -m tessera_control.a2a_peer --config /private/peer.json --port 8771
```

Run independently under an ordinary service supervisor, retaining the SQLite
database across restart. The installed module is the entrypoint; no checkout or
desktop process is needed for serving. The control service must remain available
for admitting messages. Off-host URLs require HTTPS with normal CA verification;
put the loopback service behind an authenticated TLS terminator. Do not expose the
plaintext listener to an untrusted network. Host selection/deployment and actual
independent-host/laptop-off evidence remain unverified and require authorization.

Private config fields (never put token values in this file):

| Field | Meaning |
| --- | --- |
| `public_url`, `control_url` | Explicit credential-free service URLs; no redirect following or environment proxies |
| `database` | Private durable SQLite path, separate from control DB |
| `agent_id`, `credential_id` | Recipient's enrolled control principal/credential IDs |
| `destination`, `model` | Exact approved destination and attested model; model is not cryptographic provenance |
| `control_token_env` | Environment reference to recipient's **agent** control credential, never an owner credential |
| `identities` | Mapping SHA-256(peer bearer) to `{tenant, agent_id, project_id}`; values are operator-bound identity, not request claims |

Install peer bearer credentials out of band. Grant IDs are references, not bearer
authority. Identity registry changes require service restart; remove/restart to
revoke peer-only read access. Control credential or parent-grant revocation and
expiry immediately fence new/replayed message admission through the real HTTP
check. Peer tokens are high-entropy independent secrets; hashes still belong in
private operator config. This first peer does not implement an enrollment/rotation
API. Control task IDs, A2A task IDs, and curated context IDs are distinct.

## Authority and task lifecycle

Human issues root authority using [the authority interface](agent-authority-interface.md).
The sender uses its own control credential to call the root grant's `/delegate`
with a strictly narrowed child for the recipient. For this reference-only peer,
child actions must be exactly `attempt:status`, budget zero, depth zero, and exact
binding/destination/model/project. No job admission, dispatch, approval, arbitrary
HTTP forwarding, or additional delegation is available here. On every SendMessage,
including replay, the recipient GETs the child grant with its own enrolled agent
credential and validates live effective ancestry, expiry, revision/epoch, issuer
matching authenticated sender, tenant, recipient, project, bounds and binding.
Actual execution resource freshness and spending are enforced by `/consume`,
not by this read-only exchange. The peer never consumes or creates execution grants.

One structured data part is supported; prompts, raw text/files, URLs with queries,
and arbitrary metadata are not stored. Authenticated HTTP bodies are capped at
32 KiB before SDK parsing; accepted envelopes are capped at 16 KiB. SDK dispatcher
request-body/validation logging is disabled; access logs are disabled by the
module launcher. The closed envelope is:

```json
{
  "operation": "submit",
  "reference": "agent_context_ID",
  "authority": {
    "grant_id": "agent_authority_ID",
    "revision": 1,
    "epoch": 1,
    "binding": {
      "variant_id": "variant_ID",
      "expected_variant_revision": 1,
      "expected_design_revision": 1,
      "execution_grant_id": "execution_grant_ID",
      "expected_execution_grant_revision": 1,
      "expected_execution_epoch": 1,
      "task_id": "task_ID",
      "expected_task_revision": 1,
      "plan_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "target_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }
}
```

SendMessage creates a server-ID task in INPUT_REQUIRED. A follow-up message to that
task with `operation: result` and an artifact reference completes it. This is an
**untrusted caller result reference**, marked `verified: false`; it is not remote
model reasoning, an experiment result acceptance, or scientific validation.
History contains only closed references. Cancellation is durable, idempotent and
fences late results; it cancels this protocol task, **not any linked compute job**.
Authenticated task reads and local cancellation remain available after execution
authority expires. A canceled/completed task cannot accept a fresh message.
Same message ID/content replays its current task without duplicate task/event;
same ID/different content conflicts. State, message replay index and local events
commit in one SQLite transaction. Tenant and sender scope applies to every access.

## Verification and evidence

Default ledger tests require no SDK. SDK adapter tests skip clearly if the optional
dependency is absent; injected verifiers there are **synthetic** coverage only.

```sh
python -m pytest -q tests/test_a2a_store.py tests/test_a2a_peer.py
python dev/check_a2a_fixture.py               # no effects
python dev/check_a2a_fixture.py --run         # explicitly opt-in local boundary
```

The opt-in checkout fixture uses synthetic provisioning helpers (including a
synthetic worker registry), no workload/provider effects. It launches a real HTTP
control process and two independent real SDK peer processes, driven by two SDK
clients. Real issue/delegate/revoke commands and HTTP grant validation run; there
is no mock authority server. All fixture processes/private state/environment-held
tokens are cleaned up. The fixture observed discovery, send/get/result,
restart/replay, conflicting replay denial, cancellation/late-result denial,
authentication, cross-peer denial, stale epoch denial, ancestor revocation and
real-time expiry replay denial. This is local live **protocol** evidence with
synthetic setup records, not autonomous reasoning or independent-host proof.

For existing authorized installations, `dev/check_a2a_peers.py --run
--peer-a-config /private/a.json --peer-b-config /private/b.json` uses real already
delegated grants. Configs additionally need `incoming_test_token_env` and
`test_envelope` as above. It only launches explicitly configured loopback peers,
uses existing credentials/grants, creates bounded reference tasks and cleans up
its processes. Do not point it at ports already owned by running services.

## Integration hooks and remaining work

- Packaging owner: optional pinned dependency and lock; optional console entry
  `tessera-a2a-peer = "tessera_control.a2a_peer:main"` (module launch already works).
- No control schema/API/store edit is needed: peer state is an independent ledger;
  control schema must include the authority lane's additive grant tables/routes.
- Bridge delivery now joins typed peer/runtime outcome records to the scoped
  authorized notebook through the collector below. Older reference-only
  `peer_events` and historical uninstrumented work remain local/uncollected.
- The optional reasoning bridge below now connects approved curated context to
  the real remote runtime seam; live dedicated-harness admission is still missing.
- Parent: verify installed wheel/service packaging, full integration gate and
  planner/MCP–critic/A2A journey. This lane tested the source module, not a wheel.
- No official interoperability test kit, another-language SDK, legacy 0.3,
  streaming or broad A2A conformance claim is made.

## Optional trusted critic reasoning bridge

`python -m tessera_agent.a2a_reasoning_bridge` is an additive installed module,
separate from the reference-only peer. It explicitly claims one operator-mapped
INPUT_REQUIRED task, marks it WORKING, and invokes the implemented
`remote_reasoning_runtime.ReasoningSupervisor` with its real OpenCode V2 adapter.
Reference-only clients/services remain supported and do not start inference.
No backend or packaging changes are made by this bridge lane; the parent may add
an optional console entrypoint for `tessera_agent.a2a_reasoning_bridge:main`.

Required private mapping (mode 0600, owned regular non-symlink file):

```json
{
  "task_id": "EXACT_A2A_TASK_UUID",
  "tenant": "EXACT_ORGANIZATION_ID",
  "sender_id": "AUTHENTICATED_SENDER_PRINCIPAL_ID",
  "project_id": "EXACT_PROJECT_ID",
  "context_id": "EXPLICIT_DISCLOSED_AGENT_CONTEXT_ID",
  "profile_file": "/private/approved-task-profile.json",
  "profile_sha256": "SHA256_OF_EXACT_PRIVATE_PROFILE_BYTES",
  "prompt_file": "/private/approved-task-prompt.txt"
}
```

The [remote runtime profile](remote-reasoning-runtime.md) is task-specific and
operator-approved. Its exact byte digest is pinned by the mapping; profile/run
identity cannot be rebound on restart. The recipient control credential,
destination, model and control URL must match the peer config. The profile uses a
**separately approved reasoning grant with positive reserved budget**; the peer's
zero-budget, status-only exchange child cannot silently acquire inference
authority. Reasoning grant tenant/project and exact experiment binding must match
the exchange. Both authorities are rechecked at each new effect, alongside the
existing runtime's duration/token/zero-provider-cost fences.

Only `GET /agent-contexts/{context_id}` resolves context, through the recipient's
own `context:read` credential. The response must name the exact disclosed context,
tenant, project, recipient and destination, with `disclosure_approved: true`.
The existing context response omits `org_id`: control GET enforces tenant/project
authorization and the live grant reads pin the tenant. If a context response does
include `org_id`, a contradiction is denied. Its body must hash to the already
approved `prompt_sha256`, and the private prompt
file must have that exact digest. Use the curated context body as the complete
operator-reviewed critic request. The bridge does not concatenate instructions,
forward arbitrary caller prompts/URLs, select a new model or update a prompt pin.
Context withdrawal, replacement, denial or digest drift prevents new effects.

```sh
python -m tessera_agent.a2a_reasoning_bridge \
  --mapping /private/critic-task-map.json \
  --peer-config /private/critic-peer.json \
  --state-dir /private/critic-runtime-state
```

Use an ordinary supervisor and the same private state directory on restart. The
bridge shares the runtime's nonblocking `supervisor.lock`, preventing two runtime
owners. Durable task/run/profile/context linkage is claimed before inference;
runtime session/message IDs are deterministic and persisted, and pending or
uncertain create/prompt effects never become permission to retry or replace a
session. The peer SQLite cancellation gate serializes create/prompt admission
with durable local cancellation. It is **not** a distributed transaction with
control revocation or the OpenCode service; those fences are checked immediately
before the bounded remote call, and uncertain outcomes remain unknown.

CancelTask on a WORKING task durably cancels it, and the independently running
bridge observes that state on its next tick and invokes runtime interrupt without
resume. SIGTERM/SIGINT also request a bounded interrupt. An interrupt acknowledgement
is not provider/compute termination proof: task metadata always retains
`termination_verified: false`; failed interrupts remain `stop_unknown`. Cancellation
wins over a concurrent completion publication and late external result messages
cannot complete a WORKING or canceled bridge-owned task. Linked compute jobs are
not implicitly canceled by this protocol operation.

Runtime completion publishes a **reasoning session outcome reference**, not a
transcript, critique text or scientific acceptance. Actual approved reasoning may
write a scoped proposal through its permitted Tessera MCP tools; collecting and
joining those proposal IDs belongs to the parent journey. No message-content
readback or hidden reasoning capture is added here.

### Redacted provenance outbox and enrolled collector delivery

Task claim, model outcome, permission denial and cancellation metadata is committed
to `peer_provenance_outbox` with the task/link update. Envelopes start `pending`
and become `acknowledged` only after a matching real collector receipt. A separate
durable delivery row retains attempts, request digest, closed delivery status/code
and sequence, atomically with acknowledgement. Restart/replay preserves
immutable record IDs/content. `A2AStore.provenance_outbox(tenant, sender, task_id)`
is a bounded, owner-scoped local collector seam, not a public unscoped export.
Each envelope has:

- `record`: the strict `AgentProvenanceRecord` shape, with model identity,
  outcome, correlation/causation, central task/context/grant IDs and approved
  content digest. Peer task/message/peer IDs belong in `record.remote`, never
  central references. A separate delegation-kind record anchors the narrowed
  exchange grant; a session-link record correlates runtime session to run ID.
  No text, token values, exception strings or paths are recorded.
- `grant_proof`: exact immutable grant ID/revision/epoch pins for that record.
- `links`: a closed wrapper `{peer_task_id, peer_message_id, run_id, session_id,
  message_id, context_id, profile_sha256}`. Session/message can be null before
  the runtime bind. These are attributed opaque links/digests, not model verification.

**Registered backend-owned collector:**
`POST /projects/{project_id}/agent-provenance` accepting only
`{record, grant_proof}` from this local envelope (not `links`),
the shared client's stable `provenance-<sha256(record_id)>` idempotency key,
enrolled external-agent bearer,
`X-Agent-Destination`, and explicit human-enrolled `provenance:append` scope.
Derive tenant/actor/project from live authentication, enforce exact recipient
disclosure/grant/reference ownership, and persist record/idempotency response
in one transaction. Same
record/content replay must return the same receipt; changed replay must conflict.
See the [current append contract](agent-provenance-interface.md) for its exact
receipt and enrollment/schema wiring. The bridge's shared
`tessera_agent.provenance_client.ProvenanceClient` strips private `links`, validates
the strict envelope and receipt, and sends one bounded POST per record without
redirects or environment proxies. The delivery intent is durable before sending;
loss of an acknowledgement leaves the original record pending, and the next
explicit pass reuses the exact body/key. No automatic stale-pin resolution,
scope fallback, credential replacement or authority creation is available. The
bridge must not assume delivery from a local write. `links` stays in the private
ledger; session/run/delegation relationships are also emitted as typed correlated
records, rather than an unvalidated extension of the strict record schema.
Frozen stale grant pins may block later delivery: preserve the old pending record
and append an explicitly correlated resolution with current verified pins rather
than mutating history. Notebook completeness must
include pending/unknown/uncollected bridge provenance honestly.

An independent metadata-only drain does not read a prompt, create runtime state,
open a harness connection or invoke reasoning:

```sh
python -m tessera_agent.a2a_reasoning_bridge drain-outbox \
  --mapping /private/critic-task-map.json \
  --peer-config /private/critic-peer.json --limit 100
```

The exact private profile pin is still required for its control origin,
destination and recipient's secure `agent_token_env` reference. The credential
must already have human-enrolled `provenance:append`. A bounded pass processes
1..100 pending records, then reports total/acknowledged/pending and
`outbox_fully_acknowledged`. That flag is only this local outbox's delivery
completeness, **not** complete research/notebook coverage or scientific validity.
Exit 2 means records remain pending; unknown/rejected receipts never relabel the
reported model/permission outcome. Invalid legacy envelopes also remain pending
instead of being silently rewritten. The raw prompt/profile SHA pins stay intact;
provenance content digests use the collector's typed `sha256:` representation.

For a separately admitted reasoning run, optional `run --deliver-provenance`
performs one such bounded pass after the runtime reaches a terminal state. It
does not retry inference, and leaves both runtime outcome and delivery state
visible if intake is denied. Use the separate drain command to retry metadata.

`tests/test_a2a_provenance_delivery.py` launches an owned loopback Uvicorn control
server, uses the shared real HTTP client and real collector SQLite, and verifies
the authenticated notebook/export. Synthetic provisioning and a synthetic
reasoning harness are explicit; **no inference occurs**. Observed boundary cases:
correlated model/session/delegation records, permission denial plus unchanged
unknown outcome, native POST receipts, restart after lost receipt without duplicate
central records, stale grant denial, real credential expiry, bounded partial
drain, invalid receipt/body rejection, and metadata-only CLI without prompt or
harness references. This is real local collector/notebook evidence, not live
critic reasoning or independent-host proof.

### Bridge evidence and outstanding live handoff

`tests/test_a2a_reasoning_bridge.py` exercises the real durable store/supervisor
seam with **synthetic control/harness adapters**. It is not live inference proof.
The previously passing two-process protocol evidence was not rerun for this
continuation. New tests cover immutable profile/prompt/context pins, run/session
restart, uncertain create/prompt replay, cancel/interrupt unknown, context withdrawal
between create and prompt, revoked/expired/budget/project authority, redacted pending
outbox and operator shutdown. A live bridge run requires all of:

1. Approved dedicated included-ChatGPT-OAuth-only OpenCode V2 research profile,
   exact version/schema/agent/model/permissions/workspace, and its secure HTTP
   credential reference; not the coding service and not API-key/paid fallback.
2. Enrolled critic credential with exact destination and context/authority scopes,
   active reasoning grant, active narrowed A2A child, and explicitly disclosed
   context matching the exact approved prompt bytes/digest.
3. Private task-specific mapping/profile/prompt and retained state references,
   owned supervisor process, and fresh quota admission before bounded inference.
4. Critic enrollment must include `provenance:append` for delivery. Local
   collector/notebook integration is tested; live critic output, current installed
   service credentials, pending historical records and off-host proof remain
   parent journey checks.

No dedicated live profile/password was securely handed to this worker; no model
call was attempted. Independent-host/laptop-off and installed-wheel service proof
remain separate parent checks.
