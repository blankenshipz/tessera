"""Opt-in native Electron/HTTP decision fixture using temporary synthetic authority.

Run with `.venv/bin/python scripts/check_desktop_control.py` after installing the
locked desktop dependencies. The file picker is stubbed only in the test harness;
production renderer, preload, IPC, authentication and control routes are exercised.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from check_control_runtime import serve
from fastapi.testclient import TestClient

from tessera_agent import (
    AgentCredential,
    AstraAgentFacade,
    ContextReference,
    ProviderDisabledCoordinator,
)
from tessera_control.api import create_app
from tessera_control.experiments import ExperimentStore
from tessera_control.interaction import InteractionStore
from tessera_control.recovery import backup_state, restore_state
from tessera_control.research import ResearchStore
from tessera_control.store import Store, _connect, canonical_digest, initialize_state


def prepare_research(state: Path, owner: dict, project_id: str) -> dict:
    """Author fixture-only ledger records; never assert provider execution."""
    research = ResearchStore(state)

    def record(method, resource_id, key, body):
        return method(owner, resource_id, key, canonical_digest(body), body)[1]

    study = record(
        research.create_study,
        project_id,
        "study",
        {"purpose": "Synthetic desktop ledger coverage", "protocol": "Platform fixture only"},
    )
    variants = []
    for index in range(80):
        variant = record(
            research.register_variant,
            study["id"],
            f"variant-{index}",
            {
                "name": f"Synthetic variant {index:02d}",
                "method_digest": "a" * 64,
                "input_digest": "b" * 64,
                "metadata": {"synthetic": True},
            },
        )
        variants.append(variant)
        if index < 4:
            record(
                research.record_variant_status,
                variant["id"],
                f"status-{index}",
                {
                    "expected_revision": variant["revision"],
                    "status": ("completed", "failed", "excluded", "abandoned")[index],
                    "reason": "Synthetic manual status; no provider execution",
                    "provenance": "manual",
                    "outcome": {"synthetic": True} if index == 0 else None,
                },
            )
    source = record(
        research.create_source,
        study["id"],
        "source",
        {
            "uri": "urn:tessera:synthetic:original",
            "quoted_passage": "Synthetic original passage",
            "content_digest": "c" * 64,
        },
    )
    record(
        research.create_finding,
        study["id"],
        "finding",
        {
            "statement": "Synthetic finding to exercise correction display",
            "source_ids": [source["id"]],
            "variant_ids": [variants[0]["id"]],
        },
    )
    record(
        research.correct_source,
        source["id"],
        "correction",
        {
            "expected_revision": source["revision"],
            "uri": "urn:tessera:synthetic:corrected",
            "quoted_passage": "Synthetic corrected passage",
            "content_digest": "d" * 64,
            "reason": "Synthetic correction fixture",
        },
    )
    return research.get_study(owner, study["id"])


def prepare_experiment(state: Path, owner: dict, project_id: str) -> dict:
    """Create a provider-disabled, read-only historical experiment projection."""
    records = ExperimentStore(state)

    def record(method, resource_id, key, body):
        return method(owner, resource_id, key, canonical_digest(body), body)[1]

    def design(goal: str, criterion: str, protocol: str, suffix: str) -> dict:
        return {
            "goal": goal,
            "success_criterion": criterion,
            "protocol": protocol,
            "variants": [
                {
                    "kind": "baseline",
                    "name": f"Synthetic baseline {suffix}",
                    "method_digest": "e" * 64,
                    "input_digest": "f" * 64,
                    "metadata": {"synthetic": True, "revision": suffix},
                },
                {
                    "kind": "intervention",
                    "name": f"Synthetic intervention {suffix}",
                    "method_digest": "0" * 64,
                    "input_digest": "1" * 64,
                    "metadata": {"synthetic": True, "revision": suffix},
                },
            ],
        }

    initial = design(
        "Historical synthetic goal.",
        "Retain the failed historical observation.",
        "Record-only fixture; no provider, OpenCode, or execution call.",
        "v1",
    )
    experiment = record(records.create_experiment, project_id, "experiment-create", initial)
    first_baseline = next(
        variant for variant in experiment["design"]["variants"] if variant["kind"] == "baseline"
    )
    record(
        records.record_attempt,
        first_baseline["id"],
        "experiment-historical-failed",
        {
            "expected_variant_revision": first_baseline["revision"],
            "provenance_kind": "human_reported_observation",
            "human_observation": "A human recorded the historical synthetic failure.",
            "outcome": "failed",
            "reason": "Synthetic historical failure remains visible.",
        },
    )
    current = design(
        "Current synthetic goal.",
        "Keep the uncertain baseline visible for human review.",
        "Record-only fixture; uncertainty remains unresolved and no work is dispatched.",
        "v2",
    )
    revised = record(
        records.revise_design,
        experiment["id"],
        "experiment-revise",
        current | {"expected_revision": experiment["current_design_revision"]},
    )
    current_baseline = next(
        variant for variant in revised["design"]["variants"] if variant["kind"] == "baseline"
    )
    record(
        records.record_attempt,
        current_baseline["id"],
        "experiment-current-inconclusive",
        {
            "expected_variant_revision": current_baseline["revision"],
            "provenance_kind": "human_reported_observation",
            "human_observation": "A human recorded an uncertain synthetic baseline observation.",
            "outcome": "inconclusive",
            "reason": "Synthetic uncertainty remains unresolved.",
        },
    )
    return records.get_experiment(owner, experiment["id"])


def prepare_provider_disabled_workbench(
    state: Path, owner_token: str, project_id: str, experiment: dict
) -> dict:
    """Persist coordinator history through the same authenticated HTTP routes.

    This is fixture setup only: the coordinator can read the selected experiment
    and write its provider-disabled records, but it cannot approve, grant, or
    dispatch anything.  The Electron pass below reads these persisted records.
    """
    agent_token = "desktop-workbench-agent-" + "x" * 40
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    agent_headers = {"Authorization": f"Bearer {agent_token}"}
    counter = 0

    with TestClient(create_app(state)) as client:

        def post(path: str, body: dict, *, agent: bool = False, expected: int = 200) -> dict:
            nonlocal counter
            counter += 1
            response = client.post(
                path,
                json=body,
                headers=(agent_headers if agent else owner_headers)
                | {"Idempotency-Key": f"desktop-workbench-{counter}"},
            )
            assert response.status_code == expected, response.text
            return response.json()

        post(
            "/credentials",
            {
                "token": agent_token,
                "kind": "agent",
                "audience": "control",
                "project_id": project_id,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            },
            expected=201,
        )
        facade = AstraAgentFacade(client, AgentCredential(token=agent_token, project_id=project_id))
        prompt = "Record a provider-disabled experiment recap for desktop history."
        requested = post(
            f"/astra/projects/{project_id}/turns",
            {
                "expected_conversation_revision": 0,
                "prompt": prompt,
                "selected_context": [
                    {
                        "kind": "experiment",
                        "record_id": experiment["experiment"]["id"],
                        "expected_revision": experiment["experiment"]["current_design_revision"],
                    }
                ],
            },
            expected=201,
        )
        claimed = post(
            f"/astra/turns/{requested['turn']['id']}/claim",
            {"expected_revision": requested["turn"]["revision"]},
            agent=True,
        )
        output = ProviderDisabledCoordinator(facade).assemble(
            prompt,
            [
                ContextReference(
                    kind="experiment",
                    record_id=experiment["experiment"]["id"],
                    expected_revision=experiment["experiment"]["current_design_revision"],
                )
            ],
        )
        proposal = post(
            f"/astra/turns/{requested['turn']['id']}/complete",
            {"expected_revision": claimed["revision"], **output.model_dump(mode="json")},
            agent=True,
        )["proposal"]
        status = post(
            f"/astra/projects/{project_id}/explain-status-turns",
            {
                "expected_conversation_revision": 2,
                "prompt": "What is the persisted experiment status?",
                "experiment_id": experiment["experiment"]["id"],
                "expected_design_revision": experiment["experiment"]["current_design_revision"],
            },
            expected=201,
        )
        claimed_status = post(
            f"/astra/explain-status-turns/{status['turn_id']}/claim",
            {"expected_revision": 1},
            agent=True,
        )
        intent = post(
            f"/astra/explain-status-turns/{status['turn_id']}/tool-intent",
            {
                "expected_revision": claimed_status["binding"]["revision"],
                "tool": "read_experiment_status",
                "arguments": {"identifier": experiment["experiment"]["id"]},
            },
            agent=True,
        )
        recorded = post(
            f"/astra/explain-status-turns/{status['turn_id']}/tool-result",
            {
                "expected_revision": intent["binding"]["revision"],
                "result": facade.read_experiment_status(experiment["experiment"]["id"]).model_dump(
                    mode="json"
                ),
            },
            agent=True,
        )
        answered = post(
            f"/astra/explain-status-turns/{status['turn_id']}/answer",
            {"expected_revision": recorded["binding"]["revision"]},
            agent=True,
        )
        assert answered["answer"]["provider_disabled"] is True
        assert answered["answer"]["operation_ids"] == []
        return {"proposal": proposal["body"], "answer": answered["answer"]["body"]}


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="tessera-desktop-control-") as directory:
        root = Path(directory)
        state = root / "control"
        initialize_state(state)
        store = Store(state)
        owner_token = (state / "owner.header").read_text().strip().split("Bearer ", 1)[1]
        owner = store.authenticate(owner_token)
        body = {"name": "Synthetic desktop decision fixture"}
        _, project = store.create_project(owner, "project", canonical_digest(body), body)
        interaction = InteractionStore(store)
        _, decision = interaction.request_decision(
            owner,
            "decision",
            project["id"],
            "direction",
            {"instruction": "Record synthetic platform acceptance only; do not launch work."},
            (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        )
        research_snapshot = prepare_research(state, owner, project["id"])
        experiment_snapshot = prepare_experiment(state, owner, project["id"])
        workbench = prepare_provider_disabled_workbench(
            state, owner_token, project["id"], experiment_snapshot
        )

        def run_smoke(endpoint: str, header: Path, held: bool) -> None:
            fixture = root / ("held-fixture.json" if held else "fixture.json")
            export_path = root / ("held-study.json" if held else "study.json")
            fixture.write_text(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "header": str(header),
                        "projectId": project["id"],
                        "decisionId": decision["id"],
                        "experimentId": experiment_snapshot["experiment"]["id"],
                        "studyId": research_snapshot["study"]["id"],
                        "coordinatorProposal": workbench["proposal"],
                        "statusAnswer": workbench["answer"],
                        "held": held,
                        "exportPath": str(export_path),
                        "authoredExportPath": str(root / "authored-study.json"),
                    }
                )
            )
            environment = dict(os.environ)
            environment.pop("ELECTRON_RUN_AS_NODE", None)
            harness = repository / "desktop/test/electron-control-smoke.cjs"
            subprocess.run(["node", "--check", str(harness)], check=True, timeout=10)
            process = subprocess.Popen(
                [
                    str(repository / "desktop/node_modules/.bin/electron"),
                    str(harness),
                    str(fixture),
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                _stdout, stderr = process.communicate(timeout=35)
            except subprocess.TimeoutExpired:
                # The npm Electron launcher owns a child process. Stop this
                # fixture's whole process group so a failed launch cannot leak it.
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
                raise
            if process.returncode:
                raise RuntimeError(f"Desktop control fixture failed: {stderr[-2000:]}")
            assert json.loads(export_path.read_text()) == research_snapshot

        with serve(state) as endpoint:
            run_smoke(endpoint, state / "owner.header", held=False)
            research = ResearchStore(state)
            authored = [
                study
                for study in research.list_studies(owner, project["id"])
                if study["purpose"] == "Synthetic authored study"
            ]
            assert len(authored) == 1
            authored_snapshot = research.get_study(owner, authored[0]["id"])
            assert authored_snapshot["study"]["created_by_principal_id"] == owner["id"]
            assert len(authored_snapshot["variants"]) == 2
            assert json.loads((root / "authored-study.json").read_text()) == authored_snapshot
            registered = authored_snapshot["variants"][0]
            assert len(authored_snapshot["sources"]) == 2
            assert len(authored_snapshot["findings"]) == 2
            assert authored_snapshot["findings"][0]["status"] == "superseded"
            replacement_finding = authored_snapshot["findings"][1]
            assert replacement_finding["status"] == "retracted"
            assert (
                replacement_finding["supersedes_finding_id"]
                == authored_snapshot["findings"][0]["id"]
            )
            assert replacement_finding["source_ids"] == [authored_snapshot["sources"][1]["id"]]
            assert replacement_finding["revision_reason"] == (
                "Synthetic revision preserves the original claim."
            )
            assert authored_snapshot["findings"][0]["accepted_by_principal_id"] == owner["id"]
            assert len(authored_snapshot["corrections"]) == 1
            reaffirmations = [
                event
                for event in authored_snapshot["events"]
                if event["event_type"] == "research.finding_reaffirmed"
            ]
            assert len(reaffirmations) == 1
            assert reaffirmations[0]["actor_principal_id"] == owner["id"]
            assert reaffirmations[0]["payload"]["prior_revision"] == 3
            assert reaffirmations[0]["payload"]["reason"] == (
                "Synthetic reaffirmation of unchanged evidence."
            )
            retractions = [
                event
                for event in authored_snapshot["events"]
                if event["event_type"] == "research.finding_retracted"
            ]
            revisions = [
                event
                for event in authored_snapshot["events"]
                if event["event_type"] == "research.finding_revised"
            ]
            assert len(revisions) == 1
            assert revisions[0]["actor_principal_id"] == owner["id"]
            assert revisions[0]["payload"]["prior_revision"] == 5
            assert revisions[0]["payload"]["replacement_finding_id"] == replacement_finding["id"]
            assert len(retractions) == 1
            assert retractions[0]["actor_principal_id"] == owner["id"]
            assert retractions[0]["payload"]["prior_revision"] == 2
            assert retractions[0]["payload"]["reason"] == (
                "Synthetic retraction after corrected evidence."
            )
            assert authored_snapshot["study"]["status"] == "closed"
            assert {variant["status"] for variant in authored_snapshot["variants"]} == {
                "failed",
                "abandoned",
            }
            assert registered["method_digest"] == "e" * 64
            assert registered["input_digest"] == "f" * 64
            assert registered["metadata"] == {"synthetic": True}
            assert registered["execution_receipt"] is None
            resolved = interaction.list_decisions(owner, project["id"], status="approved")
            assert len(resolved) == 1 and resolved[0]["id"] == decision["id"]
            assert resolved[0]["rationale"] == "Synthetic UI acceptance fixture."
            assert resolved[0]["resolved_by_principal_id"] == owner["id"]
            assert (
                ResearchStore(state).get_study(owner, research_snapshot["study"]["id"])
                == research_snapshot
            )
            assert (
                ExperimentStore(state).get_experiment(
                    owner, experiment_snapshot["experiment"]["id"]
                )
                == experiment_snapshot
            )
            with closing(_connect(store.database_path)) as connection:
                assert (
                    connection.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 0
                )
                assert (
                    connection.execute("SELECT count(*) FROM execution_leases").fetchone()[0] == 0
                )

        backup = root / "backup"
        backup_state(state, backup)
        restored = root / "restored"
        restore_state(backup, restored)
        restored_owner = Store(restored).authenticate(
            (restored / "owner.header").read_text().strip().split("Bearer ", 1)[1]
        )
        with serve(restored) as endpoint:
            run_smoke(endpoint, restored / "owner.header", held=True)
            assert (
                ResearchStore(restored).get_study(restored_owner, research_snapshot["study"]["id"])
                == research_snapshot
            )
            assert (
                ExperimentStore(restored).get_experiment(
                    restored_owner, experiment_snapshot["experiment"]["id"]
                )
                == experiment_snapshot
            )
            assert (
                ResearchStore(restored).get_study(restored_owner, authored[0]["id"])
                == authored_snapshot
            )
            restored_interaction = InteractionStore(Store(restored))
            restored_decisions = restored_interaction.list_decisions(
                restored_owner, project["id"], status="approved"
            )
            assert len(restored_decisions) == 1
            assert restored_decisions[0]["id"] == decision["id"]
            assert restored_decisions[0]["resolved_by_principal_id"] == owner["id"]
            print(
                json.dumps(
                    {
                        "desktop_decision": "approved",
                        "desktop_study_and_variant": "accounted_and_closed",
                        "evidence_review_and_correction": True,
                        "finding_reaffirmation": True,
                        "finding_retraction": True,
                        "finding_revision": True,
                        "held_restore_pass": True,
                        "research_variants_displayed": 80,
                        "research_history_unchanged": True,
                        "experiment_history_reconnected_unchanged": True,
                        "provider_disabled_workbench_reconnected_unchanged": True,
                        "study_exports_match": True,
                        "execution_started": False,
                    }
                )
            )


if __name__ == "__main__":
    main()
