"""Host-independent monitor receiver. No namespace access and no effect authority.

Compose beside the authenticated control service with an explicitly enrolled
assignment/monitor key, NEVER keys or tenant identity from an upload. This separate
ledger does not manufacture broker receipts or grant autonomy activation.
"""

from pydantic import Field

from tessera_monitor.protocol import (
    Assignment,
    Denied,
    Digest,
    Frozen,
    Pulse,
    Receipt,
    bound,
    digest,
    open_envelope,
    seal,
)


class Ack(Frozen):
    assignment: Digest
    nonce: Digest
    sequence: int = Field(ge=1, le=1024)
    receipt: Digest


class MonitorReceiver:
    def __init__(
        self,
        document,
        *,
        journal,
        authority_key,
        channel_key,
        evidence_key,
        tenant,
        project,
        monitor,
        now,
        custody=lambda a, artifact: False,
    ):
        self.a = open_envelope(document, Assignment, authority_key, b"assignment")
        if (self.a.tenant, self.a.project, self.a.monitor) != (tenant, project, monitor):
            raise Denied("monitor enrollment mismatch")
        if len({authority_key, channel_key, evidence_key}) != 3 or any(
            type(key) is not bytes or len(key) != 32
            for key in (authority_key, channel_key, evidence_key)
        ):
            raise Denied("distinct monitor keys required")
        self.journal, self.channel_key, self.evidence_key, self.now = (
            journal,
            channel_key,
            evidence_key,
            now,
        )
        self.custody = custody
        journal.claim(document, self.a)
        self.records()
        with journal.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS monitor_challenges ("
                "sequence INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL)"
            )

    def records(self):
        records = self.journal.receipts(self.evidence_key)
        for receipt in records:
            bound(receipt, self.a)
        return records

    def respond(self, document, *, authorized):
        # authorized is a trusted control-plane use-time check of worker, plan,
        # package, operation, lease/epoch and revocation; no workload-supplied bool.
        pulse = open_envelope(document, Pulse, self.channel_key, b"challenge")
        bound(pulse, self.a)
        if (
            pulse.action != "challenge"
            or not self.a.issued_ms <= self.now() < self.a.expires_ms
            or not 0 <= self.now() - pulse.issued_ms < self.a.limits.lease_seconds * 1000
        ):
            raise Denied("stale monitor challenge")
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            seq = db.execute("SELECT max(sequence) FROM monitor_challenges").fetchone()[0] or 0
            if pulse.sequence != seq + 1:
                raise Denied("replayed monitor challenge")
            db.execute(
                "INSERT INTO monitor_challenges VALUES (?,?)", (pulse.sequence, digest(pulse))
            )
        try:
            allowed = authorized(self.a) is True
        except Exception:
            allowed = False
        if self.now() >= self.a.expires_ms:
            allowed = False
        return seal(
            pulse.model_copy(update={"action": "continue" if allowed else "revoke"}),
            self.channel_key,
            b"response",
        )

    def ingest(self, document):
        receipt = open_envelope(document, Receipt, self.evidence_key, b"evidence")
        bound(receipt, self.a)
        if receipt.observed_ms < self.a.issued_ms or receipt.observed_ms > self.now():
            raise Denied("invalid evidence clock")
        for observation in receipt.observations:
            if observation.artifact is not None:
                artifact = observation.artifact
                facts = {f.name: f.value for f in observation.facts}
                if (
                    artifact.size > self.a.limits.output_bytes
                    or facts.get("sha256") != artifact.sha256
                    or facts.get("bytes") != artifact.size
                    or facts.get("writers_frozen") is not True
                    or self.custody(self.a, artifact) is not True
                ):
                    raise Denied("artifact custody unconfirmed")
        records = self.records()
        if receipt.sequence <= len(records):
            existing = self.journal.documents(receipt.sequence - 1, 1)[0]
            if existing != document:
                raise Denied("conflicting evidence replay")
        else:
            if records and (
                receipt.observed_ms < records[-1].observed_ms
                or receipt.elapsed_ms < records[-1].elapsed_ms
            ):
                raise Denied("evidence clock regression")
            self.journal.append(document, receipt)
        return seal(
            Ack(
                assignment=digest(self.a),
                nonce=self.a.nonce,
                sequence=receipt.sequence,
                receipt=digest(document),
            ),
            self.channel_key,
            b"ack",
        )

    def status(self):
        records = self.records()
        latest = {o.category: o for r in records for o in r.observations}
        fresh = (
            bool(records)
            and 0 <= self.now() - records[-1].observed_ms < (self.a.limits.lease_seconds * 1000)
            and self.now() < self.a.expires_ms
        )
        unknown = [k for k in self.a.required if k not in latest or latest[k].state != "verified"]
        authentic = bool(records) and all(r.source == "execution-monitor" for r in records)
        # A signature isn't target conformance. No deployed enforcing production
        # adapter is enabled; fake/closed-fixture receipts can never activate work.
        return dict(
            activation="blocked",
            fresh=fresh,
            unknown=unknown,
            authentic=authentic,
            termination_confirmed=bool(
                latest.get("termination") and latest["termination"].state == "verified"
            ),
            cleanup_confirmed=bool(latest.get("cleanup") and latest["cleanup"].state == "verified"),
        )


def accept_ack(journal, document, assignment, key):
    ack = open_envelope(document, Ack, key, b"ack")
    if (ack.assignment, ack.nonce) != (digest(assignment), assignment.nonce):
        raise Denied("acknowledgement binding mismatch")
    journal.acknowledge(ack.sequence, ack.receipt)
