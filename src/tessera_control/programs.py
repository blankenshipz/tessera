"""Research-program review over existing studies and immutable decision scopes.

All checks run inside the caller's storage transaction. No approval here releases
bytes, grants a lease, dispatches a job, or certifies scientific validity.
"""

import json

from .evaluation_models import evaluate_record
from .program_models import OUTCOME_CONDITIONS, ProgramReview
from .research import ResearchStore
from .store import Conflict, NotFound, canonical_digest

REVIEW_LIMIT = 1000


def _rows(conn, sql, parameters):
    rows, size = [], 0
    for row in conn.execute(sql + " LIMIT ?", (*parameters, REVIEW_LIMIT + 1)):
        value = dict(row)
        size += len(json.dumps(value).encode())
        if len(rows) >= REVIEW_LIMIT or size > 1_000_000:
            raise Conflict("Program context exceeds its limit; no partial review is allowed.")
        rows.append(value)
    return rows


def program_basis(conn, actor, study_id):
    study = ResearchStore._study(conn, actor, study_id)
    basis = {"study": study}
    for section in ("variants", "sources", "findings", "corrections"):
        basis[section] = _rows(
            conn, f"SELECT * FROM research_{section} WHERE study_id=? ORDER BY id", (study_id,)
        )
    for section in ("sources", "variants"):
        basis[f"finding_{section}"] = _rows(
            conn,
            f"SELECT r.* FROM research_finding_{section} r JOIN research_findings f "
            f"ON f.id=r.finding_id WHERE f.study_id=? ORDER BY r.finding_id,r.{section[:-1]}_id",
            (study_id,),
        )
    # Optional links are established in immutable study-variant metadata. Never
    # discover unrelated project operations or interpret manual outcomes as runs.
    experiment_ids = set()
    for variant in basis["variants"]:
        experiment_id = json.loads(variant["metadata_json"]).get("experiment_id")
        if experiment_id is not None:
            if not isinstance(experiment_id, str):
                raise Conflict("Variant experiment_id must identify an existing experiment.")
            experiment_ids.add(experiment_id)
    basis["executions"] = []
    for experiment_id in sorted(experiment_ids):
        experiment = conn.execute(
            "SELECT * FROM research_experiments WHERE id=? AND project_id=?",
            (experiment_id, study["project_id"]),
        ).fetchone()
        if experiment is None:
            raise NotFound("Linked experiment was not found in this project.")
        item = {"experiment": dict(experiment)}
        for section, table in (
            ("designs", "research_experiment_designs"),
            ("variants", "research_experiment_variants"),
            ("admissions", "research_experiment_admitted_attempts"),
            ("conclusions", "research_conclusions"),
            ("amendments", "research_conclusion_amendments"),
        ):
            item[section] = _rows(
                conn,
                f"SELECT * FROM {table} WHERE experiment_id=? ORDER BY rowid",
                (experiment_id,),
            )
        basis["executions"].append(item)
        if len(json.dumps(basis).encode()) > 1_000_000:
            raise Conflict("Program context exceeds its byte limit; no partial review is allowed.")
    if len(json.dumps(basis).encode()) > 1_000_000:
        raise Conflict("Program context exceeds its byte limit; no partial review is allowed.")
    return basis


def program_history(conn, project_id, study_id):
    # Filter in SQLite so unrelated generic inbox history cannot hide a review.
    rows = _rows(
        conn,
        "SELECT * FROM decision_requests WHERE project_id=? "
        "AND json_extract(scope_json,'$.type')='research_program' "
        "AND json_extract(scope_json,'$.study_id')=? ORDER BY rowid",
        (project_id, study_id),
    )
    size = 0
    for row in rows:
        row["scope"] = json.loads(row.pop("scope_json"))
        event = conn.execute(
            "SELECT payload_json FROM events WHERE project_id=? "
            "AND event_type='decision_requested' "
            "AND json_extract(payload_json,'$.decision_id')=? ORDER BY cursor LIMIT 1",
            (project_id, row["id"]),
        ).fetchone()
        row["basis_snapshot"] = (
            json.loads(event["payload_json"]).get("program_basis") if event else None
        )
        if row["scope"]["action"] == "evaluation":
            row["evaluation_report"] = (
                json.loads(event["payload_json"]).get("evaluation_report") if event else None
            )
        size += len(json.dumps(row).encode())
        if size > 1_000_000:
            raise Conflict("Program review history exceeds its response limit.")
    return rows


def _references(ids, rows, label):
    lookup = {row["id"]: row for row in rows}
    if any(ident not in lookup for ident in ids):
        raise NotFound(f"{label} was not found in this study.")
    return [lookup[ident] for ident in ids]


def _supported(sources):
    if not sources or any(
        s["verdict"] != "supported" or s["superseded_at"] is not None for s in sources
    ):
        raise Conflict("Review requires currently supported, unsuperseded sources.")


def _evaluation_report(review, history, *, current=True):
    record = review.evaluation
    plans = [r for r in history if r["scope"]["action"] == "plan"]
    plan_row = next((r for r in plans if r["id"] == record.plan_decision_id), None)
    if plan_row is None or plan_row["status"] != "approved":
        raise Conflict("Evaluation requires a human-approved plan in this study/outcome.")
    if current and plans[-1]["id"] != plan_row["id"]:
        raise Conflict("Evaluation must reference the current plan proposal head.")
    if plan_row["scope_digest"] != record.plan_scope_digest:
        raise Conflict("Evaluation plan scope digest does not match its immutable plan.")
    protocol = ProgramReview.model_validate(plan_row["scope"]).evaluation_protocol
    if protocol is None:
        raise Conflict("Evaluation requires a declared plan protocol.")
    try:
        return evaluate_record(protocol, record)
    except ValueError as error:
        raise Conflict(str(error)) from error


def _gate_direction(review, plan, history):
    if plan.evaluation_protocol is None and review.evaluation_id is None:
        return  # Legacy prose-only plans remain explicitly unevaluated.
    evaluations = [r for r in history if r["scope"]["action"] == "evaluation"]
    row = next((r for r in evaluations if r["id"] == review.evaluation_id), None)
    if row is None or row["status"] != "approved" or row["id"] != evaluations[-1]["id"]:
        raise Conflict("Go requires the current human-reviewed evaluation record.")
    if row["scope"]["basis_digest"] != review.basis_digest:
        raise Conflict("Go requires an evaluation of the current evidence basis.")
    report = _evaluation_report(ProgramReview.model_validate(row["scope"]), history)
    if report["status"] != "pass":
        raise Conflict(
            "Go requires passing declared gates; failed or inconclusive results block go."
        )


def validate_program_review(
    conn, actor, project_id, kind, scope, *, resolving_id=None, approve=False
):
    if scope.get("type") != "research_program":
        return
    review = ProgramReview.model_validate(scope)
    if kind != ("access" if review.action == "source_use" else "direction"):
        raise Conflict("Program review kind does not match its action.")
    basis = program_basis(conn, actor, review.study_id)
    if basis["study"]["project_id"] != project_id:
        raise NotFound("Study was not found in this project.")
    if canonical_digest(basis) != review.basis_digest:
        raise Conflict("Program evidence basis is stale; inspect and propose a new review.")
    outcome_history = [
        row
        for row in program_history(conn, project_id, review.study_id)
        if row["scope"]["outcome"] == review.outcome
    ]
    history = [row for row in outcome_history if row["scope"]["action"] == review.action]
    head = history[-1]["id"] if history else None
    if (resolving_id is not None and head != resolving_id) or (
        resolving_id is None and head != review.supersedes_id
    ):
        raise Conflict("Program proposal lineage is stale; reference the current head.")
    if review.brief:
        _references(review.brief.data_source_ids, basis["sources"], "Data source")
        selected = {v for condition in review.conditions.values() for v in condition.variant_ids}
        _references(selected, basis["variants"], "Variant")
        if selected != {v["id"] for v in basis["variants"]}:
            raise Conflict("Plan must account for every registered study variant.")
        if review.evaluation_protocol:
            _references(
                [g.variant_id for g in review.evaluation_protocol.gates],
                basis["variants"],
                "Evaluation variant",
            )
    report = _evaluation_report(review, outcome_history) if review.evaluation else None
    if review.evaluation_id:
        if not any(
            r["id"] == review.evaluation_id and r["scope"]["action"] == "evaluation"
            for r in outcome_history
        ):
            raise NotFound("Evaluation was not found in this study/outcome.")
    sources = _references(review.source_ids, basis["sources"], "Source")
    findings = _references(review.finding_ids, basis["findings"], "Finding")
    if not approve:
        # Preserve the exact review-time context in the same transaction's event,
        # not merely a hash of mutable projections that cannot later be recovered.
        if len(json.dumps({"basis": basis, "evaluation_report": report}).encode()) > 32_000:
            raise Conflict("Program review snapshot exceeds 32KB; use a smaller outcome study.")
        # Persist the computed report with the immutable request event, in the
        # same transaction as attribution, scope, basis and idempotency receipt.
        return {"basis": basis, "evaluation_report": report}
    if review.action == "source_use":
        _supported(sources)
    if review.action == "go_no_go" and review.recommendation == "go":
        approvals = [
            row
            for row in program_history(conn, project_id, review.study_id)
            if row["status"] == "approved" and row["scope"]["outcome"] == review.outcome
        ]
        plans = [row for row in approvals if row["scope"]["action"] == "plan"]
        if not plans:
            raise Conflict("Go requires a human-reviewed program plan.")
        plan = ProgramReview.model_validate(plans[-1]["scope"])
        _gate_direction(review, plan, outcome_history)
        if plan.brief.missing_prerequisites:
            raise Conflict("Go requires resolving the reviewed plan's missing prerequisites.")
        accounted = {v for c in plan.conditions.values() for v in c.variant_ids}
        if accounted != {v["id"] for v in basis["variants"]}:
            raise Conflict("Go requires updated plan variant accounting.")
        uses = [row for row in approvals if row["scope"]["action"] == "source_use"]
        if (
            not uses
            or uses[-1]["scope"]["basis_digest"] != review.basis_digest
            or not set(plan.brief.data_source_ids).issubset(uses[-1]["scope"]["source_ids"])
        ):
            raise Conflict("Go requires current human source-use review for the plan's data.")
        if not basis["variants"] or any(v["status"] == "registered" for v in basis["variants"]):
            raise Conflict("Go requires complete registered variant accounting.")
        if any(f["status"] != "accepted" for f in findings):
            raise Conflict(
                "Go requires accepted findings without outstanding review or retraction."
            )
        cited_sources = {
            r["source_id"]
            for r in basis["finding_sources"]
            if r["finding_id"] in review.finding_ids
        }
        _supported(_references(cited_sources, basis["sources"], "Finding source"))
        for execution in basis["executions"]:
            current_variants = {
                v["id"]
                for v in execution["variants"]
                if v["design_revision"] == execution["experiment"]["current_design_revision"]
            }
            if not current_variants.issubset({a["variant_id"] for a in execution["admissions"]}):
                raise Conflict(
                    "Go requires accounting for every current linked experiment variant."
                )
            if not execution["admissions"] or any(
                a["state"] not in {"succeeded", "failed", "cancelled"}
                or a["outcome"] not in {"succeeded", "failed", "cancelled"}
                or a["operational_observed_state"] != "exited"
                or a["operational_termination_confirmed"] != 1
                for a in execution["admissions"]
            ):
                raise Conflict("Go cannot hide incomplete or unknown linked execution.")
        # An approved go remains a human direction, including judgments based on
        # failed/null outcomes. It is not automatic scientific acceptance.


def program_context(conn, actor, study_id):
    basis = program_basis(conn, actor, study_id)
    digest = canonical_digest(basis)
    history = program_history(conn, basis["study"]["project_id"], study_id)
    if len(json.dumps(history).encode()) + len(json.dumps(basis).encode()) > 2_000_000:
        raise Conflict("Program context exceeds its response limit.")
    heads, effective = {}, {}
    for row in history:
        key = f"{row['scope']['outcome']}:{row['scope']['action']}"
        heads[key] = row["id"]
        if row["status"] == "approved":
            effective[key] = row["id"]
        row["basis_current"] = row["scope"]["basis_digest"] == digest
    for row in history:
        key = f"{row['scope']['outcome']}:{row['scope']['action']}"
        row["is_proposal_head"] = heads[key] == row["id"]
        row["is_effective_review"] = effective.get(key) == row["id"] and row["basis_current"]
        review = ProgramReview.model_validate(row["scope"])
        outcome_history = [r for r in history if r["scope"]["outcome"] == review.outcome]
        if review.evaluation:
            plans = [r for r in outcome_history if r["scope"]["action"] == "plan"]
            row["is_effective_review"] &= plans[-1]["id"] == review.evaluation.plan_decision_id
        if review.action == "go_no_go" and review.recommendation == "go":
            plans = [
                r
                for r in outcome_history
                if r["scope"]["action"] == "plan" and r["status"] == "approved"
            ]
            if plans:
                try:
                    _gate_direction(
                        review, ProgramReview.model_validate(plans[-1]["scope"]), outcome_history
                    )
                except Conflict:
                    row["is_effective_review"] = False
    return {
        "study_id": study_id,
        "project_id": basis["study"]["project_id"],
        "basis_digest": digest,
        "basis": basis,
        "reviews": history,
        "condition_catalog": OUTCOME_CONDITIONS,
        "execution_granted": False,
        "scientific_validity": "not_assessed",
        "evidence_boundary": "registered_study_records_and_explicit_experiment_links_only",
        "salad_adapter": {
            "supported": False,
            "config_target": "salad_gpu_future",
            "config_contract_version": 1,
            "health": "not_queried",
            "live_execution": False,
        },
    }
