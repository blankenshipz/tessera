"""Read-only experiment and program result summaries for enrolled external agents.

Closes the supervised loop (propose -> human approve -> run -> observe -> critique)
without a human copying results in.  Everything here is a bounded READ of records
Tessera already holds: it grants no approval, spend, execution, release or write
authority, and the summary carries no scientific-validity judgement.

Access: humans (unscoped, tenant-checked) see every section.  An enrolled agent needs
the ``results:read`` action for any access and ``budget:read`` for budget sections;
sections it may not see are listed under ``withheld``.  Column lists are explicit
allowlists; principal ids, credential ids, source references and raw content are never
selected.  Artifact bytes stay behind the human-gated release path.

Extending: another track that adds typed metrics, gate evidence or artifact lineage
registers a section without touching the summary code::

    register_result_section(ResultSection(
        name="typed_metrics", subject="experiment", scope="results:read",
        load=lambda conn, actor, experiment: [...json-serializable rows...]))

``load`` receives an open read transaction, the resolved actor and the subject row
(a dict).  It must use explicit column lists and return JSON-serializable data.
Names are unique per subject kind; duplicates are refused.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .experiment_budgets import budget_status
from .experiment_comparison import comparison_section
from .external_agents import ExternalAgentStore
from .programs import program_history
from .store import Conflict, NotFound, _connect

ROW_CAP = 1000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RESULTS_SCOPE = "results:read"
BUDGET_SCOPE = "budget:read"

EVIDENCE_BOUNDARY = (
    "Recorded Tessera records only. Absent records are not evidence of absence, "
    "measurements are not verified by Tessera, and nothing here is approval or "
    "evidence of scientific validity."
)
MEASUREMENT_LABEL = (
    "Values are human-entered or derived per the existing ledger and are not verified by Tessera."
)


@dataclass(frozen=True)
class ResultSection:
    name: str
    subject: Literal["experiment", "program"]
    scope: str
    load: Callable[[Any, dict, dict], Any]


_REGISTRY: list[ResultSection] = []


def register_result_section(section: ResultSection) -> None:
    if any(s.name == section.name and s.subject == section.subject for s in _REGISTRY):
        raise ValueError(f"Result section {section.name!r} is already registered.")
    _REGISTRY.append(section)


def _rows(connection, sql, params=()):
    rows = connection.execute(sql + f" LIMIT {ROW_CAP + 1}", params).fetchall()
    if len(rows) > ROW_CAP:
        raise Conflict("A result section exceeds its row limit; no partial summary is returned.")
    return [dict(row) for row in rows]


def _attempts(connection, actor, experiment):
    return {
        "admitted": _rows(
            connection,
            "SELECT a.id,a.variant_id,a.design_revision,a.state,a.outcome,a.reason,"
            "a.created_at,a.updated_at,"
            # The approved plan is the frozen source of the run's network profile.
            "COALESCE((SELECT json_extract(r.execution_spec_json,'$.egress.profile') "
            "FROM task_revisions r WHERE r.task_id=a.task_id AND r.plan_digest=a.plan_digest "
            "LIMIT 1),'none') AS network_profile,"
            "(SELECT COALESCE(json_extract(e.payload_json,'$.journal_sha256'),"
            "json_extract(e.payload_json,'$.journal_digest')) FROM events e "
            "WHERE e.event_type='execution.egress_journal_recorded' "
            "AND json_extract(e.payload_json,'$.operation_id')=a.operation_id "
            "ORDER BY e.cursor DESC LIMIT 1) AS egress_journal_sha256 "
            "FROM research_experiment_admitted_attempts a WHERE a.experiment_id=? "
            "ORDER BY a.created_at,a.id",
            (experiment["id"],),
        ),
        "recorded": _rows(
            connection,
            "SELECT a.id,a.variant_id,a.design_revision,a.provenance_kind,a.outcome,a.reason,"
            "a.termination_status,a.created_at FROM research_experiment_attempts a "
            "JOIN research_experiment_variants v ON v.id=a.variant_id "
            "WHERE v.experiment_id=? ORDER BY a.created_at,a.id",
            (experiment["id"],),
        ),
    }


def _inputs(connection, actor, experiment):
    """Per admitted attempt: the granted input snapshot and, if staged, its source outputs."""
    from .dataset_staging import staging_for_snapshot

    rows = _rows(
        connection,
        "SELECT a.id AS attempt_id,g.snapshot_id,g.snapshot_digest "
        "FROM research_experiment_admitted_attempts a "
        "JOIN execution_grants g ON g.id=a.grant_id WHERE a.experiment_id=? "
        "ORDER BY a.created_at,a.id",
        (experiment["id"],),
    )
    for row in rows:
        staged = staging_for_snapshot(connection, row["snapshot_id"])
        row["staged_from_artifacts"] = (
            None
            if staged is None
            else {
                "manifest_id": staged["manifest_id"],
                "manifest_digest": staged["manifest_digest"],
                "operation_id": staged["operation_id"],
                "files_count": len(staged["files"]),
            }
        )
    return rows


def _measurements(connection, actor, experiment):
    rows = _rows(
        connection,
        "SELECT m.id,m.attempt_id,m.name,m.value_json,m.artifact_id,m.created_at "
        "FROM research_attempt_measurements m "
        "JOIN research_experiment_attempts a ON a.id=m.attempt_id "
        "JOIN research_experiment_variants v ON v.id=a.variant_id "
        "WHERE v.experiment_id=? ORDER BY m.created_at,m.id",
        (experiment["id"],),
    )
    for row in rows:
        row["value"] = json.loads(row.pop("value_json"))
    return {"label": MEASUREMENT_LABEL, "measurements": rows}


def _artifacts(connection, actor, experiment):
    return _rows(
        connection,
        "SELECT r.attempt_id,r.artifact_id FROM research_attempt_artifacts r "
        "JOIN research_experiment_attempts a ON a.id=r.attempt_id "
        "JOIN research_experiment_variants v ON v.id=a.variant_id "
        "WHERE v.experiment_id=? ORDER BY r.created_at,r.attempt_id,r.artifact_id",
        (experiment["id"],),
    )


_CHECKPOINT_SCOPE = (
    "FROM execution_result_checkpoints c WHERE c.project_id=? AND c.grant_id IN "
    "(SELECT grant_id FROM research_experiment_admitted_attempts WHERE experiment_id=?)"
)


def _checkpoints(connection, actor, experiment):
    return _rows(
        connection,
        "SELECT c.id,c.grant_id,c.manifest_id,c.manifest_digest,c.validation_contract,"
        "c.validation_digest,c.committed_at,c.state "
        + _CHECKPOINT_SCOPE
        + " ORDER BY c.committed_at,c.id",
        (experiment["project_id"], experiment["id"]),
    )


def _acceptances(connection, actor, experiment):
    return _rows(
        connection,
        "SELECT r.id,r.checkpoint_id,r.revision,r.action,r.rationale,r.created_at,"
        "r.scientific_validity,r.evidence_label,r.receipt_evidence "
        "FROM execution_result_acceptances r WHERE r.checkpoint_id IN "
        "(SELECT c.id " + _CHECKPOINT_SCOPE + ") ORDER BY r.created_at,r.id",
        (experiment["project_id"], experiment["id"]),
    )


def _conclusions(connection, actor, experiment):
    return {
        "conclusions": _rows(
            connection,
            "SELECT id,design_revision,statement,status,revision,created_at,updated_at,"
            "reviewed_at FROM research_conclusions WHERE experiment_id=? ORDER BY created_at,id",
            (experiment["id"],),
        ),
        "amendments": _rows(
            connection,
            "SELECT id,conclusion_id,relation,reason,created_at "
            "FROM research_conclusion_amendments WHERE experiment_id=? ORDER BY created_at,id",
            (experiment["id"],),
        ),
    }


_BUDGET_FIELDS = (
    "ceiling_cents",
    "currency",
    "state",
    "expires_at",
    "authorization_revision",
    "accounted_exposure_cents",
    "exposure_unknown",
    "remaining_cents",
    "provider_hard_cap_verified",
)


def _budget(connection, actor, experiment):
    try:
        status = budget_status(connection, actor, experiment["id"])
    except NotFound:
        return {"authorized": False}
    entries = _rows(
        connection,
        "SELECT category,SUM(amount_cents) AS total_cents,COUNT(*) AS entries "
        "FROM experiment_budget_entries WHERE experiment_id=? GROUP BY category "
        "ORDER BY category",
        (experiment["id"],),
    )
    return {
        "authorized": True,
        **{k: status[k] for k in _BUDGET_FIELDS},
        "entries_by_category": entries,
    }


def _program_reviews(connection, actor, study):
    allowed = ("id", "kind", "status", "revision", "created_at", "resolved_at", "rationale")
    reviews = []
    for row in program_history(connection, study["project_id"], study["id"]):
        item = {k: row[k] for k in allowed if k in row}
        item["scope"] = row["scope"]
        if row.get("evaluation_report") is not None:
            item["evaluation_report"] = row["evaluation_report"]
        reviews.append(item)
    if len(reviews) > ROW_CAP:
        raise Conflict("A result section exceeds its row limit; no partial summary is returned.")
    return reviews


def _linked_experiments(connection, study):
    ids = set()
    for row in connection.execute(
        "SELECT metadata_json FROM research_variants WHERE study_id=?", (study["id"],)
    ):
        linked = json.loads(row["metadata_json"]).get("experiment_id")
        if isinstance(linked, str):
            ids.add(linked)
    return [
        dict(row)
        for ident in sorted(ids)
        if (
            row := connection.execute(
                "SELECT id,project_id,goal,success_criterion,current_design_revision,"
                "created_at,updated_at FROM research_experiments WHERE id=? AND project_id=?",
                (ident, study["project_id"]),
            ).fetchone()
        )
    ]


def _program_experiments(connection, actor, study):
    granted = actor.get("_results_scopes")
    return [
        {
            "experiment_id": experiment["id"],
            "goal": experiment["goal"],
            "summary": _experiment_sections(connection, actor, experiment, granted)[0],
        }
        for experiment in study["experiments"]
    ]


def _program_budget(connection, actor, study):
    per, ceiling, exposure, unknown = [], 0, 0, False
    for experiment in study["experiments"]:
        item = _budget(connection, actor, experiment)
        per.append({"experiment_id": experiment["id"], **item})
        if item["authorized"]:
            ceiling += item["ceiling_cents"]
            if item["exposure_unknown"] or item["accounted_exposure_cents"] is None:
                unknown = True
            else:
                exposure += item["accounted_exposure_cents"]
    return {
        "total_ceiling_cents": ceiling,
        "accounted_exposure_cents": exposure,
        "exposure_unknown": unknown,
        "experiments": per,
    }


for _section in (
    ResultSection("attempts", "experiment", RESULTS_SCOPE, _attempts),
    ResultSection("inputs", "experiment", RESULTS_SCOPE, _inputs),
    ResultSection("measurements", "experiment", RESULTS_SCOPE, _measurements),
    ResultSection("artifacts", "experiment", RESULTS_SCOPE, _artifacts),
    ResultSection("checkpoints", "experiment", RESULTS_SCOPE, _checkpoints),
    ResultSection("acceptances", "experiment", RESULTS_SCOPE, _acceptances),
    ResultSection("conclusions", "experiment", RESULTS_SCOPE, _conclusions),
    ResultSection("comparison", "experiment", RESULTS_SCOPE, comparison_section),
    ResultSection("budget", "experiment", BUDGET_SCOPE, _budget),
    ResultSection("program_reviews", "program", RESULTS_SCOPE, _program_reviews),
    ResultSection("experiments", "program", RESULTS_SCOPE, _program_experiments),
    ResultSection("budget", "program", BUDGET_SCOPE, _program_budget),
):
    register_result_section(_section)


def _experiment_sections(connection, actor, subject, granted):
    """Return (sections, withheld); ``granted`` None means every section (human)."""
    sections, withheld = {}, []
    for section in [s for s in _REGISTRY if s.subject == "experiment"]:
        if granted is not None and section.scope not in granted:
            withheld.append({"section": section.name, "required_scope": section.scope})
        else:
            sections[section.name] = section.load(connection, actor, subject)
    return sections, withheld


def _envelope(kind, subject, project_id, sections, withheld):
    response = {
        "subject": {"kind": kind, "id": subject["id"], "project_id": project_id},
        "sections": sections,
        "withheld": withheld,
        "authority_effect": "none",
        "scientific_validity": "not_assessed",
        "evidence_boundary": EVIDENCE_BOUNDARY,
    }
    if len(json.dumps(response).encode()) > MAX_RESPONSE_BYTES:
        raise Conflict("Result summary exceeds its response limit; no partial summary is returned.")
    return response


class AgentResultStore(ExternalAgentStore):
    def _granted(self, connection, actor):
        if actor["role"] == "human":
            return None
        scope = self._scope(connection, actor, RESULTS_SCOPE)
        return frozenset(json.loads(scope["actions_json"]))

    def experiment_summary(self, principal, project_id, experiment_id):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._actor(connection, principal)
            granted = self._granted(connection, actor)
            self._project(connection, actor, project_id)
            experiment = connection.execute(
                "SELECT id,project_id,goal,success_criterion,current_design_revision,"
                "created_at,updated_at FROM research_experiments WHERE id=? AND project_id=?",
                (experiment_id, project_id),
            ).fetchone()
            if experiment is None:
                raise NotFound("Experiment was not found.")
            subject = dict(experiment)
            sections, withheld = _experiment_sections(connection, actor, subject, granted)
            return _envelope("experiment", subject, project_id, sections, withheld)
        finally:
            connection.close()

    def program_summary(self, principal, project_id, study_id):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._actor(connection, principal)
            granted = self._granted(connection, actor)
            self._project(connection, actor, project_id)
            study = connection.execute(
                "SELECT id,project_id,purpose,mode,status,created_at FROM research_studies "
                "WHERE id=? AND project_id=?",
                (study_id, project_id),
            ).fetchone()
            if study is None:
                raise NotFound("Study was not found.")
            subject = dict(study)
            subject["experiments"] = _linked_experiments(connection, subject)
            actor["_results_scopes"] = granted
            sections, withheld = {}, []
            for section in [s for s in _REGISTRY if s.subject == "program"]:
                if granted is not None and section.scope not in granted:
                    withheld.append({"section": section.name, "required_scope": section.scope})
                else:
                    sections[section.name] = section.load(connection, actor, subject)
            return _envelope("program", subject, project_id, sections, withheld)
        finally:
            connection.close()
