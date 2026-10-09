"""Complete staged loop on synthetic ports; not model or live execution proof."""

from pathlib import Path
from types import SimpleNamespace

from test_research_journey import setup  # noqa: F401

from tessera_agent.research_consumer_store import ResearchConsumerStore
from tessera_agent.research_journey import ResearchJourney


def test_ordered_synthetic_loop_polls_same_operation_after_journal_restart(request):
    make, planner, critic, state, cancellations, status = request.getfixturevalue("setup")
    calls = []
    journey = make()
    planner.tick = lambda text: {"reasoning_state": "completed", "output_id": "planner-output"}
    critic.tick = lambda text: {"reasoning_state": "completed", "output_id": "critic-output"}
    planner.capture = critic.capture = lambda *a: {
        "acknowledged_events": 1,
        "unknown_capture_events": 1,
    }
    critic.deliver_critic = lambda *a: calls.append("peer-result") or "peer-task"
    planner.consume_critic = lambda *a: calls.append("planner-read-ack")
    journey.initial_prompts.read = lambda role: "Synthetic admitted " + role
    journey.initial_prompts.verify_critic_input = lambda output: calls.append(
        "independent-critic-context"
    )
    pins = {
        "family": "research-generated-v1",
        "output_id": "critic-output",
        "disclosure_id": "planner-disclosure",
        "actor_id": "critic",
        "critic_credential_id": "critic",
        "org_id": "org",
        "project_id": "project",
        "model_id": "model",
        "run_id": "critic",
        "session_id": critic.binding["session_id"],
        "harness_instance_id": "instance",
        "context_id": "criticcontext",
        "context_digest": "a" * 64,
        "skill_digest": "b" * 64,
        "config_digest": "c" * 64,
        "task_id": "central-task",
        "operation_id": None,
        "source_event_id": "critic-final",
        "source_sequence": 1,
        "content_digest": "sha256:" + "d" * 64,
        "registration_grant_id": "registration",
        "registration_revision": 1,
        "output_kind": "critic_review",
        "capture_record_id": "capture",
        "capture_sequence": 1,
    }
    journey.critic_transport.read_registered_pins = lambda *a: pins
    planner.supervisor.tick = lambda run: planner.store.runtime.get(run)
    state.update(
        planner_author_disclosure_id="planner-author-disclosure",
        critic_author_disclosure_id="critic-author-disclosure",
        transport_authority={
            "grant_id": "transport",
            "revision": 1,
            "epoch": 1,
            "binding": {
                "variant_id": "variant",
                "expected_variant_revision": 1,
                "expected_design_revision": 1,
                "execution_grant_id": "execution",
                "expected_execution_grant_revision": 1,
                "expected_execution_epoch": 1,
                "task_id": "central-task",
                "expected_task_revision": 1,
                "plan_digest": "a" * 64,
                "target_digest": "a" * 64,
            },
        },
    )
    for expected in (
        "planner_disclosure",
        "critic",
        "critic_disclosure",
        "peer_delivery",
        "planner_consume",
        "planner_continue",
        "planner_continuation_running",
    ):
        assert journey.advance()["stage"] == expected
    assert calls == ["independent-critic-context", "peer-result", "planner-read-ack"]
    assert len(planner.prompts) == 1
    planner.store.runtime.state(planner.run_id, "completed")
    journey.output_pipeline.register_and_capture = lambda *a, **k: {
        "output_id": "continuation-output",
        "source_id": "continuation-source",
        "source_sequence": 2,
        "output_digest": "e" * 64,
        "author_disclosure_id": "continuation-author",
    }
    journey.output_pipeline.propose_registered = lambda *a: {"id": "proposal"}
    assert journey.advance()["stage"] == "submit_review_proposal"
    assert journey.advance()["stage"] == "human_review"
    assert journey.advance()["stage"] == "human_review"

    def mcp(tool, args):
        if tool == "tessera_read_proposal":
            return {
                "id": "proposal",
                "promotion": {"proposal_id": "proposal", "task_id": "approved-task"},
            }
        calls.append("status")
        return {"result": {"id": "attempt", "status": status["status"]}}

    planner.mcp = SimpleNamespace(call=mcp)
    planner.consume_grant = lambda grant, payload: {
        "consumption_id": "consumption-" + payload["command"]["action"].split(":")[1],
        "result_id": "attempt",
        "operation_id": "operation" if payload["command"]["action"] == "attempt:dispatch" else None,
    }

    def command(value):
        return {
            "grant_id": "execution-authority",
            "payload": {
                "expected_revision": 1,
                "expected_epoch": 1,
                "model": "model",
                "command": value,
            },
        }

    state.update(
        review_state="approved",
        review_id="human-review",
        review_subject_id="proposal",
        approved_task_id="approved-task",
        commands={
            "admit": command(
                {
                    "action": "attempt:admit",
                    "variant_id": "variant",
                    "expected_variant_revision": 1,
                    "expected_design_revision": 1,
                    "execution_grant_id": "execution",
                }
            ),
            "dispatch": command(
                {
                    "action": "attempt:dispatch",
                    "attempt_id": "attempt",
                    "expected_revision": 1,
                    "target_digest": "a" * 64,
                    "profile_version": "development-minikube-opencode-v1",
                }
            ),
            "status": command({"action": "attempt:status", "attempt_id": "attempt"}),
        },
    )
    for expected in ("admit", "dispatch", "experiment_running"):
        assert journey.advance()["stage"] == expected
    journey.output_pipeline.attempt_status = lambda response, attempt: response["result"]["status"]
    assert journey.advance()["stage"] == "experiment_running"
    before = journey.ref("operation_id")
    for role in (planner, critic):
        directory = Path(role.store.runtime.db.execute("PRAGMA database_list").fetchone()[2]).parent
        role.store.close()
        role.store = ResearchConsumerStore(directory)
    resumed = ResearchJourney(
        "journey",
        planner,
        critic,
        approvals=journey.approvals,
        output_pipeline=journey.output_pipeline,
        initial_prompts=journey.initial_prompts,
        critic_transport=journey.critic_transport,
    )
    assert resumed.ref("operation_id") == before == "operation"
    assert len(planner.prompts) == 1
    status["status"] = "succeeded"
    resumed.output_pipeline.observe_registered_experiment = lambda *a: calls.append("observation")
    resumed.output_pipeline.export_notebook = lambda *a: calls.append("export")
    planner.notebook = lambda: calls.append("notebook")
    for expected in ("record_observation", "notebook", "completed"):
        assert resumed.advance()["stage"] == expected
    assert calls[-3:] == ["observation", "notebook", "export"]
