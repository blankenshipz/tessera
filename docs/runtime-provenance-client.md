# Runtime/A2A provenance client seam (intent revision 1)

Implementing `tessera_agent.provenance_client.ProvenanceClient`:

```python
client = ProvenanceClient(control_url, agent_token, destination)
result = client.append(project_id, record, grant_proof=None, idempotency_key=None)
client.close()
```

Receipts are parsed as a strict collector row plus `AgentProvenanceRecord`; known
optional defaults are normalized on **both** sides before comparison. This permits
omitted-versus-null additive references without changing an outbox body or key.
Unknown fields (even null references), changed non-null links, record/project IDs,
correlation, kind/outcome, digests, grant pins and remote identity still reject the
acknowledgement as unknown delivery. A positive sequence is required.

When enrollment IDs are available, pass
`expected_actor={"org_id": ..., "actor_principal_id": ..., "credential_id": ...}`
to the client constructor to pin the **first** receipt's authenticated actor metadata.
Without these IDs, the trusted collector's first accepted actor tuple is pinned for
subsequent receipts in that client instance; this is server attribution, not
independent identity verification. There is no additional discovery/auth request.

`record` accepts the strict `AgentProvenanceRecord` or dictionary shape; proof accepts
the strict `ProvenanceGrantProof` or dictionary. No wrapper `links`, prompts, raw
tool outputs, free-form errors or approval events. `DeliveryResult` has `status`
(`accepted`, `rejected`, `unknown`), safe `code`, `record_id`, `status_code` and
optional acknowledged `sequence`. Default idempotency key is stable from record ID
alone; a changed envelope cannot quietly acquire a new key. Each append performs
**one bounded request**, without automatic retry. Unknown receipts retain uncertainty.
The caller explicitly drains its durable outbox and resends the same body/key.

POST goes only to `/projects/{project_id}/agent-provenance`, own enrolled bearer and
`X-Agent-Destination`. Requires explicit `provenance:append`. The shared client
does not fetch owner auth, issue/consume grants, approve, select providers or infer
authority. Origin is explicit: off-host HTTPS, literal-loopback HTTP; no proxies,
redirects or cookies. Bodies <=16 KiB, responses <=64 KiB and bounded timeouts.

Collector acceptance is acknowledgement of **self-reported content-free metadata**,
not proof of reasoning/provider/experiment success. Grant-linked records require
exact current proof; the collector can reject stale pins after revoke/expiry or
reject revoked enrollment. Keep that record pending/rejected locally. Do not mutate
its proof/history, drop grant attribution or manufacture delivery success. An
explicitly authorized causal resolution would be a separate new record.

The client and runtime integration are implemented. A2A owns its separate bridge
outbox integration and must strip local `links` before calling `append`. Digests
use `sha256:<64 lowercase hex>`, not plain hex. Targeted evidence follows in the
runtime checkpoint; no inference admission is implied.

**Backend/install seam:** add `httpx>=0.27,<1` to ordinary application dependencies
in `pyproject.toml` (currently only dev/A2A transitive dependencies provide it).
The shared client is imported by the installed runtime/store; a fresh wheel must
not rely on the developer environment or optional SDK to supply this dependency.

## Runtime outbox and completeness

`ReasoningStore` commits one immutable outbox record with each observed bind,
RPC request/ack/unknown, current-authority observation, stop request, fence and
terminal model-state transition. IDs derive from immutable binding + transition;
same-stage polling cannot create duplicate metadata. Queue is capped at 64 records
per run, drained in explicit batches of at most 8, with one request per item and
stop-on-first-rejection/unknown. Pending/unknown/rejected records survive restart;
accepted receipts require the exact record ID, fields, current proof attribution,
destination digest and positive collector sequence. Previously accepted delivery
cannot be downgraded by a late uncertain receipt.

`profile.project_id` enables one bounded post-run delivery batch using the same
enrolled agent credential. No profile key means **local pending outbox**, not
delivered provenance. Extra batches/reconnects use the installed command:

```sh
tessera-reasoning report --profile /PRIVATE/reasoning.json --state-dir /PRIVATE/state
```

`report` does not instantiate a harness, start reasoning, read provider auth or
change authority. It returns exit 0 only when all **recorded supervisor metadata**
has collector acknowledgement; pending delivery returns 2. Normal run/status
receipts separately show recorded/pending counts and `delivery_complete`.

This is bounded **supervisor-observed metadata coverage**, not full notebook or
instrumented inner model/MCP-tool coverage. Recorded `opencode.session.*` tool
outcomes describe that RPC boundary, not execution/provider termination. The
terminal model outcome is the pinned harness's attributed status. Unobserved
model internals, private reasoning, raw tool output and historical execution are
not collected or inferred. Expired/revoked/stale proof can leave all records
pending; a new separately authorized causal resolution needs a new record, never
rewriting the old proof or substituting an owner credential.
