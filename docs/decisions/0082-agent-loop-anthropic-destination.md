# 0082 — Reasoning-plane freeze lifted; Anthropic is an enrolled model destination called by API-key reference

## Status

Proposed (2026-11-22, owner direction). Implements slices 1 and 2 of the on-platform
agent-loop work. Amends [ADR 0033](0033-freeze-over-built-planes.md) for the agent and
reasoning plane only. Keeps the data-destination invariant of
[ADR 0047](0047-agent-routed-data-destinations.md). Model-call receipts are
[ADR 0083](0083-model-call-receipts.md); client-requested research and review jobs and their
findings are planned ADR 0084; the standing loop and auto-created jobs are planned ADR 0085 (neither
is on main yet). Numbers 0069-0081 are claimed by other in-flight records; the integrator may
renumber this record at merge.

## Context

Plan critiques, dataset and technique scouting and pre-gate reviews happen today outside
Tessera. No receipt records which model ran them, against which plan bytes, or at what cost.

Tessera already has the pieces of an on-platform agent: a bounded planner/critic research
journey (`tessera_agent.research_journey`), a production factory and service that fail closed
unless digest-pinned ports and an enrolled harness exist, and a manually started, admission-gated
reasoning supervisor (`tessera-reasoning`) that keeps a private journal and starts only from
systemd `LoadCredential` references. Two things stop it from being used:

1. ADR 0033 froze the coordinator, OpenCode harness, A2A peer and agent-sandbox/provenance
   projections to bug and security fixes.
2. Profile validation, harness binding, the observer and the research-service model pattern
   admitted only included ChatGPT OAuth with `providerID: "openai"`, driven through an
   operator-pinned OpenCode V2 harness. Tessera had no client of its own for another model API.

Two design notes proposed how to proceed. One extends the existing reasoning stack and names
Anthropic as the loop's model destination. The other proposes review jobs as ordinary
single-stage campaigns under a human review policy, with an operator model-destination registry
in which private inputs default to local or self-hosted models. This record reconciles them
(see "Reconciling the two designs").

## Decision

1. **Freeze amendment.** By owner direction, 2026-11-22, the ADR 0033 freeze is lifted for the
   agent and reasoning plane only: the research journey and factory, the reasoning supervisor
   and its startup mapper, harness binding, observer and runtime admission. Work needed to
   enrol Anthropic, receipt model calls, run research and review jobs, and later stand up a
   control-plane loop is in scope. Every other plane listed in ADR 0033 stays frozen.

2. **One enrolled destination registry.** `tessera_agent.model_destinations` is the single,
   code-reviewed list of model destinations the reasoning plane admits. It has two entries:

   | provider id | account mode | runtime | credential | model-runtime hosts |
   |---|---|---|---|---|
   | `anthropic` (**default**) | `anthropic-api-key-ref` | `anthropic-messages-api` (Tessera's own client) | `credential_ref: anthropic-api-key://OWNER/REFERENCE`; key in a systemd credential | `api.anthropic.com:443` |
   | `openai` (optional alternative) | `included-chatgpt-oauth` | `opencode-v2` (pinned harness) | held by the pinned harness, unchanged | none added |

   Anthropic is the default destination of the on-platform reasoning loop (owner direction,
   2026-11-22): Tessera's own client calls the Anthropic Messages API directly. It does not
   run Claude Code and does not use any Claude subscription login. OpenAI included ChatGPT
   OAuth through the OpenCode V2 harness stays admitted as an optional drop-in alternative,
   chosen by profile; nothing falls back from one to the other. The OpenCode path refuses
   Anthropic (`require_runtime`). A Claude Code runtime could be added later as a separate
   optional provider; this record does not provide one.

   Profile validation, harness binding, the observer, runtime admission and the research-service
   and consumer model pattern resolve the provider and account mode through this registry. The
   provider prefix of a qualified model id (`openai/<id>` or `anthropic/<id>`) must match the
   configured account mode. Unknown providers, unknown account modes, any profile field
   carrying an API key value, and a mismatched provider/mode pair are refused with sanitized
   errors. The OpenAI path behaves exactly as before.

3. **Operator attestation, not discovery.** Admitting the Anthropic destination is an operator
   act: the operator writes the profile, enrols the credential reference and key file, pins the model limits
   and prices, and creates the admission marker (an OpenAI profile also pins the harness). Tessera never infers the destination from a model name or from
   files found on the host.

4. **Credentials are references (slice 2).** The Anthropic API key is held only as a systemd
   credential: the base unit has `LoadCredential=anthropic-api-key:/etc/tessera/credentials/reasoning-anthropic-api-key`,
   a root-owned 0600 operator file. Tessera reads `$CREDENTIALS_DIRECTORY/anthropic-api-key`
   at call time, keeps it in memory only and sends it only as the `x-api-key` header to the
   allowlisted host. It is never an environment variable, argv, shell variable, repository file,
   log line, error, journal row, receipt or provenance field. The profile names the key by an
   opaque `anthropic-api-key://OWNER/REFERENCE` reference; OWNER and REFERENCE are operator
   labels, not account names or secrets. The startup mapper checks that the manager-provided
   `model-credential` file (read only as `$CREDENTIALS_DIRECTORY/model-credential`) exists, is
   private, and that its content equals the profile's `credential_ref`, and that the
   `anthropic-api-key` file is a private non-empty regular file; comparisons are made without
   logging any value. Errors and logs name only the reference.
   **The reasoning unit refuses to start when `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`,
   `CLAUDE_API_KEY`) is set**, for every provider, before it reads the profile. The key is
   supplied only through the credential file.

5. **Egress.** The Anthropic API host (`api.anthropic.com:443`) is on the model-runtime
   allowlist in the registry (`model_runtime_egress_allowed(provider, host, port)`), and that
   allowlist is enforced by Tessera's own transport, not only declared: the client accepts
   `https` only, requires the exact host and port, calls `model_runtime_egress_allowed` before it
   opens any connection, ignores environment proxies and refuses redirects. A refusal is the
   code `model_egress_refused`. Host firewalling of the reasoning service remains recommended
   defence in depth. For an OpenAI profile the pinned harness connects, not Tessera, and no
   network-level enforcement for the harness process is implemented (a gap: host firewalling is
   the operator's responsibility); OpenAI admission still relies on the pinned effective
   harness config digest and the trusted guard and v2 evidence checks. No host is added to the
   agent-sandbox allowlist, the `allowlist-proxy-v1` campaign-stage profile, or Salad fetch
   hosts. Public web research, if a job needs it, is a separate step on the existing
   allowlist; it is never routed through a model call.

   **Tool use (amended by [ADR 0083](0083-model-call-receipts.md)).** The Anthropic runtime
   may run a bounded multi-turn tool loop. It is deny by default: the profile `permissions`
   start with `{"action":"*","resource":"*","effect":"deny"}` and may then add exact
   `tessera_*` allows; with no allows the runtime sends a single user message and offers no
   tools, as before. The only tools are Tessera's own MCP tools, reached through the existing
   `tessera_mcp` server with the same agent token (read by file path) and the same control-plane
   scope checks. No tool host is added to any egress allowlist. The tool contract, receipts and
   limits are in ADR 0083.

6. **Relation to ADR 0047.** The Anthropic destination is not "registered" in ADR 0047's
   sense (a provider in the provider-trust file). By owner direction, 2026-11-22, it is an
   operator-attested model destination (see the amendment in
   [ADR 0047](0047-agent-routed-data-destinations.md)). There is no automatic data-class check
   of plan or context text before a model call: the operator decides what is admitted by
   writing the profile and prompt. Each model-call receipt records the profile's declared
   `input_class` (`public`, `private_code_approved` or `private_derived`; a missing value reads
   as `private_derived`, per [ADR 0039](0039-data-class-guard.md)).
   The provider-trust setting digest is out of scope for reasoning receipts because model
   destinations are not governed by the trust file. Campaign stages with non-public inputs
   still cannot use egress and nothing here reads or changes provider trust. Sealed holdout
   data (ADR 0068) is never admissible as a model input, and this is checked in code:
   `refuse_sealed_inputs` runs over the declared `input_digests` values and the prompt digest,
   against the control plane's holdout claims for the project, before the prompt is sent, and
   fails closed. Declared `input_digests` with no `project_id`, or a claims lookup that fails,
   halt the run (`model_input_seal_unknown`) before any model call.

7. **Tier policy.** Policy tiers are named on the profile and later receipts: `sonnet` and
   `opus` are the defaults, `haiku` is for cheap checks, `fable` needs an explicit per-job human
   approval id (`fable_approval_id`) or the profile is refused. An Anthropic model id that maps
   to no tier is refused. Tessera never picks a cheaper tier on its own; a rate-limit or quota
   failure is recorded and backed off ([ADR 0083](0083-model-call-receipts.md)).

8. **Still manual.** The `tessera-reasoning` unit stays manual-start, admission-gated by its
   marker file, and is not enabled at boot. A completed run does not start another. Changing
   that is ADR 0085.

## Reconciling the two designs

| Question | Reuse-the-stack note | Review-policy note | This record |
|---|---|---|---|
| Model path | Extend reasoning profile, harness, journey | Pinned reviewer image as a campaign stage | Extend the reasoning stack (no new backend, no second agent framework) |
| Destination policy | Anthropic registered by owner direction | Operator registry `{id, tier, endpoint_digest, data_classes}`; private inputs default to local/self-hosted | One code-reviewed registry; Anthropic admitted for private inputs by owner direction and operator attestation; specs and agents may narrow, never widen |
| "Tier" | Model tier (Sonnet/Opus/Haiku/Fable) | Destination kind (local/self-hosted/external) | Two words: *policy tier* is the model family; *destination* is the registry entry |
| Credentials | API key by reference | Runtime references (DESIGN §5.1) | `anthropic-api-key://` reference plus a systemd credential file; never an environment variable |
| Egress | Anthropic host on model-runtime allowlist only | Model calls via `allowlist-proxy-v1`; private subjects never egress | Model-runtime allowlist only, enforced by Tessera's transport; campaign-stage egress rules unchanged |
| Receipt | Model, tier, input digests, tokens, cost | Configured destination and tier, *reported* model identity, policy/subject digests | Both, in ADR 0083; identity is "reported", never "verified" |
| Jobs and findings | Client-requested journey (slice 4) | Review policy, job and finding tables, flag-gated decide tool | ADR 0084 |
| Triggers | Standing loop creates jobs (slices 5–6) | Job row in the subject event's transaction, unique trigger key | ADR 0085 |

From the review-policy note this record keeps: findings are advisory and never approve, promote,
unlock or widen anything; no self-review; tenant checks at data access with job, finding,
event and idempotency writes in one transaction; provider-disabled paths stay truthful
(`not_started(reason)`, never a fabricated completion); and synthetic fixtures are labelled
synthetic. Those invariants bind ADR 0084 and ADR 0085.

## Consequences

- An operator can admit a Claude-backed reasoning run by profile, key credential, reference and
  marker. The API key lives only in a root-owned credential file and in the process memory of
  the reasoning service while a call is made.
- The first live run is an operator step on the control host following
  [the reasoning runbook](../remote-reasoning-runtime.md): one bounded `run` that journals a
  model call and a stop. This record does not claim that run has happened.
- **OpenAI route evidence contract (unchanged).** The OpenAI registry entry names its billing
  route (`chatgpt-included`), the exact guard policy
  `{allowed_auth_methods: ["oauth"], billing_routes: [route], on_unknown: "deny",
  on_exhausted: "stop"}`, and its v2 local-check names (`openai_only`/`chatgpt_endpoint`). The
  observer's trusted-guard path and v2 runtime admission check evidence against the destination
  of the binding's account mode or pinned model; cross-provider evidence fails closed. The
  Anthropic path has no harness, so none of this applies to it; its controls are the
  enforced allowlist, the per-call and daily caps and the receipts of
  [ADR 0083](0083-model-call-receipts.md).
- The single unit template is Anthropic-default: it loads the `model-credential` and
  `anthropic-api-key` `LoadCredential=` references plus the profile, prompt and agent-token. An
  OpenAI profile is selected by installing the optional systemd drop-in
  `tessera-reasoning.service.d/openai.conf.example`, which resets `LoadCredential=` and re-adds
  only the profile, prompt, agent-token and harness-password references, so no Anthropic
  credential is loaded. The harness password is loaded only by that drop-in. Removing the
  drop-in returns to the Anthropic default. There is no second unit. With or without it,
  startup refuses when an API-key variable is set or the model or tier is not admitted.
- **Contract surface for later work.** `model_tier(provider, id)` returns
  `sonnet|opus|haiku|fable|unclassified`; `require_tier_admitted(tier, fable_approval_id)`
  refuses Fable without a per-job approval id; `refuse_api_key_environment(env)` refuses
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `CLAUDE_API_KEY` and exits 78 in every
  `service_startup` mode. Model-call receipt fields and the `rate_limit` surface are in
  [ADR 0083](0083-model-call-receipts.md). Planned ADR 0084 should attach `model_calls`
  verbatim and keep `model_reported` labelled unverified; planned ADR 0085 should honour
  `rate_limit.backoff_until`, never re-admit a profile at a cheaper tier on a limit, and reuse
  `refuse_api_key_environment`.
- **Upgrading.** Existing OpenAI deployments must install the openai drop-in
  (`tessera-reasoning.service.d/openai.conf.example`) before updating the unit file; otherwise
  systemd refuses to start because the base unit's `model-credential` and `anthropic-api-key`
  `LoadCredential=` sources are missing. Existing deployments that move to Anthropic must create
  both credential files (`reasoning-model-credential` and `reasoning-anthropic-api-key`) and
  switch the profile before restarting.
- Until planned ADR 0084 is implemented, a research or review submission is refused with a clear reason;
  it is never approximated off-platform.

## Notes

Direction is recorded as owner direction, 2026-11-22. No private messages are quoted.
