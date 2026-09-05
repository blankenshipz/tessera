# 0001: Local control-plane foundation

Status: accepted for the first implementation slice, 2026-09-05.

Use Python 3.11+, FastAPI, Pydantic, and stdlib SQLite. The backend runs as a
separate process; Electron is not required to use or test it. Python supports
research tooling, FastAPI provides request schemas and testable HTTP endpoints,
and SQLite keeps a local slice runnable without operating a database server.

This implements part of DESIGN.md's durable-control step, not a completed harness
feasibility spike. Persist tenant-scoped projects, immutable draft task inputs,
human approval decisions, idempotency responses, and ordered labbook events.
Keep each mutation and its event/replay result in one transaction.

Bootstrap creates one owner credential under a private state directory. The
database stores a token digest; the client header file is local secret material.
This is limited local bearer authentication, not production organization login,
short-lived workload identity, or a complete credential broker. Bind to loopback;
reject browser origins until an authenticated desktop/browser integration exists.
The owner bearer credential identifies the human role by possession only; it must
not be shared with agents. Human-session proof and credential rotation are later
work. Development Compose binds inside its container to all interfaces but
publishes only on host loopback. uv and a committed uv.lock manage dependencies;
Compose packages development/tests, not production deployment.

Alternative: TypeScript backend sharing client types. We choose Python for this
small standalone service; HTTP schemas remain the client boundary. PostgreSQL is
the intended shared-deployment candidate, but no migration adapter is fabricated
before relational schemas and workflows settle.

Not included: real job execution, billing, grants to workers, Electron, scientific
claim acceptance, production auth/hosting, or secret-provider integrations.
Approval records plan intent only. The next execution slice must prove isolation,
leases/epochs, cancellation, credential expiry and provider accounting before
running real research code. A future database change requires migration tests
and state/event/replay conformance; do not dual-write two authorities.
