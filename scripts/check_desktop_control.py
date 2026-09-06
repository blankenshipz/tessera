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


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="tessera-desktop-control-") as directory:
        root = Path(directory)
        state = root / "control"
        initialize_state(state)
        store = Store(state)
        owner = store.authenticate(
            (state / "owner.header").read_text().strip().split("Bearer ", 1)[1]
        )
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
                        "studyId": research_snapshot["study"]["id"],
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
            assert len(authored_snapshot["findings"]) == 1
            assert authored_snapshot["findings"][0]["status"] == "needs_review"
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
                        "held_restore_pass": True,
                        "research_variants_displayed": 80,
                        "research_history_unchanged": True,
                        "study_exports_match": True,
                        "execution_started": False,
                    }
                )
            )


if __name__ == "__main__":
    main()
