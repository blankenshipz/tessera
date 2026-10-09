"""At-most-once suspended-Job lifecycle and explicit, networkless fake client.

No kubectl, ambient kubeconfig, credential discovery, live client or automatic retry.
The default real-effects broker is deliberately NOT enabled by this module.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import UTC, datetime
from functools import wraps

from tessera_control.store import canonical_digest

from .capability_activation import canonical, stamp
from .capability_effects import BackendBlocked
from .capability_inspection import RawEvidenceInspector, raw_object
from .capability_journal import CapabilityJournal


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)

    return call


class RunnerJournal(CapabilityJournal):
    def __init__(self, root):
        super().__init__(root)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS capability_runs (
                    operation TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL,
                    package TEXT UNIQUE NOT NULL, plan_digest TEXT UNIQUE NOT NULL,
                    plan BLOB NOT NULL, evidence TEXT UNIQUE, uid TEXT,
                    state TEXT NOT NULL, cancel_at TEXT);
                CREATE TABLE IF NOT EXISTS capability_run_events (
                    operation TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(operation,sequence));
                CREATE TABLE IF NOT EXISTS capability_runner_heartbeats (
                    operation TEXT PRIMARY KEY, sequence INTEGER NOT NULL,
                    observed_at TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS capability_events_no_update
                    BEFORE UPDATE ON capability_run_events
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS capability_events_no_delete
                    BEFORE DELETE ON capability_run_events
                    BEGIN SELECT RAISE(ABORT,'retained receipts'); END;
                CREATE TRIGGER IF NOT EXISTS capability_runs_no_delete
                    BEFORE DELETE ON capability_runs
                    BEGIN SELECT RAISE(ABORT,'retained nonce'); END;
                CREATE TRIGGER IF NOT EXISTS capability_runs_immutable_binding
                    BEFORE UPDATE OF operation,nonce,package,plan_digest,plan ON capability_runs
                    BEGIN SELECT RAISE(ABORT,'immutable binding'); END;
                CREATE TRIGGER IF NOT EXISTS capability_runs_immutable_evidence
                    BEFORE UPDATE OF evidence ON capability_runs WHEN OLD.evidence IS NOT NULL
                    BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
                CREATE TRIGGER IF NOT EXISTS capability_runs_immutable_uid
                    BEFORE UPDATE OF uid ON capability_runs WHEN OLD.uid IS NOT NULL
                    BEGIN SELECT RAISE(ABORT,'immutable job'); END;
            """)

    def begin(self, plan, now):
        m = plan.activation
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT 1 FROM capability_runs WHERE state NOT IN ('blocked','terminated')"
                ).fetchone():
                    raise ValueError
                db.execute(
                    "INSERT INTO capability_runs VALUES (?,?,?,?,?,NULL,NULL,'inspecting',NULL)",
                    (m.operation_id, m.nonce, m.package_digest, plan.digest, canonical(plan)),
                )
                self.event(db, m.operation_id, "inspecting", now)
        except Exception:
            raise BackendBlocked("runner replay, unresolved effect or custody conflict") from None

    def event(self, db, operation, state, now, *, evidence=None):
        previous = db.execute(
            "SELECT payload FROM capability_run_events WHERE operation=? "
            "ORDER BY sequence DESC LIMIT 1",
            (operation,),
        ).fetchone()
        prior = json.loads(previous[0]) if previous else None
        row = db.execute("SELECT * FROM capability_runs WHERE operation=?", (operation,)).fetchone()
        receipt = {
            "operation": operation,
            "package": row["package"],
            "nonce": row["nonce"],
            "plan_digest": row["plan_digest"],
            "state": state,
            "observed_at": now.isoformat(),
            "sequence": prior["sequence"] + 1 if prior else 0,
            "previous_digest": canonical_digest(prior) if prior else "0" * 64,
            "evidence_digest": evidence or row["evidence"],
            "job_uid": row["uid"],
            "source": "fake-kubernetes",
            "custody": "quarantined_unreleased",
        }
        db.execute("UPDATE capability_runs SET state=? WHERE operation=?", (state, operation))
        db.execute(
            "INSERT INTO capability_run_events VALUES (?,?,?)",
            (operation, receipt["sequence"], json.dumps(receipt, sort_keys=True)),
        )

    def transition(self, operation, state, now):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.event(db, operation, state, now)
        return state

    def row(self, plan):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM capability_runs WHERE operation=?", (plan.activation.operation_id,)
            ).fetchone()
        if row is None or row["plan_digest"] != plan.digest:
            raise BackendBlocked("unknown or mismatched runner operation")
        return row

    def receipts(self, operation):
        with self.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM capability_run_events WHERE operation=? ORDER BY sequence",
                    (operation,),
                )
            ]


class KubernetesCapabilityRunner:
    def __init__(self, *, client, journal, verifier, claim, now=None, evidence_collector=None):
        # No accidental live use through an object claiming capabilities or signed booleans.
        if type(client) is not FakeKubernetesCapabilityClient:
            raise BackendBlocked("real capability client/collector conformance unavailable")
        self.client, self.journal, self.verifier, self.claim = client, journal, verifier, claim
        self._mutex = threading.RLock()
        self.now = now or (lambda: datetime.now(UTC))
        if evidence_collector is not None:
            from .capability_collector import KubernetesEvidenceCollector

            if type(evidence_collector) is not KubernetesEvidenceCollector:
                raise BackendBlocked("authenticated collector required")
        self.inspector = RawEvidenceInspector(client=evidence_collector or client, now=self.now)
        journal.attach()
        with journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute(
                "SELECT operation FROM capability_runs "
                "WHERE state NOT IN ('blocked','terminated','unknown')"
            ).fetchall():
                journal.event(db, row[0], "unknown", self.now())

    @serialized
    def submit(self, plan, verified):
        plan.validate_package(verified, self.verifier)
        self.journal.begin(plan, self.now())  # burns the operation/nonce even on failed evidence
        op = plan.activation.operation_id
        effect = False
        try:
            evidence = self.inspector.inspect(plan)
            plan.validate_package(verified, self.verifier)
            # Authenticated global operation claim must precede any create; no retry on loss.
            self.journal.transition(op, "claim_intent", self.now())
            effect = True  # a lost global claim reply must be reconciled, not inferred denied
            if self.claim(verified.package, self.journal.identity) is not True:
                effect = False
                raise BackendBlocked("global operation claim denied")
            with self.journal.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE capability_runs SET evidence=? WHERE operation=?", (evidence, op)
                )
                self.journal.event(db, op, "create_intent", self.now())
            effect = True
            job = self.client.create_suspended(plan, plan.job())
            uid = self._job(plan, job, suspended=True)
            with self.journal.connect() as db:
                db.execute("UPDATE capability_runs SET uid=? WHERE operation=?", (uid, op))
                self.journal.event(db, op, "suspended", self.now())
            # Arm exact UID before any workload can be scheduled. Lost arm reply never resumes.
            self.client.arm(plan, uid)
            self._heartbeat(plan, uid)
            plan.validate_package(verified, self.verifier)
            self.journal.transition(op, "start_intent", self.now())
            self.client.unsuspend(plan, uid)
            return self.journal.transition(op, "pending", self.now())
        except Exception:
            return self.journal.transition(op, "unknown" if effect else "blocked", self.now())

    def _job(self, plan, job, *, suspended):
        if raw_object(job) != plan.job(suspended=suspended) or not job["metadata"].get("uid"):
            raise BackendBlocked("Job readback mismatch")
        return job["metadata"]["uid"]

    def _heartbeat(self, plan, uid):
        m = plan.activation
        heartbeat = self.client.heartbeat(plan, uid)
        if set(heartbeat) != {
            "identity",
            "certificate_sha256",
            "nonce",
            "job_uid",
            "sequence",
            "observed_at",
            "deadline",
        }:
            raise ValueError
        if (
            heartbeat["identity"],
            heartbeat["certificate_sha256"],
            heartbeat["nonce"],
            heartbeat["job_uid"],
        ) != (m.watchdog.identity, m.watchdog.certificate_sha256, m.nonce, uid):
            raise ValueError
        if (
            type(heartbeat["sequence"]) is not int
            or heartbeat["sequence"] < 1
            or not 0
            <= (self.now() - stamp(heartbeat["observed_at"])).total_seconds()
            <= m.watchdog.heartbeat_seconds
            or not self.now() < stamp(heartbeat["deadline"]) <= stamp(m.expires_at)
            or (stamp(heartbeat["deadline"]) - self.now()).total_seconds()
            > m.watchdog.lease_seconds
        ):
            raise ValueError
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT sequence,observed_at FROM capability_runner_heartbeats WHERE operation=?",
                (m.operation_id,),
            ).fetchone()
            if heartbeat["sequence"] != (previous[0] + 1 if previous else 1) or (
                previous and stamp(heartbeat["observed_at"]) < stamp(previous[1])
            ):
                raise ValueError
            db.execute(
                "INSERT OR REPLACE INTO capability_runner_heartbeats VALUES (?,?,?)",
                (m.operation_id, heartbeat["sequence"], heartbeat["observed_at"]),
            )

    @serialized
    def monitor(self, plan, verified):
        row = self.journal.row(plan)
        if row["state"] not in {"pending", "running"}:
            return row["state"]  # unknown never becomes runnable again
        try:
            plan.validate_package(verified, self.verifier)
            self._heartbeat(plan, row["uid"])
            observation = self.client.observe(plan)
            self.inspector.fresh(observation["observed_at"], plan.activation, self.now())
            if self._job(plan, observation["job"], suspended=False) != row["uid"]:
                raise ValueError
            pods = observation["pods"]
            if pods["kind"] != "PodList" or len(pods["items"]) != 1:
                raise ValueError
            pod = pods["items"][0]
            want = plan.job(suspended=False)["spec"]["template"]
            if (
                pod["spec"] != want["spec"]
                or pod["metadata"]["labels"] != plan.labels
                or pod["metadata"]["annotations"] != plan.annotations
                or pod["metadata"]["namespace"] != plan.activation.namespace
                or pod["metadata"]["ownerReferences"]
                != [{"kind": "Job", "name": plan.name, "uid": row["uid"], "controller": True}]
            ):
                raise ValueError
            statuses = pod["status"]["containerStatuses"]
            if len(statuses) == 1 and "terminated" in statuses[0].get("state", {}):
                terminal = statuses[0]["state"]["terminated"]
                if (
                    set(terminal) != {"exitCode", "finishedAt"}
                    or type(terminal["exitCode"]) is not int
                    or not 0 <= terminal["exitCode"] <= 255
                    or not stamp(plan.activation.issued_at)
                    <= stamp(terminal["finishedAt"])
                    <= self.now()
                    or statuses[0]
                    != {
                        "name": "installer",
                        "ready": False,
                        "restartCount": 0,
                        "imageID": plan.activation.image,
                        "state": {"terminated": terminal},
                    }
                ):
                    raise ValueError
                succeeded = terminal["exitCode"] == 0
                if observation["job"].get("status") != {
                    "active": 0,
                    "succeeded": int(succeeded),
                    "failed": int(not succeeded),
                    "conditions": [
                        {"type": "Complete" if succeeded else "Failed", "status": "True"}
                    ],
                }:
                    raise ValueError
                # Process stop only. Installer receipt/custody, not exit 0, proves install success.
                return self.journal.transition(row["operation"], "terminated", self.now())
            if len(statuses) != 1 or statuses[0] != {
                "name": "installer",
                "ready": True,
                "restartCount": 0,
                "imageID": plan.activation.image,
                "state": {"running": {}},
            }:
                raise ValueError
            return self.journal.transition(row["operation"], "running", self.now())
        except Exception:
            self.journal.transition(row["operation"], "unknown", self.now())
            # External watchdog owns hard expiry. Do not repeat uncertain delete here.
            return "unknown"

    @serialized
    def cancel(self, plan):
        row = self.journal.row(plan)
        if row["cancel_at"] or row["state"] in {"blocked", "terminated"}:
            return row["state"]
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE capability_runs SET cancel_at=? WHERE operation=?",
                (self.now().isoformat(), row["operation"]),
            )
            self.journal.event(db, row["operation"], "cancel_requested", self.now())
        try:
            if not row["uid"]:
                raise ValueError
            # Exact UID+nonce preconditions; acknowledgement is not termination.
            self.client.delete(plan, row["uid"], plan.activation.nonce)
        except Exception:
            return self.journal.transition(row["operation"], "unknown", self.now())
        return "cancel_requested"

    @serialized
    def reconcile(self, plan):
        row = self.journal.row(plan)
        if row["state"] in {"blocked", "terminated"}:
            return row["state"]
        try:
            observation = self.client.observe(plan)
            if not row["cancel_at"] or not row["uid"]:
                raise ValueError
            # Historical stop evidence may arrive after activation expires.
            observed = stamp(observation["observed_at"])
            if (
                not stamp(row["cancel_at"]) <= observed <= self.now()
                or (self.now() - observed).total_seconds() > 5
            ):
                raise ValueError
            if observation != {
                "observed_at": observation["observed_at"],
                "nonce": plan.activation.nonce,
                "namespace_uid": plan.activation.namespace_uid,
                "job_uid": row["uid"],
                "job": {"kind": "Status", "code": 404, "reason": "NotFound"},
                "pods": {"kind": "PodList", "items": []},
            }:
                raise ValueError
            return self.journal.transition(row["operation"], "terminated", self.now())
        except Exception:
            return self.journal.transition(row["operation"], "unknown", self.now())


class FakeKubernetesCapabilityClient:
    """Only explicit test observations. Does not synthesize successful probe evidence."""

    def __init__(self, *, snapshot, heartbeat):
        self.snapshot, self.heartbeat_response = snapshot, heartbeat
        self.calls, self.jobs = [], {}
        self.observation = None
        self.fail_at = None

    def _call(self, name):
        self.calls.append(name)
        if self.fail_at == name:
            raise OSError("fake upstream failure (not retained)")

    def collect(self, plan, requests):
        self._call("collect")
        return copy.deepcopy(self.snapshot)

    def create_suspended(self, plan, manifest):
        plan.validate_job(manifest)
        if plan.name in self.jobs:
            raise BackendBlocked("Job already exists")
        job = copy.deepcopy(manifest)
        job["metadata"].update(uid="fake-job-uid", resourceVersion="1")
        self.jobs[plan.name] = job
        self._call("create")  # can model applied create followed by lost reply
        return copy.deepcopy(job)

    def arm(self, plan, uid):
        self._call("arm")

    def heartbeat(self, plan, uid):
        self._call("heartbeat")
        return copy.deepcopy(self.heartbeat_response)

    def unsuspend(self, plan, uid):
        self._call("unsuspend")
        job = self.jobs[plan.name]
        if job["metadata"]["uid"] != uid:
            raise ValueError
        job["spec"]["suspend"] = False

    def observe(self, plan):
        self._call("observe")
        return copy.deepcopy(self.observation)

    def delete(self, plan, uid, nonce):
        self._call("delete")
        job = self.jobs[plan.name]
        if (job["metadata"]["uid"], job["metadata"]["annotations"]) != (
            uid,
            plan.annotations,
        ) or nonce != plan.activation.nonce:
            raise BackendBlocked("cleanup ownership mismatch")
        del self.jobs[plan.name]  # never namespace, PVC, artifacts, receipts or foreign resources
