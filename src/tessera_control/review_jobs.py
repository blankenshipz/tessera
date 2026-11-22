"""Client-requested research/review jobs, findings and human decisions (ADR 0084).

A job is a durable request for a reviewer. Nothing here runs a model: a runner (the research
journey, a later change) calls ``mark_running``/``record_findings``/``mark_failed``. Without one
a job stays ``queued`` and is never shown as completed. Findings are advisory text from an
untrusted reviewer; a human decision on one changes no plan, proposal, campaign or gate.

Inputs are ``{snapshot_id, digest}`` only. Submit looks each snapshot up in the caller's
project (a missing, foreign-project or foreign-tenant snapshot is the same ``input_not_found``),
checks the digest (``input_digest_mismatch``) and derives the data class from the stored record
(ADR 0039; an unrecorded snapshot reads ``private_derived``). The derived classes, not any
client label, go to the policy and are stored and returned on the job.

``budget_cents`` is a recorded cap only. Nothing reserves or meters it here; a policy may refuse
a cap above its limit, but no spend is held against it.

The runner seams (``mark_running``, ``mark_failed``, ``mark_not_started``) have no HTTP routes.
They accept a human, a worker credential, or a findings-scoped agent that is not the job's
submitter. Separation of duties compares principals, not just credentials: the principal that
submitted a job, or that supplied its findings, can never resolve those findings.
"""

import json
import uuid

from pydantic import ValidationError

from .agent_research import AgentResearchStore
from .approval_trust import require_decision_actor
from .data_class import snapshot_data_class
from .review_jobs_models import (
    ApproveReviewTier,
    IngestFindings,
    ResolveReviewFinding,
    SubmitReviewJob,
)
from .store import Conflict, Forbidden, NotFound, _connect, canonical_digest, utc_now

REFUSAL_REASONS = (
    "destination_not_enrolled",
    "tier_exceeds_policy",
    "data_class_not_allowed_for_destination",
    "budget_exceeds_policy",
    "input_sealed",
    "input_not_found",
    "input_digest_mismatch",
)
JOB_STATES = (
    "awaiting_approval",
    "queued",
    "running",
    "completed",
    "failed",
    "not_started",
    "cancelled",
)


class ReviewRefused(Conflict):
    """A policy (409) or output-validation (422) refusal carrying a stable reason code."""

    def __init__(self, reason: str, status: int = 409):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _declared_partitions(node, found):
    if isinstance(node, dict):
        holdout = node.get("holdout")
        if isinstance(holdout, dict) and isinstance(holdout.get("partition_digest"), str):
            found.add(holdout["partition_digest"])
        for value in node.values():
            _declared_partitions(value, found)
    elif isinstance(node, list):
        for value in node:
            _declared_partitions(value, found)


def _sealed_digests(connection, project_id):
    """Holdout partitions the project declares or claims, in any campaign status, plus the
    digests of lineage nodes recorded as descending (any relation) from one of them."""
    sealed = {
        r[0]
        for r in connection.execute(
            "SELECT DISTINCT partition_digest FROM campaign_holdout_claims WHERE project_id=?",
            (project_id,),
        )
    }
    for row in connection.execute(
        "SELECT spec_json FROM campaigns WHERE project_id=?", (project_id,)
    ):
        try:
            _declared_partitions(json.loads(row[0]), sealed)
        except (TypeError, ValueError):
            continue
    if sealed:
        sealed |= {
            r[0]
            for r in connection.execute(
                """WITH RECURSIVE down(id) AS (
                    SELECT id FROM artifact_lineage_nodes
                    WHERE project_id=? AND sha256 IN (SELECT value FROM json_each(?))
                    UNION
                    SELECT e.child_node_id FROM artifact_lineage_edges e
                    JOIN down d ON e.parent_node_id=d.id WHERE e.project_id=?)
                SELECT n.sha256 FROM artifact_lineage_nodes n JOIN down d ON d.id=n.id""",
                (project_id, json.dumps(sorted(sealed)), project_id),
            )
        }
    return sealed


class NoDestinationsPolicy:
    """Default policy: no destination is enrolled, so every submission is refused.

    A real registry replaces this on ``app.state.review_policy``; the interface is
    ``check(destination_id, tier, data_classes, budget_cents) -> str | None``.
    """

    def check(self, destination_id, tier, data_classes, budget_cents):
        return "destination_not_enrolled"


def create_review_jobs_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS review_jobs (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id),
        actor_id TEXT NOT NULL REFERENCES principals(id),
        kind TEXT NOT NULL CHECK(kind IN
            ('plan_review','dataset_scout','technique_scout','pre_gate_review')),
        title TEXT NOT NULL, inputs_json TEXT NOT NULL,
        prompt_digest TEXT NOT NULL CHECK(length(prompt_digest)=64),
        destination_id TEXT NOT NULL,
        tier TEXT NOT NULL CHECK(tier IN ('haiku','sonnet','opus','fable')),
        budget_cents INTEGER NOT NULL CHECK(budget_cents BETWEEN 0 AND 10000),
        egress TEXT NOT NULL CHECK(egress IN ('none','model_runtime')),
        state TEXT NOT NULL CHECK(state IN ('awaiting_approval','queued','running','completed',
            'failed','not_started','cancelled')),
        reason TEXT, revision INTEGER NOT NULL CHECK(revision>=1),
        digest TEXT NOT NULL CHECK(length(digest)=64),
        receipt_digest TEXT, review_md_digest TEXT, findings_actor_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS review_job_approvals (
        job_id TEXT PRIMARY KEY REFERENCES review_jobs(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        actor_id TEXT NOT NULL REFERENCES principals(id),
        rationale TEXT NOT NULL, job_digest TEXT NOT NULL, created_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS review_findings (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        job_id TEXT NOT NULL REFERENCES review_jobs(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        finding_key TEXT NOT NULL, finding_json TEXT NOT NULL,
        digest TEXT NOT NULL CHECK(length(digest)=64),
        state TEXT NOT NULL CHECK(state IN ('open','accepted','rejected')),
        revision INTEGER NOT NULL CHECK(revision>=1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(job_id, finding_key))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS review_finding_decisions (
        finding_id TEXT PRIMARY KEY REFERENCES review_findings(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        actor_id TEXT NOT NULL REFERENCES principals(id),
        credential_kind TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('accept','reject')),
        rationale TEXT NOT NULL, finding_digest TEXT NOT NULL,
        job_digest TEXT NOT NULL, subject_digests_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS review_jobs_project ON review_jobs(org_id,project_id,sequence)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS review_findings_project "
        "ON review_findings(org_id,project_id,sequence)"
    )
    for table in ("review_job_approvals", "review_finding_decisions"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                f"BEFORE {action} ON {table} BEGIN "
                "SELECT RAISE(ABORT,'review decisions are immutable'); END"
            )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS review_findings_no_delete BEFORE DELETE ON review_findings "
        "BEGIN SELECT RAISE(ABORT,'review findings are permanent'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS review_jobs_no_delete BEFORE DELETE ON review_jobs "
        "BEGIN SELECT RAISE(ABORT,'review jobs are permanent'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS review_findings_content_fixed "
        "BEFORE UPDATE ON review_findings "
        "WHEN NEW.finding_json IS NOT OLD.finding_json OR NEW.digest IS NOT OLD.digest "
        "OR NEW.job_id IS NOT OLD.job_id BEGIN "
        "SELECT RAISE(ABORT,'review finding content is immutable'); END"
    )


class ReviewJobStore(AgentResearchStore):
    # -- shared helpers -------------------------------------------------------------------

    @staticmethod
    def _job_record(connection, row):
        approval = connection.execute(
            "SELECT actor_id,rationale,created_at FROM review_job_approvals WHERE job_id=?",
            (row["id"],),
        ).fetchone()
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "title": row["title"],
            "inputs": json.loads(row["inputs_json"]),
            "data_classes": sorted({i["data_class"] for i in json.loads(row["inputs_json"])}),
            "prompt_digest": row["prompt_digest"],
            "destination_id": row["destination_id"],
            "tier": row["tier"],
            "budget_cents": row["budget_cents"],
            "egress": row["egress"],
            "state": row["state"],
            "reason": row["reason"],
            "revision": row["revision"],
            "digest": row["digest"],
            "receipt_digest": row["receipt_digest"],
            "review_md_digest": row["review_md_digest"],
            "submitter_credential_id": row["credential_id"],
            "submitter_principal_id": row["actor_id"],
            "approval": dict(approval) if approval else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "advisory": True,
            "execution_authorized": False,
        }

    @staticmethod
    def _finding_record(connection, row):
        decision = connection.execute(
            "SELECT * FROM review_finding_decisions WHERE finding_id=?", (row["id"],)
        ).fetchone()
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "project_id": row["project_id"],
            "finding": json.loads(row["finding_json"]),
            "digest": row["digest"],
            "state": row["state"],
            "revision": row["revision"],
            "decision": (
                {
                    "decision": decision["decision"],
                    "rationale": decision["rationale"],
                    "actor_id": decision["actor_id"],
                    "credential_kind": decision["credential_kind"],
                    "finding_digest": decision["finding_digest"],
                    "job_digest": decision["job_digest"],
                    "subject_digests": json.loads(decision["subject_digests_json"]),
                    "created_at": decision["created_at"],
                }
                if decision
                else None
            ),
            "created_at": row["created_at"],
            "untrusted_text": True,
            "advisory": True,
            "execution_authorized": False,
        }

    @staticmethod
    def _load_job(connection, actor, job_id):
        row = connection.execute(
            "SELECT * FROM review_jobs WHERE id=? AND org_id=?", (job_id, actor["org_id"])
        ).fetchone()
        if row is None:
            raise NotFound("Review job was not found.")
        return row

    def _read_actor(self, connection, principal):
        actor = self._actor(connection, principal)
        if actor["role"] != "human":
            self._scope(connection, actor, "review:read")
        return actor

    @staticmethod
    def _bump(connection, job_id, state, revision, reason=None, **columns):
        sets = "state=?,reason=?,revision=?,updated_at=?"
        values = [state, reason, revision + 1, utc_now()]
        for name, value in columns.items():
            sets += f",{name}=?"
            values.append(value)
        connection.execute(f"UPDATE review_jobs SET {sets} WHERE id=?", (*values, job_id))

    # -- submit / approve -----------------------------------------------------------------

    def submit(self, principal, project_id, key, payload, policy):
        body = self._body(SubmitReviewJob, payload)

        def guard(connection, actor):
            if actor["role"] == "agent":
                self._scope(connection, actor, "review:submit")
            elif actor["role"] != "human":
                raise Forbidden("Only a human or an enrolled agent may submit a review job.")
            self._project(connection, actor, project_id)

        def command(connection, actor):
            guard(connection, actor)
            inputs = []
            for item in body["inputs"]:
                found = connection.execute(
                    "SELECT s.digest FROM source_snapshots s JOIN projects p ON p.id=s.project_id "
                    "WHERE s.id=? AND s.project_id=? AND p.org_id=?",
                    (item["snapshot_id"], project_id, actor["org_id"]),
                ).fetchone()
                if found is None:
                    raise ReviewRefused("input_not_found")
                if found["digest"] != item["digest"]:
                    raise ReviewRefused("input_digest_mismatch")
                derived = snapshot_data_class(connection, item["snapshot_id"])["data_class"]
                inputs.append({**item, "data_class": derived})
            sealed = _sealed_digests(connection, project_id)
            if any(item["digest"] in sealed for item in inputs):
                raise ReviewRefused("input_sealed")
            classes = sorted({item["data_class"] for item in inputs})
            reason = policy.check(
                body["destination_id"], body["tier"], classes, body["budget_cents"]
            )
            if reason:
                raise ReviewRefused(
                    reason if reason in REFUSAL_REASONS else "destination_not_enrolled"
                )
            ident = "review_job_" + uuid.uuid4().hex
            state = "awaiting_approval" if body["tier"] == "fable" else "queued"
            now = utc_now()
            connection.execute(
                "INSERT INTO review_jobs (id,org_id,project_id,credential_id,actor_id,kind,"
                "title,inputs_json,prompt_digest,destination_id,tier,budget_cents,egress,state,"
                "revision,digest,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (
                    ident,
                    actor["org_id"],
                    project_id,
                    actor["credential_id"],
                    actor["id"],
                    body["kind"],
                    body["title"],
                    json.dumps(inputs),
                    body["prompt_digest"],
                    body["destination_id"],
                    body["tier"],
                    body["budget_cents"],
                    body["egress"],
                    state,
                    canonical_digest({**body, "inputs": inputs}),
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                None,
                "review_job.submitted",
                1,
                {"job_id": ident, "kind": body["kind"], "tier": body["tier"], "state": state},
            )
            return 201, self._job_record(
                connection,
                connection.execute("SELECT * FROM review_jobs WHERE id=?", (ident,)).fetchone(),
            )

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/review-jobs",
            canonical_digest(body),
            command,
            replay_guard=lambda c, a, r: guard(c, a),
        )

    def approve_tier(self, principal, job_id, key, payload):
        body = self._body(ApproveReviewTier, payload)

        def command(connection, actor):
            self._human(actor)
            row = self._load_job(connection, actor, job_id)
            self._project(connection, actor, row["project_id"])
            if body["expected_revision"] != row["revision"]:
                raise Conflict("Review job revision changed.")
            if row["state"] != "awaiting_approval":
                raise Conflict("Review job is not awaiting tier approval.")
            connection.execute(
                "INSERT INTO review_job_approvals VALUES (?,?,?,?,?,?)",
                (job_id, actor["org_id"], actor["id"], body["rationale"], row["digest"], utc_now()),
            )
            self._bump(connection, job_id, "queued", row["revision"])
            self._event(
                connection,
                actor,
                row["project_id"],
                None,
                "review_job.tier_approved",
                row["revision"] + 1,
                {"job_id": job_id, "tier": row["tier"], "job_digest": row["digest"]},
            )
            return 200, self._job_record(
                connection,
                connection.execute("SELECT * FROM review_jobs WHERE id=?", (job_id,)).fetchone(),
            )

        return self._mutate(
            principal,
            key,
            "POST",
            f"/review-jobs/{job_id}/approve-tier",
            canonical_digest(body),
            command,
            replay_guard=lambda c, a, r: self._human(a),
        )

    # -- runner seams (no model is run here) ----------------------------------------------

    def _runner(self, connection, actor, row):
        """A human, a worker credential, or a findings-scoped agent that is not the submitter."""
        if actor["role"] == "human" or actor["kind"] == "worker":
            return
        if actor["id"] == row["actor_id"] or actor["credential_id"] == row["credential_id"]:
            raise Forbidden("A review job's submitter cannot run it.")
        self._scope(connection, actor, "review:findings")

    def _transition(self, principal, job_id, key, action, allowed, state, reason=None, **columns):
        digest = canonical_digest({"action": action, "reason": reason, **columns})

        def guard(connection, actor):
            row = self._load_job(connection, actor, job_id)
            self._project(connection, actor, row["project_id"])
            self._runner(connection, actor, row)
            return row

        def command(connection, actor):
            row = guard(connection, actor)
            if row["state"] not in allowed:
                raise Conflict(f"Review job cannot move from {row['state']} to {state}.")
            self._bump(connection, job_id, state, row["revision"], reason, **columns)
            self._event(
                connection,
                actor,
                row["project_id"],
                None,
                f"review_job.{state}",
                row["revision"] + 1,
                {"job_id": job_id, "reason": reason},
            )
            return 200, self._job_record(
                connection,
                connection.execute("SELECT * FROM review_jobs WHERE id=?", (job_id,)).fetchone(),
            )

        return self._mutate(
            principal,
            key,
            "POST",
            f"/review-jobs/{job_id}/{action}",
            digest,
            command,
            replay_guard=lambda c, a, r: guard(c, a),
        )

    def mark_running(self, principal, job_id, key):
        return self._transition(principal, job_id, key, "running", {"queued"}, "running")

    def mark_failed(self, principal, job_id, key, reason, receipt_digest=None):
        return self._transition(
            principal,
            job_id,
            key,
            "failed",
            {"queued", "running"},
            "failed",
            reason,
            receipt_digest=receipt_digest,
        )

    def mark_not_started(self, principal, job_id, key, reason):
        return self._transition(
            principal,
            job_id,
            key,
            "not_started",
            {"queued", "awaiting_approval"},
            "not_started",
            reason,
        )

    # -- findings -------------------------------------------------------------------------

    def record_findings(self, principal, job_id, key, payload):
        digest = canonical_digest(payload)

        def guard(connection, actor):
            self._scope(connection, actor, "review:findings")
            row = self._load_job(connection, actor, job_id)
            self._project(connection, actor, row["project_id"])
            if row["credential_id"] == actor["credential_id"] or row["actor_id"] == actor["id"]:
                raise Forbidden("A review job's submitter cannot supply its findings.")
            return row

        def command(connection, actor):
            row = guard(connection, actor)
            if row["state"] not in ("queued", "running"):
                raise Conflict("Review job is not accepting findings.")
            try:
                body = IngestFindings.model_validate(payload).model_dump(mode="json")
            except ValidationError as error:
                raise ReviewRefused("failed_output_invalid", 422) from error
            now = utc_now()
            for item in body["findings"]:
                connection.execute(
                    "INSERT INTO review_findings (id,job_id,org_id,project_id,finding_key,"
                    "finding_json,digest,state,revision,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,'open',1,?,?)",
                    (
                        "review_finding_" + uuid.uuid4().hex,
                        job_id,
                        actor["org_id"],
                        row["project_id"],
                        item["id"],
                        json.dumps(item),
                        canonical_digest({"job_digest": row["digest"], "finding": item}),
                        now,
                        now,
                    ),
                )
            self._bump(
                connection,
                job_id,
                "completed",
                row["revision"],
                None,
                receipt_digest=body["receipt_digest"],
                review_md_digest=body["review_md_digest"],
                findings_actor_id=actor["id"],
            )
            self._event(
                connection,
                actor,
                row["project_id"],
                None,
                "review_job.completed",
                row["revision"] + 1,
                {"job_id": job_id, "findings": len(body["findings"])},
            )
            return 201, {
                "job": self._job_record(
                    connection,
                    connection.execute(
                        "SELECT * FROM review_jobs WHERE id=?", (job_id,)
                    ).fetchone(),
                ),
                "findings": [
                    self._finding_record(connection, f)
                    for f in connection.execute(
                        "SELECT * FROM review_findings WHERE job_id=? ORDER BY sequence", (job_id,)
                    )
                ],
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/review-jobs/{job_id}/findings",
            digest,
            command,
            replay_guard=lambda c, a, r: guard(c, a),
        )

    def resolve(self, principal, finding_id, key, payload):
        body = self._body(ResolveReviewFinding, payload)

        def command(connection, actor):
            self._human(actor)
            require_decision_actor(actor, forbidden=Forbidden)
            row = connection.execute(
                "SELECT * FROM review_findings WHERE id=? AND org_id=?",
                (finding_id, actor["org_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Review finding was not found.")
            self._project(connection, actor, row["project_id"])
            job = connection.execute(
                "SELECT * FROM review_jobs WHERE id=?", (row["job_id"],)
            ).fetchone()
            if actor["id"] in (job["actor_id"], job["findings_actor_id"]):
                raise Forbidden("The submitter or reviewer of a job cannot resolve its findings.")
            if body["expected_digest"] != row["digest"]:
                raise Conflict("Review finding digest changed.")
            existing = connection.execute(
                "SELECT decision,rationale FROM review_finding_decisions WHERE finding_id=?",
                (finding_id,),
            ).fetchone()
            if existing:
                if (existing["decision"], existing["rationale"]) != (
                    body["decision"],
                    body["rationale"],
                ):
                    raise Conflict("Review finding was already resolved differently.")
                return 200, self._resolution(connection, row)
            if body["expected_revision"] != row["revision"]:
                raise Conflict("Review finding revision changed.")
            subjects = [item["digest"] for item in json.loads(job["inputs_json"])]
            connection.execute(
                "INSERT INTO review_finding_decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    finding_id,
                    actor["org_id"],
                    actor["id"],
                    actor["kind"],
                    body["decision"],
                    body["rationale"],
                    row["digest"],
                    job["digest"],
                    json.dumps(subjects),
                    utc_now(),
                ),
            )
            connection.execute(
                "UPDATE review_findings SET state=?,revision=?,updated_at=? WHERE id=?",
                (
                    "accepted" if body["decision"] == "accept" else "rejected",
                    row["revision"] + 1,
                    utc_now(),
                    finding_id,
                ),
            )
            self._event(
                connection,
                actor,
                row["project_id"],
                None,
                "review_finding.resolved",
                row["revision"] + 1,
                {
                    "finding_id": finding_id,
                    "job_id": row["job_id"],
                    "decision": body["decision"],
                    "rationale": body["rationale"],
                    "credential_kind": actor["kind"],
                    "finding_digest": row["digest"],
                    "job_digest": job["digest"],
                    "subject_digests": subjects,
                },
            )
            return 201, self._resolution(connection, row)

        def replay(connection, actor, response):
            self._human(actor)
            require_decision_actor(actor, forbidden=Forbidden)

        return self._mutate(
            principal,
            key,
            "POST",
            f"/review-findings/{finding_id}/resolve",
            canonical_digest(body),
            command,
            replay_guard=replay,
        )

    def _resolution(self, connection, row):
        fresh = connection.execute(
            "SELECT * FROM review_findings WHERE id=?", (row["id"],)
        ).fetchone()
        return self._finding_record(connection, fresh)

    # -- reads ----------------------------------------------------------------------------

    def get_job(self, principal, job_id):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._read_actor(connection, principal)
            row = self._load_job(connection, actor, job_id)
            self._project(connection, actor, row["project_id"])
            return self._job_record(connection, row)
        finally:
            connection.close()

    def list_jobs(self, principal, project_id, offset=0, limit=100):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._read_actor(connection, principal)
            self._project(connection, actor, project_id)
            return [
                self._job_record(connection, row)
                for row in connection.execute(
                    "SELECT * FROM review_jobs WHERE org_id=? AND project_id=? "
                    "ORDER BY sequence LIMIT ? OFFSET ?",
                    (actor["org_id"], project_id, limit, offset),
                )
            ]
        finally:
            connection.close()

    def get_finding(self, principal, finding_id):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._read_actor(connection, principal)
            row = connection.execute(
                "SELECT * FROM review_findings WHERE id=? AND org_id=?",
                (finding_id, actor["org_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Review finding was not found.")
            self._project(connection, actor, row["project_id"])
            return self._finding_record(connection, row)
        finally:
            connection.close()

    def list_findings(self, principal, project_id, job_id=None, state=None, offset=0, limit=100):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._read_actor(connection, principal)
            self._project(connection, actor, project_id)
            return [
                self._finding_record(connection, row)
                for row in connection.execute(
                    "SELECT * FROM review_findings WHERE org_id=? AND project_id=? "
                    "AND (? IS NULL OR job_id=?) AND (? IS NULL OR state=?) "
                    "ORDER BY sequence LIMIT ? OFFSET ?",
                    (actor["org_id"], project_id, job_id, job_id, state, state, limit, offset),
                )
            ]
        finally:
            connection.close()
