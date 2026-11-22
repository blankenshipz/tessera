# Standalone governed reasoning runtime

This is a durable reasoning supervisor with two runtimes: a direct Anthropic Messages API
client (default) and an OpenCode **V2** HTTP adapter (optional OpenAI path). It is not
the deterministic `tessera_agent.coordinator`, the synthetic OpenCode adapter or
the V1.18.30 provider-disabled context-delivery service. It is installed by the
ordinary Tessera Python distribution; no repository checkout is required to run
`python -m tessera_agent.remote_reasoning_runtime`.

`tessera-reasoning = "tessera_agent.remote_reasoning_runtime:main"` is now registered
in `[project.scripts]`. No shared schema hook is needed: the supervisor owns its
private SQLite journal independently of the control database. The current checkout's
`.venv/bin/tessera-reasoning` was absent during inspection; module `--help` worked.
A refreshed installation/fresh-wheel console script is a separate verification,
not proved by adding the entrypoint to pyproject.

Reviewed Linux service templates and the fixed `LoadCredential` startup mapper are
in [deploy/remote-agent-platform](../deploy/remote-agent-platform/README.md). They
keep reasoning manual-start/admission-gated, separate users/private state, and
never install or start an OpenCode harness implicitly. Linux service execution
remains separate from the static/synthetic profile checks.

## Anthropic Messages API by key reference (ADR 0082, ADR 0083)

The default destination is the Anthropic Messages API, called directly by Tessera's own
client in `tessera-reasoning`. There is no Claude subscription login, no Claude Code
runtime and no OpenCode harness on this path (a Claude Code runtime could later be a
separate optional provider). The API key is held only as a systemd credential and is never
an environment variable, shell variable, repository or env file, argv, log, journal or
receipt. OpenAI included ChatGPT OAuth through the OpenCode V2 harness stays the optional
drop-in alternative; the OpenCode path refuses Anthropic.

Generic operator procedure (all paths operator-supplied; no automatic start):

1. Create the key file, root-owned and mode 0600, at
   `/etc/tessera/credentials/reasoning-anthropic-api-key`. Preferred: create it with a root
   editor, for example `sudoedit /etc/tessera/credentials/reasoning-anthropic-api-key`
   (or `sudo -e`), then set the mode and owner with `sudo chmod 0600` and `sudo chown root:root`
   if the editor created it with other defaults. If you must paste on stdin, disable terminal
   echo first so the key is neither shown nor left in scrollback:
   `stty -echo; sudo install -m 0600 -o root -g root /dev/stdin
   /etc/tessera/credentials/reasoning-anthropic-api-key; stty echo` (paste the key, press
   Enter, then Ctrl-D). Do not `export` it, do not put it in a shell variable, a repository file, an `Environment=`
   or `EnvironmentFile=` line, or a command-line argument. The base unit loads it as
   `LoadCredential=anthropic-api-key:...` and Tessera reads `$CREDENTIALS_DIRECTORY/anthropic-api-key`
   at call time, keeping it in memory only.
2. Write the reference file `/etc/tessera/credentials/reasoning-model-credential` (private,
   non-empty) containing exactly the profile's `credential_ref` (one line, trailing newline
   allowed), for example `anthropic-api-key://OWNER/REFERENCE`. OWNER and REFERENCE are operator
   labels, not secrets. Tessera compares it and never logs it.
3. Write `/etc/tessera/reasoning.json` (see the Anthropic profile below): the model, the
   `credential_ref`, `limits` and operator-pinned `pricing_cents_per_mtok`. Fable additionally
   needs `fable_approval_id`. Optional `input_class`
   (`public` | `private_code_approved` | `private_derived`; missing reads as `private_derived`)
   is recorded on receipts. Optional `input_digests` and `project_id` feed the sealed-holdout
   check, which refuses a sealed input before the prompt is sent.
4. Confirm the unit environment has none of `ANTHROPIC_API_KEY`,
   `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_API_KEY`; startup refuses (exit 78) otherwise.
5. Create the admission marker: `sudo install -m 0600 /dev/null /etc/tessera/reasoning.admitted`.
6. Start one bounded run through the manual unit
   (`sudo systemctl start tessera-reasoning.service`). The unit has no `[Install]` section, so it
   is manual-start and not enabled at boot.
7. Check `sudo systemctl status tessera-reasoning.service` and `tessera-reasoning status` (the
   state directory is `/var/lib/tessera-reasoning`): it prints one receipt row per model call,
   including tokens, computed cost, limits in force and the day's totals. The completion text
   is in a private output file in the state directory, not in the journal.
8. Stop with `sudo systemctl stop tessera-reasoning.service` and remove the marker.
   Never `enable` the unit or attach it to a boot target.

*Alternative, OpenAI included ChatGPT OAuth:* install the optional drop-in
`install -m 0644 deploy/remote-agent-platform/tessera-reasoning.service.d/openai.conf.example
/etc/systemd/system/tessera-reasoning.service.d/openai.conf`, then `systemctl daemon-reload`.
It resets the list with an empty `LoadCredential=` and re-adds only profile, prompt,
agent-token and harness-password, so no Anthropic credential is loaded. Verify with
`systemctl show -p LoadCredential tessera-reasoning`. Use the OpenAI profile and the approved
harness described below. Remove the drop-in and `daemon-reload` to return to the Anthropic
default.

**Caps and receipts.** Before each billed call Tessera counts input tokens with the Messages API
`count_tokens` endpoint on the same host, then refuses the call (`model_call_cap_exceeded`,
nothing billed) if input tokens, worst-case cost, the UTC-day token or cost cap, or the grant's
remaining budget would be exceeded. The daily totals are re-checked after the call. Cost is
computed from the prices you pin in the profile; keep them current. A 429, 529 or billing error
is recorded and backed off with the same model; a model the response reports that does not match
the configured one halts the run. See [ADR 0083](decisions/0083-model-call-receipts.md).

**Known gap.** This runtime sends one user message and uses no tools; the profile's
`permissions` must be exactly deny-all.

**Upgrading.** Existing OpenAI deployments must install the `openai.conf` drop-in before
updating the unit file; the base unit now loads `model-credential` and `anthropic-api-key`, and
systemd refuses to start if those sources are missing. Existing deployments moving to Anthropic
must create both credential files (steps 1 and 2) and switch the profile; then
`systemctl daemon-reload`. The base unit no longer loads `harness-password`; only the OpenAI
drop-in does.

**Deploy order.** Upgrade the control plane before the reasoning supervisor. The control-side
provenance model rejects unknown fields (the new `rate_limit` provenance records), so an older control
plane would reject them until the run's outbox bound halts it.

**Egress.** `api.anthropic.com:443` is on the model-runtime allowlist and Tessera's own
transport enforces it: https only, exact host and port checked with
`model_runtime_egress_allowed` before it connects, environment proxies ignored, redirects
refused (`model_egress_refused`). Host firewalling of the service remains recommended defence in
depth. No sandbox or campaign-stage egress is added. For an OpenAI profile the pinned harness
connects, admission relies on the pinned effective harness config digest and guard evidence
(`openai_only`, `chatgpt_endpoint`), and network-level enforcement for the harness process is not
implemented (host firewalling is the operator's responsibility).

## Required secure operator handoff

1. An enrolled external-agent credential scoped to the exact project and
   destination, with proposal/context/request/consume rights as needed. Never use
   an owner token. For isolated local acceptance, the authorized human setup API
   can mint this test credential and bounded grant; it is implementation/setup work,
   **not** a secret the user must supply again. Supply its named environment reference to the supervisor
   and the scoped MCP service, never in the prompt or profile contents.
2. A separately human-issued bounded agent-authority grant, plus the separately
   approved execution grants/resource pins needed for experiment commands. The
   runtime reads `/agent-authority-grants/{id}` through its own agent credential;
   all experimental effects go through typed Tessera MCP tools and control-side
   transactional consume checks, not a generic HTTP proxy.
3. An approved **dedicated** OpenCode V2 harness service, with its HTTP credential
   delivered by a named environment reference, exact release and canonical
   `/openapi.json` SHA-256 pin, isolated workspace and a custom bounded-step agent.
   Configure the agent's final rules as deny-all followed by only the exact
   `tessera_<tool>` MCP actions it needs. V2 MCP permission resources are `*`;
   destination enforcement belongs to the separately authenticated Tessera MCP
   credential, not an invented OpenCode permission resource.
4. Owner-bound subscription login on that dedicated harness (OpenAI path only): OpenAI
   included ChatGPT OAuth. The Anthropic default needs no harness. The operator must
   verify no API-key provider credential, paid-credit fallback, provider-selection
   plugin or account sharing is possible. `account_mode` below is an explicit
   operator attestation, **not** cryptographic billing proof. The runtime never
   discovers/reads auth stores or keychain and never configures providers. Do not
   attach the general coding-agent service: its tools/ambient credentials and
   permission configuration are not this enrolled research profile.
5. Service supervision and isolation on the chosen host. A restart policy resumes
   the existing journal; it does not create replacement sessions. Host isolation
   must deny unapproved plugins/process/network/credential access. OpenCode
   permission rules alone are not an OS sandbox or a financial hard limit.

Local test enrollment/grants can be minted by the parent using the authorized human
API setup. The unresolved live-reasoning prerequisite is instead an **approved
dedicated included-OAuth research process/profile**, its own HTTP password reference
`TESSERA_REASONING_HARNESS_TOKEN`, and private profile reference
`TESSERA_REASONING_PROFILE`. Neither reference was supplied to this worker. An
independent authorized host is needed only for off-host/laptop-off proof, not for
bounded local reasoning acceptance. No inference, auth-store read/copy, keychain
operation or external deployment was performed.

## Observed installation and supported dedicated-process setup

Inspection on 2026-10-04 found:

- Command: `$OPENCODE_ROOT/bin/opencode` (set `OPENCODE_ROOT` to the approved
  installed wrapper root; do not use an unguarded binary path).
- Version: `opencode v2.0.18-subscription.20260929`.
- Wrapper-verified binary:
  `$OPENCODE_ROOT/runtime/v2-current/built/cli-darwin-x64/bin/opencode`;
  release manifest SHA-256
  `10c61d2c6c9d03c3959e01894a1309d69ceb70059c30b780a7a5e8e6c0933b09`.
- Wrapper config: `$OPENCODE_CONFIG_ROOT/v2` (set to the approved configuration root).
  Metadata shows default `router`, model `openai/gpt-6-luna`, four plugins,
  no MCP servers and no `tessera-researcher` agent. These are coding-profile facts,
  not research-harness approval or proof of an OAuth login.
- The wrapper hard-codes HOME, config and XDG paths. External XDG/profile overrides
  are discarded. There is no observed `--profile` flag. Do not bypass the wrapper
  or call its raw binary to evade subscription guards.
- The checked guard source (OpenAI route) admits OpenAI ChatGPT OAuth methods
  `chatgpt-browser`/`chatgpt-headless`, checks same-account quota admission and
  prohibits API-key/custom-endpoint fallback. Its presence is metadata, not a
  live admission result. No provider credential contents were inspected.

Supported CLI commands were checked with `--help`: `serve --hostname --port`,
`--standalone`, `api --server`, and `auth login --server --method`. The native V2
dedicated connection uses Basic username `opencode` with its own server password.
The runtime now supports `harness_auth_mode: "basic"` (default) without changing
Tessera control's bearer authentication. This HTTP password is **not** a provider
API key or an OAuth token.

After the parent approves a dedicated **local process/workspace** profile, the
following is the supported setup sequence; it was not executed here:

1. Create a private workspace outside the coding repository, under an explicitly
    approved private path supplied by the operator in `$TESSERA_RESEARCH_PROFILE_DIR`. Place
   an operator-reviewed `opencode.json` there with a custom primary
   `tessera-researcher`, approved `openai/MODEL_ID`, `steps: 3`, final deny-all rules
   plus exact MCP permissions, `snapshots: false` and `compaction.auto: false`.
   Do not add providers, API-key environment variables or endpoint overrides.
2. Configure only the installed Tessera MCP stdio command. For example, under
   `mcp.servers.tessera`, use `type: "local"`, `command: ["/ABS/INSTALL/bin/tessera-mcp"]`,
   `protocol: "legacy"` (Tessera MCP 2025-11-25), `codemode: false`, and these
   environment **references**: `TESSERA_CONTROL_URL`, `TESSERA_AGENT_TOKEN_FILE`,
   `TESSERA_PROJECT_ID`, `TESSERA_AGENT_DESTINATION`. Use `{env:NAME}` substitutions,
   not token values. The wrapper does not forward arbitrary ambient variables, so
   alternatively use approved literal nonsecret URL/project/destination and the
   private **test-agent token-file path**, not its contents. The actual permission
   for tool `tessera_propose_task_plan` under server `tessera` is
   `tessera_tessera_propose_task_plan`, resource `*`. `codemode: false` avoids
   granting the generic `execute` action merely to call scoped tools.
3. Have the parent mint a fresh private HTTP password into the secure reference
   `TESSERA_REASONING_HARNESS_TOKEN`. Run the **guarded wrapper** as a dedicated
   foreground server from that workspace; port 0 selects a fresh port without
   attaching to or stopping the coding service:

   ```sh
   OPENCODE_SERVER_PASSWORD="$TESSERA_REASONING_HARNESS_TOKEN" \
      "$OPENCODE_ROOT/bin/opencode" serve \
       --hostname 127.0.0.1 --port 0
   ```

   The working directory must be the approved workspace. Keep the process owned
   and supervised independently; do not use `service set/start/stop/restart` to
   alter the coding singleton. Capture only the new process's reported loopback
   URL. Put it into the private runtime profile; never substitute the coding URL.
4. Inspect this dedicated endpoint through supported CLI commands, with its
   password reference set as `OPENCODE_SERVER_PASSWORD` and `--server` selecting
   **only the URL returned by step 3**:

   ```sh
   opencode api --server "$TESSERA_RESEARCH_URL" get /api/info \
     | python -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"version":d["version"]}))'
   opencode api --server "$TESSERA_RESEARCH_URL" get /api/agent/tessera-researcher \
     | python -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(json.dumps({k:d.get(k) for k in ("id","model","steps","permissions")}))'
   opencode auth list --server "$TESSERA_RESEARCH_URL" --format json \
     | python -c 'import json,sys; rows=json.load(sys.stdin); print(json.dumps([{"id":r["id"],"connections":[{k:c.get(k) for k in ("type","method")} for c in r["connections"]]} for r in rows]))'
   ```

   Capture only release, final permission/model/step metadata and connection
   type/method; redact account identifiers and never retain full credential/API
   responses. Pin the dedicated endpoint's canonical `/openapi.json` digest.
5. This dedicated process uses the wrapper's existing owner-bound account database
   **without copying OAuth material**. It is a separate process/workspace, not a
   separate account/data trust domain. Reusing the owner's saved included OAuth
   connection therefore needs explicit parent admission and confirmation of the
   OAuth-only metadata/guard policy above. No such research-profile admission was
   established here. If no included account is connected, the supported owner
   login is `opencode auth login openai --server "$TESSERA_RESEARCH_URL"` and
   selecting **ChatGPT Pro/Plus (headless)**, or explicit method `chatgpt-headless`
   after dedicated integration metadata confirms it. This is an interactive
   owner action; do not invoke it headlessly, copy auth files or select `key`.
   A new login affects this shared account database and is not isolated from the
   owner's coding accounts. True data/account isolation needs a separately approved
   guarded launcher/profile and owner login; that launcher is not presently supplied.
6. Before the one bounded inference admission, verify the dedicated profile's
   final permissions/model/steps and MCP connection, effective active Tessera
   grant, quota admission and zero-paid-fallback safeguards. Securely hand the
   profile/password references to the runtime. The opt-in check below is then
   <=3 steps/120 seconds, not authority to launch arbitrary inference.

`opencode --standalone WORKSPACE` is useful for interactive owner setup but is not
the durable installed headless service: inspected native code uses a private
password and stdin ownership lease, so the private server exits when its CLI owner
ends. It also shares the wrapper's data paths. Neither `--standalone` nor alternate
XDG values prove an isolated account profile or laptop-off durability.

Sources: [V2 CLI](https://opencode.ai/v2/docs/cli),
[configuration](https://opencode.ai/v2/docs/config),
[provider accounts](https://opencode.ai/v2/docs/cli/providers),
[agents](https://opencode.ai/v2/docs/agents),
[MCP](https://opencode.ai/v2/docs/mcp-servers), plus bounded nonsecret inspection of
the installed wrapper and native standalone/server-connection source.

## Private profile

Create a mode-0600 JSON profile outside the repository and a mode-0600 prompt
file (at most 8192 bytes). Replace every placeholder with operator-approved facts;
calculate `prompt_sha256` from the exact UTF-8 prompt bytes. Canonical schema digest
uses `json.dumps(schema, sort_keys=True, separators=(",", ":"))` SHA-256.

Anthropic profile (default; direct Messages API, no harness fields). The numbers in
`limits` and `pricing_cents_per_mtok` are placeholders: the prices are operator-pinned, so check
current published prices before use.

```json
{
  "run_id": "research-run-001",
  "model": {"providerID": "anthropic", "id": "APPROVED_MODEL_ID"},
  "account_mode": "anthropic-api-key-ref",
  "credential_ref": "anthropic-api-key://OWNER/REFERENCE",
  "input_class": "private_derived",
  "control_url": "https://CONTROL_ORIGIN",
  "project_id": "EXACT_ENROLLED_PROJECT_ID",
  "agent_token_env": "TESSERA_REASONING_AGENT_TOKEN",
  "grant_id": "APPROVED_GRANT_ID",
  "revision": 1,
  "epoch": 1,
  "subject_credential_id": "ENROLLED_AGENT_CREDENTIAL_ID",
  "destination": "EXACT_DESTINATION",
  "prompt_sha256": "EXACT_PROMPT_SHA256",
  "max_steps": 3,
  "max_tokens": 8192,
  "max_duration_seconds": 120,
  "permissions": [
    {"action": "*", "resource": "*", "effect": "deny"}
  ],
  "limits": {
    "max_output_tokens_per_call": 4096,
    "max_input_tokens_per_call": 20000,
    "max_cost_cents_per_call": 50,
    "daily_token_cap": 200000,
    "daily_cost_cap_cents": 500
  },
  "pricing_cents_per_mtok": {
    "input": 300,
    "output": 1500,
    "cache_read": 30,
    "cache_write": 375
  }
}
```

`max_output_tokens_per_call` is at most 32768, `max_cost_cents_per_call` at most 5000 and
`daily_cost_cap_cents` at most 100000; all `limits` values are positive integers and the prices
are non-negative numbers (cents per million tokens). `permissions` must be exactly the deny-all
rule shown (no tool use in this runtime yet). Harness fields (`harness_url`, `agent`,
`workspace`, `version`, `schema_sha256`) are not used. Optional `fable_approval_id` and
`input_digests` apply as above.

Optional OpenAI alternative (OpenCode V2 harness; included ChatGPT OAuth, no `credential_ref`):

```json
{
  "run_id": "research-run-001",
  "agent": "tessera-researcher",
  "model": {"providerID": "openai", "id": "APPROVED_MODEL"},
  "account_mode": "included-chatgpt-oauth",
  "workspace": "/srv/tessera/research-workspace",
  "version": "EXACT_INSTALLED_V2_VERSION",
  "schema_sha256": "CANONICAL_APPROVED_SCHEMA_SHA256",
  "control_url": "https://CONTROL_ORIGIN",
  "project_id": "EXACT_ENROLLED_PROJECT_ID",
  "harness_url": "https://HARNESS_ORIGIN",
  "harness_auth_mode": "basic",
  "agent_token_env": "TESSERA_REASONING_AGENT_TOKEN",
  "harness_token_env": "TESSERA_REASONING_HARNESS_TOKEN",
  "grant_id": "APPROVED_GRANT_ID",
  "revision": 1,
  "epoch": 1,
  "subject_credential_id": "ENROLLED_AGENT_CREDENTIAL_ID",
  "destination": "EXACT_DESTINATION",
  "prompt_sha256": "EXACT_PROMPT_SHA256",
  "max_steps": 3,
  "max_tokens": 8192,
  "max_duration_seconds": 120,
  "permissions": [
    {"action": "*", "resource": "*", "effect": "deny"},
    {"action": "tessera_propose", "resource": "*", "effect": "allow"}
  ]
}
```

Use the **actual MCP catalog tool names** when selecting permission actions;
`tessera_propose` above is illustrative, not a protocol claim. Grant `model` must
match the model ID; this is attributed model identity, not attestation of weights.
HTTPS is required off host; literal loopback HTTP is accepted for local checks.
Environment proxies, redirects and cookie persistence are disabled.

```sh
python -m tessera_agent.remote_reasoning_runtime run \
  --profile /srv/tessera/private/reasoning.json \
  --state-dir /srv/tessera/private/reasoning-state \
  --prompt-file /srv/tessera/private/goal.txt
python -m tessera_agent.remote_reasoning_runtime status \
  --profile /srv/tessera/private/reasoning.json \
  --state-dir /srv/tessera/private/reasoning-state
python -m tessera_agent.remote_reasoning_runtime stop \
  --profile /srv/tessera/private/reasoning.json \
  --state-dir /srv/tessera/private/reasoning-state
```

Run under an operator-installed systemd/launchd service, on the authorized host,
as a dedicated user with private state. Use `Restart=on-failure` and a credentials
delivery mechanism rather than inline service-file token values. A nonzero exit
after a fenced run does not restart reasoning: terminal journal states are retained.
Closing Electron does not affect this process. Hard host-supervisor-loss and
independent-host proof still require the actual host test; no laptop-off result is
claimed from Python unit tests.

## Durability and fences

- An immutable run/profile/prompt digest pins the exact authority and produces
  stable upstream session/message IDs. Changing a profile needs a separately
  authorized run identity, not rebinding an old operation.
- Each create/prompt/interrupt intent commits before the HTTP call. A pending or
  uncertain reply is never automatically resent, even if upstream IDs look
  replayable. Restart resumes status polling of acknowledged existing sessions.
  Unknown calls require operator reconciliation; no replacement launch exists.
- Stop, local duration, grant status/revision/epoch/expiry, destination/model and
  remaining budget are checked before new calls; authority-read failure is closed.
  MCP checks the real grant lineage/resource pins on every effect. The supervisor
  also requires the backend's `effective_status=active`, which includes ancestral
  revocation/expiry; missing or fenced effective status blocks admission.
- Total token counters are persisted monotonically. Crossing the observation cap,
  any observed nonzero USD cost, unknown/regressing usage or changed session pins
  triggers interrupt without resuming parked input. This is an **observed-usage
  fence**, not a hard pre-token financial guarantee: one in-flight provider step
  can exceed the observation cap. Require provider/harness hard limits for strict
  token budgets; missing support must block such a profile rather than be relabeled.
- Interrupt acknowledgement is **not** provider termination or experiment cancel.
  `stop_acknowledged` and `stop_unknown` retain that distinction. MCP experiment
  cancellation and provider receipts remain independently authoritative.
- Journal and terminal receipts contain only binding digests, IDs, states and usage.
  No raw prompt, response, secret or hidden chain-of-thought is captured. Public
  research output belongs in governed notebook/artifact tools, not this private
  supervisor journal.

Content-free supervisor provenance now has a transactional durable outbox and
the scoped collector client described in
[runtime-provenance-client.md](runtime-provenance-client.md). Enrollment must
explicitly include `provenance:append`. Profile `project_id` enables one post-run
batch; `tessera-reasoning report` drains another batch without invoking a harness.
Receipts expose pending/recorded counts rather than inventing complete delivery.
Revoked enrollment or stale grant proof remains locally pending; no owner proxy,
proof rewrite or blanket full-notebook completeness claim is made.

## Acceptance

Default isolated tests: `pytest tests/test_remote_reasoning_runtime.py -q`.
Opt-in genuine reasoning, only after the secure prerequisites above:

```sh
python scripts/check_remote_reasoning_live.py --execute-included-oauth \
  --profile /srv/tessera/private/reasoning.json \
  --state-dir /srv/tessera/private/reasoning-state \
  --prompt-file /srv/tessera/private/goal.txt
```

The harness permits <=3 steps, <=120 seconds, one durable prompt and no retries.
It accepts only succeeded OpenCode status, observed positive token usage and zero
observed USD cost. This tests real harness reasoning, not scientific validity,
complete MCP experimentation, full independent-host resilience or provider
termination. Repeat restart/expiry/revocation acceptance against the real installed
host and grant control after the first bounded run; do not treat fixtures as proof.
