# ADR 0127: the execution-monitor receiver listens inside the control service, opt-in

Status: proposed. Implements the "TLS server/service integration" gate that
[ADR 0022](0022-transferable-execution-monitor.md) left open. Operator attestation
as the authority for "continue" is an **interim** measure (see Decision and Open
questions). No schema change and no HTTP route.

## Context

The execution monitor and its `MonitorReceiver` (ADR 0022) existed only as a
library: nothing in the product accepted a monitor connection, so a monitor had no
control-side peer to report to (README gap G-20). ADR 0022 listed deployment
enrollment and TLS service integration as unfinished production gates. Control does
not yet issue monitor assignments, so there is no control-side record to consult
for "may this monitor keep running its workload".

## Decision

`tessera-control serve --monitor-enrollments FILE` starts, **only when given**, an
mTLS listener inside the control service that runs the existing receiver and the
existing framed protocol (challenge/response, evidence/ack). Opt-in and off by
default: without the flag nothing starts and no port opens.

* **Enrollment file** (`tessera.monitor-enrollments.v1`) must be owner-only (0600,
  no symlink, absolute paths). It names the `listener` (`bind_ip` numeric, `port`,
  `server_cert`, `server_key`, `client_ca`) and each enrolled monitor (`monitor`,
  `tenant`, `project`, `assignment` path, `authority_key`, `channel_key` and
  `evidence_key` paths to 32-byte owner-private key files, never inline keys,
  `client_cert_sha256`, `worker_credential_id`, `active`).
* **Startup validates everything and refuses on any problem** (permissions,
  symlinks, unreadable, wrongly sized or reused keys, malformed or unknown fields,
  duplicate ids or pins, an assignment that does not open under its authority key or
  names another tenant, project or monitor, a journal already holding a different
  assignment). Errors name the entry, never a path or key.
* **Selection:** a connection is accepted only over TLS 1.3 from a client
  certificate whose SHA-256 matches an enrollment; that match selects the receiver.
  At most 8 connections, one per monitor, with handshake and frame deadlines and a
  10-minute lifetime. Each monitor has its own durable journal under
  `<state dir>/execution-monitors/<monitor>/`.
* **Authority for "continue" (interim):** until control issues monitor assignments
  itself, **operator enrollment is the authority**. The answer is "continue" only
  while all hold, otherwise "revoke": the entry is present and active in the file
  (re-read at each check), the sealed assignment is unexpired and byte-identical to
  the enrolled one, and the worker's control credential is live. Operator
  attestation is an interim authority, to be replaced by control-side assignment
  issuance.
* Artifact evidence stays refused (no custody integration yet).

## Trust statement

This is operator attestation, not discovery. A monitor is trusted because the
owner wrote its certificate pin, assignment and keys into the file; nothing is
learned from the network and there is no self-enrollment. To revoke, remove the
entry or set `active` to false (takes effect at the next check, no restart), revoke
the worker's control credential, or let the assignment expire. Revocation is stop
only: the monitor is told to revoke, never to continue.

## Does not do

No HTTP route or self-enrollment endpoint. No issuance of assignments, broker
receipts or global claims. `status()` stays `activation: blocked`. No artifact
custody, no provider activation, no change to Docker/Kubernetes/Salad adapters, and
no reading of the file by agents or the desktop.

## Consequences

* A monitor can now report to a running service, closing G-20 for the opt-in path.
* Control's "continue" is only as good as the operator's file. Because it is
  interim, a stale file with an active entry keeps answering "continue" until
  expiry, so assignments stay short (ADR 0022 caps them at 120 seconds).
* Enabling the listener opens one more network surface, behind mTLS with pinned
  client certificates; the default install is unchanged.
* The enrollment file holds paths, never key material.
* Limits of this slice: the file is re-validated whole at every check, so one bad
  edit makes every monitor answer "revoke" until fixed; new or changed entries need
  a restart (a changed entry answers "revoke" until then); an expired assignment's
  challenge is refused as stale and the connection closes, so the monitor sees a
  closed channel rather than a signed "revoke" (never "continue"); evidence uploads
  are still accepted after revocation so stop and recovery receipts reach the
  journal; a monitor id keeps one assignment per journal; and the assignment's
  `worker` name is not matched to the credential's principal (no mapping exists yet;
  the credential is checked by id within the entry's tenant and project).

## Tests

Default isolated tests (`tests/test_monitor_service.py`, localhost mTLS with
generated certificates) cover: no flag means nothing is constructed or bound; a bad
file exits serve with status 2; each startup refusal above; the listener accepting
while serving and closed after shutdown; the pinned monitor getting "continue" with
its evidence acked and journaled; unpinned and foreign-CA clients dropped with
nothing journaled; a second connection for one monitor dropped; and revocation on
the same connection, each answering "revoke": entry set inactive, entry removed,
file permissions loosened, pin changed, assignment file replaced, worker credential
revoked, then "continue" again once restored. Expiry is tested at the authorizer and
over the wire (connection closed, never "continue"). `status()` stays blocked. No
live, provider or paid test is involved.

## Open questions

* Control-side assignment issuance replacing operator attestation (the planned
  replacement for the interim authority above).
* Custody integration so artifact evidence can be accepted.
* A read-only status surface for enrolled monitors and listener state.
