# Ordinary installed remote-agent services

These Linux **systemd >=247** examples separate control, supervised reasoning and
an A2A reference peer. They run installed entrypoints/modules, not a development
receipt or desktop process. They are templates for an **operator-selected host**;
adding these files does not select a host, install units, enable services, enroll
agents or authorize inference. No service was started during this implementation.

## Install the reviewed application artifact

On the explicitly selected host, install a reviewed wheel into an immutable
operator-owned virtual environment. Use Python >=3.11. A2A is optional and requires
the pinned official SDK extra `a2a-sdk[http-server]==1.2.1`; MCP is a separate extra.
Do not install an unpinned SDK to get past a compatibility failure.

```sh
sudo python3 -m venv /opt/tessera/venv
sudo /opt/tessera/venv/bin/python -m pip install '/ABS/REVIEWED/tessera_control-0.1.0-py3-none-any.whl[a2a,mcp]'
/opt/tessera/venv/bin/tessera --help
/opt/tessera/venv/bin/tessera-reasoning --help
/opt/tessera/venv/bin/tessera-a2a-peer --help
/opt/tessera/venv/bin/python -m tessera_agent.service_startup --help
```

The absolute reviewed artifact path is supplied by the operator. Dependency
installation is not provider inference. This example does not download OpenCode,
alter the coding service or enroll its owner account. Fresh-wheel installation
and these Linux service lifecycle commands were **not executed** by this macOS
worker. The parent retains distribution/integration acceptance.

Create three distinct service users, private persistent directories and the
configuration directory. These are operator actions on the selected host, not
commands to run on a development laptop by default:

```sh
sudo useradd --system --home-dir /var/lib/tessera-control --shell /usr/sbin/nologin tessera-control
sudo useradd --system --home-dir /var/lib/tessera-reasoning --shell /usr/sbin/nologin tessera-reasoning
sudo useradd --system --home-dir /var/lib/tessera-a2a --shell /usr/sbin/nologin tessera-a2a
sudo install -d -m 0700 -o tessera-control -g tessera-control /var/lib/tessera-control
sudo install -d -m 0700 -o tessera-reasoning -g tessera-reasoning /var/lib/tessera-reasoning
sudo install -d -m 0700 -o tessera-a2a -g tessera-a2a /var/lib/tessera-a2a
sudo install -d -m 0700 /etc/tessera /etc/tessera/credentials
sudo -u tessera-control /opt/tessera/venv/bin/tessera init --state-dir /var/lib/tessera-control
```

Initialization outputs file **paths**, not token values. Leave owner auth material
private in control state; do not insert it in agents, peer profiles, unit files,
shell command lines or the journal. Use the existing human-authenticated enrollment
API to issue separate project/destination-scoped test/workload agent credentials
and bounded authority. Enrollment and grants are not inferred from installation.
For an existing database, take an authorized backup and run the supported `tessera
migrate` workflow before starting the new service; never initialize over a restore.

## Units and references

Copy the reviewed `.service` templates to `/etc/systemd/system/` and run
`sudo systemctl daemon-reload`. Verify on the target with:

```sh
sudo systemd-analyze verify /etc/systemd/system/tessera-control.service \
  /etc/systemd/system/tessera-reasoning.service \
  /etc/systemd/system/tessera-a2a-peer.service
```

This directory's defaults bind **only 127.0.0.1** (control 8765, peer 8771).
There is no public listener, reverse proxy, wildcard CORS, ambient cluster target
or provider host. Remote access requires an independently approved TLS/tunnel
profile with explicit origin/tenant/authentication. Do not simply switch bind to
`0.0.0.0`. The supervisor's configured control/harness destinations require HTTPS
off host and use no proxy/redirect fallback. Each approved A2A instance needs its
own user/state/config/unit and explicit collision-free `listen_port` in its profile;
this supplied unit describes one peer, not two independent peers already running.

| Unit | Private persistent state | Named manager credential sources |
| --- | --- | --- |
| control | `/var/lib/tessera-control` | No agent token; own bootstrap owner credential stays in control state |
| reasoning | `/var/lib/tessera-reasoning` | `profile`, `prompt`, `agent-token`, `harness-password` |
| A2A | `/var/lib/tessera-a2a/peer.sqlite3` | `profile`, `agent-token` |

All source profiles, prompts and credential files under `/etc/tessera` must be
root-owned mode 0600, and its directories mode 0700. Deliver content through the
approved secret provisioner or private-file handoff, never shell literals,
`Environment=TOKEN=...`, unit environment files or printed exports. `LoadCredential`
copies those explicitly named files into the manager's private runtime credential
directory accessible to the service user. The fixed startup mapper validates
private owned regular files and rejects final symlinks, invalid token bytes and
unexpected environment mappings. It never reads OAuth/keychain/auth databases.

Because existing runtime clients consume environment references, the mapper places
only the designated workload tokens in the child environment (not argv/logs). The
service user/root can still inspect that process environment; this is not encrypted
memory or a defense against host root. Distinct service users and OS isolation are
required. Provider API keys, `PYTHONPATH`, loader overrides and arbitrary parent
environment are not forwarded. The mapper cannot run an arbitrary command.

### Reasoning profile and separate harness admission

Prepare `/etc/tessera/reasoning.json` from
[the runtime runbook](../../docs/remote-reasoning-runtime.md), with exact enrolled
credential/grant/revision/epoch, model/destination, prompt digest, usage bounds and
approved dedicated harness release/schema/permissions. For this startup profile,
the environment names are fixed:

- `agent_token_env`: `TESSERA_REASONING_AGENT_TOKEN`, mapped from
  `/etc/tessera/credentials/reasoning-agent-token`.
- `harness_token_env`: `TESSERA_REASONING_HARNESS_TOKEN`, mapped from
  `/etc/tessera/credentials/reasoning-harness-password`.
- `harness_auth_mode`: `basic` for the inspected native V2 endpoint. Its password
  authenticates HTTP only; it is neither an owner Tessera token nor provider key.
- Prompt bytes: `/etc/tessera/reasoning-prompt.txt` (at most 8192 bytes), delivered
  through the `prompt` reference. The digest must match exact bytes.

The service never starts OpenCode. Its harness is a **separately approved externally
supervised operator service**, with included owner-bound ChatGPT OAuth and pinned
financial guardrails. See the exact guarded setup in the runtime runbook. The
locally inspected guarded wrapper is macOS-specific and fixes owner config/data
paths; do not transplant those owner paths/account files to a Linux worker. An
approved compatible guarded harness service and secure HTTP reference are still
required; raw OpenCode or a shared coding endpoint is not a fallback.

Reasoning has **no `[Install]` target** and requires
`/etc/tessera/reasoning.admitted`. Only after profile/harness/grant admission should
the operator create this empty marker, then explicitly start one bounded run:

```sh
sudo install -m 0600 /dev/null /etc/tessera/reasoning.admitted
sudo systemctl start tessera-reasoning.service
sudo systemctl status tessera-reasoning.service
```

Creating the marker is an operator readiness assertion, **not** human grant issue
or billing proof. It cannot bypass runtime/MCP grant, epoch, expiry or quota checks.
Do not `enable` this unit or add it to a boot target. A completed/failed/fenced run
retains its journal. An OS/process failure can resume acknowledged sessions under
`Restart=on-failure`; pending/uncertain calls cannot be replayed. Exit 2 (fenced
run) and 78 (invalid startup references) suppress automatic restart. No automatic
new run identity/profile or replacement inference is created.

### A2A profile

Copy `a2a-peer.example.json` into the private `/etc/tessera/a2a-peer.json`, replacing
all identifiers and the peer bearer digest with enrolled identities. `identities`
maps the SHA-256 of a **separately issued peer bearer** to exact tenant/project/
sender identity; it does not contain the bearer itself. The supplied placeholder
must not be used as an authenticated live profile. `control_token_env` must be
`TESSERA_A2A_CONTROL_TOKEN`, mapped from the recipient workload credential source
`/etc/tessera/credentials/a2a-control-agent-token`. Database must be exactly
`/var/lib/tessera-a2a/peer.sqlite3` for this unit. Use a separate profile/state per peer.
`listen_port` defaults to 8771 and may be an operator-selected integer 1024..65535
for another instance; the mapper still fixes the bind address to `127.0.0.1`.
Keep the advertised `public_url` consistent with the admitted listener/TLS route.

After enrollment and peer identity/delegation review, create the admission marker
and explicitly start the installed pinned-SDK peer:

```sh
sudo install -m 0600 /dev/null /etc/tessera/a2a-peer.admitted
sudo systemctl start tessera-a2a-peer.service
sudo systemctl status tessera-a2a-peer.service
```

This A2A 1.0 JSON-RPC service is a governed **reference exchange**, not an agent
reasoner or arbitrary experiment runner. Delegated effective authority is checked
through its own control credential. Restart preserves the reference/task ledger.
An A2A task cancel changes the peer ledger; it is not proof of experiment/provider
termination. No hidden inference, SDK mock or invented protocol receipt is involved
in the service examples.

## Start, enable, stop and drain

Only control may be enabled for boot after initialized-state/readiness review:

```sh
sudo systemctl enable --now tessera-control.service
sudo systemctl status tessera-control.service
```

`active` means a live OS process, not authenticated API readiness or completed
research. Check the existing control `/health/ready` contract, authenticated scoped
API access and schema readiness before admitting work. Process status and an A2A
agent card are not model-reasoning or experiment-completion evidence.

**Drain reasoning:** first use the human control API to revoke/expire the exact
agent authority (and separately cancel named execution grants when needed). Then
`sudo systemctl stop tessera-reasoning.service`. Its fixed `ExecStop` records the
local durable stop request before systemd signals the supervisor. The supervisor
fences new calls and asks only the dedicated harness session to interrupt with
resume disabled. Read the private runtime terminal receipt/journal:
`stop_acknowledged` is not provider termination; `stop_unknown` needs reconciliation.
If SIGKILL, timeout or host loss occurs before acknowledgement, no exit code proves
termination. The next admitted restart must use the same private journal; uncertain
effects remain uncertain. Do not replace the run to conceal them.

**Drain A2A:** stop new submissions by `sudo systemctl stop tessera-a2a-peer.service`;
retained tasks/messages/references survive. Resolve/cancel outstanding peer tasks
through the typed peer interface before stopping when required. Cancel real
execution separately through Tessera. Do not delete the ledger to make it look drained.

**Drain control:** revoke/cancel/drain named authority and remote workers using the
existing worker workflow, verify independent provider termination/accounting as
required, then `sudo systemctl stop tessera-control.service`. Closing Electron or
stopping control does not terminate remote work automatically. Reuse
[the remote dispatch worker deployment](../kubernetes/remote-dispatch-worker.example.yaml)
and its approved lease/channel/output workflow; these templates do not modify it.

Remove admission markers to block subsequent manual reasoning/peer starts only
after recording the stop state; removing a marker does not stop an already running
unit. `systemctl disable tessera-control` removes boot enablement, not a running
process; use `stop` separately. No automated destructive teardown is supplied.

## Evidence and limitations

Static/synthetic tests verify fixed command selection, credential mapping/denials,
private references, stop persistence and unit hardening/defaults. They do **not**
validate Linux systemd sandbox behavior, TLS, an installed independent host, real
OAuth reasoning, A2A peer traffic, provider termination or laptop-off continuity.
`systemd-analyze` is not installed on this macOS environment. The root owns those
remaining live/install gates; there is no inference or host-deployment prerequisite
for implementing and checking these install profiles.
