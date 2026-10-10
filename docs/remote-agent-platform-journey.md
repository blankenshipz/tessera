# Bounded agent integration harness

`dev/remote_agent_platform_live.py` is an opt-in MCP/A2A/ordinary-worker journey,
outside default pytest/CI. It does not prove genuine planner/critic reasoning,
native dialogs, independent-host execution or laptop-off behavior. See
[agent setup](remote-agent-platform-install.md) for installation and identities.

## Prerequisites and invocation

Use an authorized target, inspect service/container/port ownership and quiesce
writers. Supply the installed MCP/A2A extras, ready control/ordinary worker,
explicit target/witness and separately scoped agent/human/critic references.
Do not reuse a historical grant, expired credential or another project's service.
Prepare a mode-0600 JSON manifest outside the repository, with **no bearer values**:

- `environment`: `local_owned_docker` or `authorized_remote`.
- `ownership_inspected`, `writers_quiesced`, `peer_paid_inference_disabled`: true.
  These are operator declarations, not automatic ownership/financial proof.
- `max_seconds`: 1–600; verified `control_url`/`critic_url` origins, `project_id`,
  `agent_destination`, `critic_agent_id`; private `agent_token_file`,
  `human_token_file`, `critic_token_file` and optional public `ca_file` references.
- `steps`: the ordered stages below, with exact typed arguments/assertions.

```sh
.venv/bin/python dev/remote_agent_platform_live.py --manifest /private/path/journey.json
.venv/bin/python dev/remote_agent_platform_live.py --manifest /private/path/journey.json --run
```

Without `--run`, only structure is checked: no credentials opened, services
contacted or live success claimed. Execution has one deadline, bounded responses
and no automatic mutation retry. No provider launch or financial fallback is
authorized by this harness.

## Manifest contract

Every step has `stage`, `via` and nonempty `assertions`. MCP steps have `tool` and
`arguments` matching installed `ResearchProposal`, `AuthorityBounds` and `Consume`
schemas in `tessera_mcp.mcp_workflow`, after response references resolve. Admission/
dispatch commands are `attempt:admit`/`attempt:dispatch`; a version string alone
does not prove those modules exist.

Human steps have `method`, absolute local `path`, optional `body` and mutation
`idempotency_key`. Named path placeholders resolve through `path_parameters`,
for example `{"grant_id":{"$ref":"human_issue.body.id"}}`; resolved segments
must be alphanumeric/underscore/hyphen, not URLs/traversal.

A2A discovery uses official `A2ACardResolver`. The task uses protobuf
`SendMessageRequest`: stable `messageId`, `role="ROLE_USER"`, optional `taskId`
and `parts=[{"data": EXACT_CLOSED_REFERENCE_ENVELOPE}]`. Use the implemented
`operation`, `reference`, `authority` envelope and immutable pins from
[A2A peer](a2a-peer.md), not a guessed legacy method. SDK JSON field names apply.
Caller-supplied result references are `verified=false`, not generated reasoning;
peer text must not be copied into human authorization bounds.

Restart stages use `via="kubernetes"` with a `restart_probe` containing explicit
`kubeconfig`, `context`, `namespace`, `namespace_uid`, `deployment`,
`deployment_uid`. Only bounded exact-scope `kubectl get` calls are made.
`body.generation` hashes actual container starts, not Deployment resourceVersion;
`restart_observed` must differ from `restart_baseline.body.generation`. The
harness does not itself restart the deployment. A human GET generation mode only
applies if the target really implements that route; do not invent one.

Assertions use dot-separated dictionary/list `path`, `op` (`eq`, `ne`, `contains`,
`nonempty`) and `value`. Responses wrap `http_status`/`body` or MCP
`is_error`/`body`. Recursive `{"$ref":"human_issue.body.id"}` references resolve
actual earlier values. No arbitrary shell/Python hooks are supported.

| Stage | Required meaning |
|---|---|
| `planner_proposal` | MCP `tessera_propose_research`; durable proposal |
| `critic_discovery` | Exact A2A protocol/identity assertions |
| `critic_task` | Independent SDK reference task, not generated critique |
| `authority_request` | MCP `tessera_request_authority`; narrow immutable binding |
| `human_issue` | Human POST project `agent-authority-grants`; exact request-linked bounds |
| `agent_admit` | MCP consume `attempt:admit`; actual attempt identity |
| `agent_dispatch` | MCP consume `attempt:dispatch`; ordinary-worker operation |
| `retained_output` | Authorized GET; retained digest/attempt/operation |
| `denied_expansion` | Out-of-bounds consume; exact 403/409/410 fence |
| `lost_reply_replay` | Original dispatch arguments/key; stable response identity |
| `restart_baseline` | Actual pinned container-start generation |
| `restart_observed` | Same probe after externally authorized restart; different generation |
| `expiry_fence` | Expired consume after aware `wait_until`; exact fence status |
| `human_revoke` | Human grant revoke with expected revision/epoch |
| `revocation_fence` | Revoked consume; exact fence status |
| `joined_export` | MCP `tessera_export_notebook`; joined IDs/digests/watermarks/completeness |

Only `retained_output`/`restart_observed` may poll (`poll_seconds` 0–60).
Expiry waits consume the overall deadline. Replay reuses the original dispatch
step, regardless of supplied new arguments. This is client-visible reply-discard/
replay, not a network-dropped submission or provider-reconciliation proof.
Timeout/ambiguous response stops as unknown with no new identity. A generic MCP
`isError` is not proof of denial; exact recognized HTTP fence status is required.

Notebook export must account for both sequence and event cursors, watermarks,
`has_more` and completeness. A partial page or ignored stream is not complete.
Follow the installed [provenance contract](agent-provenance-interface.md).

## Receipts

Retain the closed JSON receipt and exact installed artifact/source revisions
privately. Response bodies stay in memory, not reports; no prompts, credentials,
tool content or hidden reasoning belongs in a shared journal. Preserve failed
and historical receipts at their original identity rather than relabeling them
against newer source.

A local harness pass proves only its returned assertions. Genuine supervised
reasoning, two independent reasoning peers, host isolation and laptop shutdown
need separate real observations. The [release checklist](release-checklist.md)
keeps those boundaries explicit; packaging/tests cannot substitute for them.
