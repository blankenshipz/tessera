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
import math
import uuid

from pydantic import ValidationError

from .agent_research import AgentResearchStore
from .approval_trust import require_decision_actor
from .code_review_jobs import (
    CodeReviewRunStore,
    append_attempt,
    attempt_entry,
    code_review_record,
    create_code_review_schema,
    receipt_bound,
    recheck_output_valid,
    source_record,
)
from .data_class import snapshot_data_class
from .license_labels import snapshot_labels
from .review_jobs_models import (
    BILLED_COST_BASES,
    ApproveReviewTier,
    IngestFindings,
    ResolveReviewFinding,
    SubmitReviewJob,
)
from .review_tiers import (
    DEFAULT_TIERS,
    TIER_NOT_CONFIGURED,
    TIER_START_REASONS,
    approval_record,
    create_review_tier_grants_schema,
    tier_grant_check,
    tier_start_refusal,
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
# The runner's launch-time policy re-check may also fail without a policy reason code; a code
# review also does not start without the pinned prompt, model egress and a supported destination.
NOT_START_REASONS = (
    *REFUSAL_REASONS,
    "policy_check_failed",
    "prompt_not_pinned",
    "egress_not_permitted",
    "destination_not_supported",
    # A tier approval's grant is re-checked when the job starts (review_tiers).
    *TIER_START_REASONS,
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


class ReviewForbidden(Forbidden):
    """A separation-of-duties refusal (403) with a stable reason code."""

    def __init__(self, reason: str):
        super().__init__(
            f"The submitter or reviewer of a job, or the same person, cannot act on it ({reason})."
        )
        self.reason = reason


class ReviewRefused(Conflict):
    """A policy (409) or output-validation (422) refusal carrying a stable reason code."""

    def __init__(self, reason: str, status: int = 409, detail: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        # Internal sub-cause for the runner (e.g. ``budget_exceeded``); never a response field.
        self.detail = detail


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


_LINEAGE_WALK_LIMIT = 10000


def _sealed_inputs(connection, project_id, digests):
    """The subset of ``digests`` that is, or is recorded in lineage as derived from, a holdout
    partition the project declares or claims (any campaign status).

    Only rows that can concern these digests are read: claims through their unique index, specs
    only when their text contains a candidate digest, and lineage upward from the inputs. A walk
    that exceeds its bound fails closed (every input counts as sealed)."""
    digests = sorted(set(digests))
    if not digests:
        return set()
    ancestors = {}  # candidate digest -> the input digests it is an ancestor-or-self of
    rows = list(
        connection.execute(
            """WITH RECURSIVE up(id, root) AS (
                SELECT n.id, n.sha256 FROM artifact_lineage_nodes n
                WHERE n.project_id=? AND n.sha256 IN (SELECT value FROM json_each(?))
                UNION
                SELECT e.parent_node_id, u.root FROM artifact_lineage_edges e
                JOIN up u ON e.child_node_id=u.id WHERE e.project_id=?
                LIMIT ?)
            SELECT n.sha256, u.root FROM up u JOIN artifact_lineage_nodes n ON n.id=u.id""",
            (project_id, json.dumps(digests), project_id, _LINEAGE_WALK_LIMIT + 1),
        )
    )
    if len(rows) > _LINEAGE_WALK_LIMIT:
        return set(digests)
    for digest in digests:
        ancestors.setdefault(digest, set()).add(digest)
    for digest, root in rows:
        ancestors.setdefault(digest, set()).add(root)
    candidates = json.dumps(sorted(ancestors))
    sealed = {
        r[0]
        for r in connection.execute(
            "SELECT DISTINCT partition_digest FROM campaign_holdout_claims "
            "WHERE project_id=? AND partition_digest IN (SELECT value FROM json_each(?))",
            (project_id, candidates),
        )
    }
    declared = set()
    for row in connection.execute(
        "SELECT spec_json FROM campaigns WHERE project_id=? AND EXISTS "
        "(SELECT 1 FROM json_each(?) j WHERE instr(spec_json, j.value) > 0)",
        (project_id, candidates),
    ):
        try:
            _declared_partitions(json.loads(row[0]), declared)
        except (TypeError, ValueError):
            continue
    sealed |= declared & set(ancestors)
    return set().union(*(ancestors[d] for d in sealed)) if sealed else set()


_CHAIN_LIMIT = 64
DISTINCT_PERSON = "distinct_person"
SINGLE_OWNER = "single_owner_opt_in"


def _enrolment_roots(connection, principal_id):
    """The root principals (no creating principal) reached by walking the credential chain
    (``created_by_principal_id`` and the parent credential's principal) upward. Returns
    ``(roots, exceeded)``; ``exceeded`` means the bounded walk did not finish."""
    seen, todo, roots = set(), [principal_id], set()
    while todo:
        current = todo.pop()
        if current is None or current in seen:
            continue
        if len(seen) >= _CHAIN_LIMIT:
            return roots, True
        seen.add(current)
        parents = set()
        for row in connection.execute(
            "SELECT c.created_by_principal_id, p.principal_id FROM credentials c "
            "LEFT JOIN credentials p ON p.id=c.parent_id WHERE c.principal_id=?",
            (current,),
        ):
            parents.update(x for x in row if x is not None and x != current)
        if parents:
            todo.extend(parents)
        else:
            roots.add(current)
    return roots, False


def _same_person(connection, left, right):
    """Two principals are one person when their enrolment-root sets intersect. A walk that
    exceeds its bound fails closed (same person)."""
    if left == right:
        return True
    left_roots, left_over = _enrolment_roots(connection, left)
    right_roots, right_over = _enrolment_roots(connection, right)
    return left_over or right_over or bool(left_roots & right_roots)


def _separation(connection, caller_id, others, single_owner):
    """The recorded basis for a separation-of-duties check, or Forbidden.

    By default the caller must be a different person from every principal in ``others``.
    With the deployment's single-owner opt-in only the identical principal is refused and the
    same person through another credential is allowed, recorded as ``single_owner_opt_in``."""
    basis = DISTINCT_PERSON
    for other in others:
        if other is None:
            continue
        if other == caller_id:
            raise ReviewForbidden("same_principal")
        if _same_person(connection, caller_id, other):
            if not single_owner:
                raise ReviewForbidden("same_person")
            basis = SINGLE_OWNER
    return basis


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
            ('plan_review','dataset_scout','technique_scout','pre_gate_review','code_review')),
        title TEXT NOT NULL, inputs_json TEXT NOT NULL,
        prompt_digest TEXT NOT NULL CHECK(length(prompt_digest)=64),
        destination_id TEXT NOT NULL,
        tier TEXT NOT NULL CHECK(length(tier) BETWEEN 1 AND 32 AND tier GLOB '[a-z]*'
            AND tier NOT GLOB '*[^a-z0-9-]*'),
        budget_cents INTEGER NOT NULL CHECK(budget_cents BETWEEN 0 AND 10000),
        egress TEXT NOT NULL CHECK(egress IN ('none','model_runtime')),
        state TEXT NOT NULL CHECK(state IN ('awaiting_approval','queued','running','completed',
            'failed','not_started','cancelled')),
        reason TEXT, revision INTEGER NOT NULL CHECK(revision>=1),
        digest TEXT NOT NULL CHECK(length(digest)=64),
        receipt_digest TEXT, review_md_digest TEXT, findings_actor_id TEXT,
        findings_separation_basis TEXT,
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
        job_digest TEXT NOT NULL, subject_digests_json TEXT NOT NULL, created_at TEXT NOT NULL,
        separation_basis TEXT NOT NULL CHECK(separation_basis IN
            ('distinct_person','single_owner_opt_in','legacy_unrecorded')))""")
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
    create_code_review_schema(connection)
    create_review_tier_grants_schema(connection)


class ReviewJobStore(CodeReviewRunStore, AgentResearchStore):
    #: Deployment opt-in (``tessera serve --review-single-owner``); set by ``create_app``.
    single_owner = False
    #: Configured review tiers (``tessera serve --review-tiers``); set by ``serve``.
    tiers = DEFAULT_TIERS

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
            "findings_separation_basis": row["findings_separation_basis"],
            "code_review": (
                code_review_record(connection, row["id"]) if row["kind"] == "code_review" else None
            ),
            "submitter_credential_id": row["credential_id"],
            "submitter_principal_id": row["actor_id"],
            "approval": (approval_record(connection, row["id"], approval) if approval else None),
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
                    "separation_basis": decision["separation_basis"],
                    "created_at": decision["created_at"],
                }
                if decision
                else None
            ),
            "separation_basis": decision["separation_basis"] if decision else None,
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

    def _submit_guard(self, connection, actor, project_id):
        if actor["role"] == "agent":
            self._scope(connection, actor, "review:submit")
        elif actor["role"] != "human":
            raise Forbidden("Only a human or an enrolled agent may submit a review job.")
        self._project(connection, actor, project_id)

    def submit(self, principal, project_id, key, payload, policy):
        body = self._body(SubmitReviewJob, payload)
        subject = body.pop("code_review", None)
        if subject is not None:
            # Other kinds keep their ADR 0084 digests (no code_review key bound).
            body["code_review"] = subject

        def command(connection, actor):
            self._submit_guard(connection, actor, project_id)
            return 201, self._submit_job(connection, actor, project_id, body, policy)

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/review-jobs",
            canonical_digest(body),
            command,
            replay_guard=lambda c, a, r: self._submit_guard(c, a, project_id),
        )

    def submit_code_review(self, principal, project_id, key, payload, policy, git):
        """Submit a code review of a platform git branch (ADR 0108); see ``code_review_submit``."""
        from .code_review_submit import submit_code_review

        return submit_code_review(self, principal, project_id, key, payload, policy, git)

    def _submit_job(self, connection, actor, project_id, body, policy, source=None):
        """Insert a validated job (and a code review's subject and optional git ``source``) with
        its event, inside the caller's transaction; returns the job record. The caller has
        already authorized ``actor`` for ``project_id``."""
        body = dict(body)
        subject = body.pop("code_review", None)
        if subject is not None:
            # Other kinds keep their ADR 0084 job digest; a code review binds its subject.
            body["code_review"] = subject
        if source is not None and subject is None:
            raise ValueError("a git source belongs to a code review job")
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
        sealed = _sealed_inputs(connection, project_id, [i["digest"] for i in inputs])
        if any(item["digest"] in sealed for item in inputs):
            raise ReviewRefused("input_sealed")
        classes = sorted({item["data_class"] for item in inputs})
        if not self.tiers.known(body["tier"]):
            raise ReviewRefused(TIER_NOT_CONFIGURED, status=422)
        reason = policy.check(body["destination_id"], body["tier"], classes, body["budget_cents"])
        if reason:
            raise ReviewRefused(reason if reason in REFUSAL_REASONS else "destination_not_enrolled")
        # ADR 0168: the inputs' licence labels (ADR 0040) must be admitted for the destination;
        # a policy that does not carry labels (the default refuses every submit) is not asked.
        labels = sorted(
            set().union(*(snapshot_labels(connection, item["snapshot_id"]) for item in inputs))
        )
        label_check = getattr(policy, "license_labels_refusal", None)
        if label_check is not None:
            reason = label_check(body["destination_id"], labels)
            if reason:
                raise ReviewRefused(
                    reason if reason in REFUSAL_REASONS else "destination_not_enrolled"
                )
        admitted = {
            "destination_id": body["destination_id"],
            "data_classes": classes,
            "license_labels": labels,
            "policy_digest": getattr(policy, "digest", None),
        }
        ident = "review_job_" + uuid.uuid4().hex
        state = "awaiting_approval" if self.tiers.requires_approval(body["tier"]) else "queued"
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
                # A job from a git source also binds that source (ADR 0108).
                canonical_digest(
                    {**body, "inputs": inputs}
                    if source is None
                    else {**body, "inputs": inputs, "source": source}
                ),
                now,
                now,
            ),
        )
        if subject is not None:
            connection.execute(
                "INSERT INTO code_review_jobs (job_id,org_id,mode,base_digest,head_digest,"
                "rubric_digest) VALUES (?,?,?,?,?,?)",
                (
                    ident,
                    actor["org_id"],
                    subject["mode"],
                    subject["base_digest"],
                    subject["head_digest"],
                    subject["rubric_digest"],
                ),
            )
        event = {
            "job_id": ident,
            "kind": body["kind"],
            "tier": body["tier"],
            "state": state,
            **admitted,
        }
        if source is not None:
            connection.execute(
                "INSERT INTO code_review_sources (job_id,org_id,project_id,repo,branch,"
                "head_sha,base_branch,base_sha,focused_tests_json,round,previous_job_id,"
                "recheck_findings_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    actor["org_id"],
                    project_id,
                    source["repo"],
                    source["branch"],
                    source["head_sha"],
                    source["base_branch"],
                    source["base_sha"],
                    json.dumps(source["focused_tests"]),
                    source["round"],
                    source["previous_job_id"],
                    json.dumps(source["recheck_findings"]),
                    now,
                ),
            )
            event["source"] = source
        self._event(
            connection,
            actor,
            project_id,
            None,
            "review_job.submitted",
            1,
            event,
        )
        return self._job_record(
            connection,
            connection.execute("SELECT * FROM review_jobs WHERE id=?", (ident,)).fetchone(),
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
            # The approval names the grant that pays for this tier: a reasoning grant of the
            # job's project for its destination and a model bound to its tier (review_tiers).
            refusal, binding = tier_grant_check(connection, row, body["grant_id"], self.tiers)
            if refusal:
                raise ReviewRefused(refusal)
            now = utc_now()
            connection.execute(
                "INSERT INTO review_job_approvals (job_id,org_id,actor_id,rationale,job_digest,"
                "created_at) VALUES (?,?,?,?,?,?)",
                (job_id, actor["org_id"], actor["id"], body["rationale"], row["digest"], now),
            )
            connection.execute(
                "INSERT INTO review_job_tier_grants (job_id,org_id,grant_id,tier,provider_id,"
                "model,subject_credential_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    actor["org_id"],
                    binding["grant_id"],
                    binding["tier"],
                    binding["provider_id"],
                    binding["model"],
                    binding["subject_credential_id"],
                    now,
                ),
            )
            self._bump(connection, job_id, "queued", row["revision"])
            self._event(
                connection,
                actor,
                row["project_id"],
                None,
                "review_job.tier_approved",
                row["revision"] + 1,
                {
                    "job_id": job_id,
                    "tier": row["tier"],
                    "job_digest": row["digest"],
                    "grant_id": binding["grant_id"],
                    "provider_id": binding["provider_id"],
                    "model": binding["model"],
                },
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
        if actor["kind"] == "worker":
            return
        if actor["credential_id"] == row["credential_id"]:
            raise ReviewForbidden("same_principal")
        _separation(connection, actor["id"], [row["actor_id"]], self.single_owner)
        if actor["role"] == "human":
            return
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
            if state == "running" and row["kind"] == "code_review":
                # Only the leased runner claim may start a code review: a lease-less running job
                # would hold a concurrency slot that is never reaped (ADR 0092).
                raise Conflict("A code review job is started only by the runner's claim.")
            if state == "running":
                refusal = tier_start_refusal(connection, row)
                if refusal:
                    raise ReviewRefused(refusal)
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
            if row["credential_id"] == actor["credential_id"]:
                raise ReviewForbidden("same_principal")
            basis = _separation(connection, actor["id"], [row["actor_id"]], self.single_owner)
            return row, basis

        def command(connection, actor):
            row, basis = guard(connection, actor)
            if row["state"] not in ("queued", "running"):
                raise Conflict("Review job is not accepting findings.")
            body = self._validated_output(row, payload, connection)
            if row["kind"] == "code_review":
                connection.execute(
                    "UPDATE code_review_jobs SET lease_owner=NULL,lease_expires_at=NULL "
                    "WHERE job_id=?",
                    (job_id,),
                )
            return self._store_output(
                connection, actor, row, body, findings_actor_id=actor["id"], basis=basis
            )

        return self._mutate(
            principal,
            key,
            "POST",
            f"/review-jobs/{job_id}/findings",
            digest,
            command,
            replay_guard=lambda c, a, r: guard(c, a),
        )

    @staticmethod
    def _validated_output(row, payload, connection):
        """Validate reviewer output for ``row``; the one rule set for HTTP ingest and the runner.

        Every violation is ``ReviewRefused("failed_output_invalid", 422)`` before any write.
        ``connection`` is the caller's transaction (it reads a code review's git source for the
        ADR 0108 re-review rule).
        """

        def refuse(detail=None):
            return ReviewRefused("failed_output_invalid", 422, detail)

        try:
            body = IngestFindings.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise refuse() from error
        if row["kind"] != "code_review":
            if body.pop("verdict") is not None or body.pop("receipt") is not None:
                raise refuse()
            return body
        findings, verdict, receipt = body["findings"], body["verdict"], body["receipt"]
        if verdict is None or receipt is None:
            raise refuse()
        if [item["id"] for item in findings] != [str(n) for n in range(1, len(findings) + 1)]:
            raise refuse()
        severities = {item["severity"] for item in findings}
        if "blocking" in severities and verdict != "CHANGES-REQUIRED":
            raise refuse()
        if verdict == "MERGE" and severities & {"major", "blocking"}:
            raise refuse()
        # A re-review (round 2-3) checks only the listed previous findings (ADR 0108).
        if not recheck_output_valid(source_record(connection, row["id"]), findings):
            raise refuse()
        if receipt["tier"] != row["tier"]:
            raise refuse()
        # The receipt must name this job's inputs, its most private class and its destination
        # (and, for anthropic, pinned prices and the provider-reported model).
        if not receipt_bound(row, receipt):
            raise refuse()
        basis, cents = receipt["cost_basis"], receipt["cost_cents"]
        if basis in BILLED_COST_BASES and (cents is None or cents > row["budget_cents"]):
            raise refuse("budget_exceeded")
        # ``cost`` is cents; a recorded integer may not understate the computed price.
        if basis == "computed_from_pinned_prices" and cents < math.ceil(receipt["cost"]):
            raise refuse()
        # ADR 0083: included usage is the OpenAI included-OAuth mode reporting zero cost.
        if basis == "included_subscription_usage" and receipt["cost"] != 0:
            raise refuse()
        digest = canonical_digest(receipt)
        if body["receipt_digest"] is not None and body["receipt_digest"] != digest:
            raise refuse()
        body["receipt_digest"] = digest
        return body

    def _store_output(
        self, connection, actor, row, body, system_actor=None, findings_actor_id=None, basis=None
    ):
        """Store validated findings (and a code review's verdict and receipts), complete the job
        and write its event, inside the caller's transaction."""
        job_id = row["id"]
        now = utc_now()
        for item in body["findings"]:
            connection.execute(
                "INSERT INTO review_findings (id,job_id,org_id,project_id,finding_key,"
                "finding_json,digest,state,revision,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,'open',1,?,?)",
                (
                    "review_finding_" + uuid.uuid4().hex,
                    job_id,
                    row["org_id"],
                    row["project_id"],
                    item["id"],
                    json.dumps(item),
                    canonical_digest({"job_digest": row["digest"], "finding": item}),
                    now,
                    now,
                ),
            )
        event = {"job_id": job_id, "findings": len(body["findings"])}
        if system_actor is not None:
            event["actor"] = system_actor
        if basis is not None:
            event["separation_basis"] = basis
        if row["kind"] == "code_review":
            side = connection.execute(
                "SELECT * FROM code_review_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            review_receipt = {
                "job_digest": row["digest"],
                "mode": side["mode"],
                "base_digest": side["base_digest"],
                "head_digest": side["head_digest"],
                "rubric_digest": side["rubric_digest"],
                "verdict": body["verdict"],
                "findings_digest": canonical_digest({"findings": body["findings"]}),
                # The verdict and the findings themselves are on the receipt, with the git
                # source when the job was submitted from one (ADR 0108).
                "findings": body["findings"],
                "source": source_record(connection, job_id),
                "model_call": body["receipt"],
            }
            review_digest = canonical_digest(review_receipt)
            connection.execute(
                "UPDATE code_review_jobs SET verdict=?,receipt_json=?,review_receipt_json=?,"
                "review_receipt_digest=? WHERE job_id=?",
                (
                    body["verdict"],
                    json.dumps(body["receipt"], sort_keys=True),
                    json.dumps(review_receipt, sort_keys=True),
                    review_digest,
                    job_id,
                ),
            )
            append_attempt(
                connection,
                job_id,
                attempt_entry(row, max(side["attempts"], 1), "completed", None, body["receipt"]),
            )
            event.update(verdict=body["verdict"], review_receipt_digest=review_digest)
        self._bump(
            connection,
            job_id,
            "completed",
            row["revision"],
            None,
            receipt_digest=body["receipt_digest"],
            review_md_digest=body["review_md_digest"],
            findings_actor_id=findings_actor_id,
            findings_separation_basis=basis,
        )
        self._event(
            connection,
            actor,
            row["project_id"],
            None,
            "review_job.completed",
            row["revision"] + 1,
            event,
        )
        return 201, {
            "job": self._job_record(
                connection,
                connection.execute("SELECT * FROM review_jobs WHERE id=?", (job_id,)).fetchone(),
            ),
            "findings": [
                self._finding_record(connection, f)
                for f in connection.execute(
                    "SELECT * FROM review_findings WHERE job_id=? ORDER BY sequence", (job_id,)
                )
            ],
        }

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
            basis = _separation(
                connection,
                actor["id"],
                [job["actor_id"], job["findings_actor_id"]],
                self.single_owner,
            )
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
                "INSERT INTO review_finding_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                    basis,
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
                    "separation_basis": basis,
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

    def list_jobs(self, principal, project_id, offset=0, limit=100, kind=None):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._read_actor(connection, principal)
            self._project(connection, actor, project_id)
            return [
                self._job_record(connection, row)
                for row in connection.execute(
                    "SELECT * FROM review_jobs WHERE org_id=? AND project_id=? "
                    "AND (? IS NULL OR kind=?) ORDER BY sequence LIMIT ? OFFSET ?",
                    (actor["org_id"], project_id, kind, kind, limit, offset),
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
