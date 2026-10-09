"""Durable orchestration with synthetic authenticated ports/model outcomes, no inference."""

from types import SimpleNamespace

import pytest

from tessera_agent.research_consumer_store import ConsumerBlocked, ResearchConsumerStore
from tessera_agent.research_journey import ResearchJourney


class Role:
    def __init__(self, path, kind, name):
        self.kind, self.run_id = kind, name
        self.store = ResearchConsumerStore(path)
        row = self.store.runtime.bind(name, {"profile_sha256": "a" * 64}, 1)
        self.store.runtime.state(name, "completed")
        self.binding = {
            "credential_id": name,
            "context_id": name + "context",
            "project_id": "project",
            "session_id": row["session_id"],
        }
        self.peer = self.collector = None
        self.prompts = []
        self.fail_prompt = False

        def prompt(row, text):
            self.prompts.append((row["session_id"], row["message_id"]))
            if self.fail_prompt:
                raise OSError("synthetic lost acknowledgement")

        self.supervisor = SimpleNamespace(
            harness=SimpleNamespace(prompt=prompt), halt=lambda *a: None, profile={"max_steps": 1}
        )

    def fence(self):
        return self.store.runtime.get(self.run_id)


@pytest.fixture
def setup(tmp_path):
    planner = Role(tmp_path / "planner", "planner_proposal", "planner")
    critic = Role(tmp_path / "critic", "critic_review", "critic")
    state = {"journey_id": "journey", "review_state": "pending"}
    cancellations = []
    status = {"attempt_id": "attempt", "status": "running"}
    approvals = SimpleNamespace(
        poll=lambda identifier: dict(state),
        consume_cancel=lambda *a: cancellations.append(a) or {"id": "cancel-receipt"},
        read_cancel_status=lambda *a: dict(status),
    )
    prompts = SimpleNamespace(
        read_continuation=lambda output: "Synthetic admitted critic response",
        verify_continuation=lambda output, digest: {
            "session_id": planner.binding["session_id"],
            "prompt_digest": digest,
            "observed_session_steps": 1,
        },
    )

    def make():
        return ResearchJourney(
            "journey",
            planner,
            critic,
            approvals=approvals,
            output_pipeline=SimpleNamespace(),
            initial_prompts=prompts,
            critic_transport=SimpleNamespace(),
        )

    yield make, planner, critic, state, cancellations, status
    planner.store.close()
    critic.store.close()


def test_same_session_continuation_journaled_before_effect_and_restart(setup):
    make, planner, critic, state, cancellations, status = setup
    journey = make()
    journey.ref("critic_output", "critic-output")
    journey.stage("planner_continue")
    receipt = journey.advance()
    assert receipt["stage"] == "planner_continuation_running"
    assert len(planner.prompts) == 1
    assert planner.prompts[0][0] == planner.binding["session_id"]
    assert planner.prompts[0][1] == journey.ref("continuation_message_id")
    restarted = make()
    assert restarted.ref("continuation_message_id") == planner.prompts[0][1]
    # Crash after acknowledged prompt but before stage update never re-prompts.
    planner.store.runtime.state(planner.run_id, "completed")
    restarted.stage("planner_continue")
    restarted.advance()
    assert len(planner.prompts) == 1


def test_unknown_continuation_is_not_replayed_and_exact_authenticated_readback(setup):
    make, planner, critic, state, cancellations, status = setup
    journey = make()
    journey.ref("critic_output", "critic-output")
    journey.stage("planner_continue")
    planner.fail_prompt = True
    with pytest.raises(ConsumerBlocked, match="continuation_unknown"):
        journey.advance()
    with pytest.raises(ConsumerBlocked, match="continuation_unknown"):
        make().advance()
    assert len(planner.prompts) == 1
    reader = SimpleNamespace(
        read_prompt_receipt=lambda session, message: {
            "session_id": session,
            "message_id": message,
            "prompt_digest": journey.ref("continuation_prompt_digest"),
        }
    )
    journey.reconcile_continuation(reader)
    assert journey.receipt()["stage"] == "planner_continuation_running"
    assert len(planner.prompts) == 1


def test_rejected_human_record_stops_before_grant_consumption(setup):
    make, planner, critic, state, cancellations, status = setup
    journey = make()
    journey.ref("proposal_id", "proposal")
    journey.stage("human_review")
    state.update(review_state="rejected", review_id="human-review", review_subject_id="proposal")
    assert journey.advance()["stage"] == "rejected"
    assert not planner.prompts and not cancellations


def test_cancel_ack_does_not_claim_running_experiment_terminated(setup):
    make, planner, critic, state, cancellations, status = setup
    journey = make()
    journey.ref("attempt_id", "attempt")
    journey.stage("experiment_running")
    state.update(
        control_state="cancel",
        commands={
            "cancel": {
                "grant_id": "cancel-grant",
                "payload": {
                    "expected_revision": 1,
                    "expected_epoch": 1,
                    "model": "model",
                    "command": {
                        "action": "execution:cancel",
                        "execution_grant_id": "execution",
                        "expected_revision": 1,
                    },
                },
            }
        },
    )
    assert journey.advance()["stage"] == "cancel_requested"
    assert make().advance()["stage"] == "cancel_requested"
    assert len(cancellations) == 1
    status["status"] = "canceled"
    assert make().advance()["stage"] == "canceled"
    assert len(cancellations) == 1


def test_unrecognized_authenticated_snapshot_is_not_journaled(setup):
    make, planner, critic, state, cancellations, status = setup
    state["raw_tool_output"] = "synthetic-private"
    with pytest.raises(ConsumerBlocked, match="unsupported_authenticated"):
        make().advance()
    assert "synthetic-private" not in str(
        list(planner.store.db.execute("SELECT * FROM research_journey_refs"))
    )


def test_lost_cancel_ack_holds_same_key_until_explicit_typed_reconciliation(setup):
    make, planner, critic, state, cancellations, status = setup
    journey = make()
    journey.ref("attempt_id", "attempt")
    journey.stage("experiment_running")
    state.update(
        control_state="cancel",
        commands={
            "cancel": {
                "grant_id": "cancel-grant",
                "payload": {
                    "expected_revision": 1,
                    "expected_epoch": 1,
                    "model": "model",
                    "command": {
                        "action": "execution:cancel",
                        "execution_grant_id": "execution",
                        "expected_revision": 1,
                    },
                },
            }
        },
    )
    calls = []

    def cancel(*args):
        calls.append(args[-1])
        if len(calls) == 1:
            raise OSError("synthetic lost cancel acknowledgement")
        return {"id": "cancel-receipt"}

    journey.approvals.consume_cancel = cancel
    with pytest.raises(OSError):
        journey.advance()
    with pytest.raises(ConsumerBlocked, match="cancel_ack_unknown"):
        make().advance()
    assert len(calls) == 1
    journey.reconcile_cancel()
    assert calls[0] == calls[1] and journey.receipt()["stage"] == "cancel_requested"


def test_continuation_cannot_reset_cumulative_three_step_budget(setup):
    make, planner, critic, state, cancellations, status = setup
    journey = make()
    journey.ref("critic_output", "critic-output")
    journey.stage("planner_continue")
    planner.supervisor.profile["max_steps"] = 3
    with pytest.raises(ConsumerBlocked, match="cumulative_step_admission"):
        journey.advance()
    assert not planner.prompts


def test_critic_active_prompt_restart_does_not_readmit_updated_human_record(setup):
    make, planner, critic, state, cancellations, status = setup
    journey = make()
    journey.ref("planner_initial_output", "planner-output")
    journey.stage("critic")
    assert critic.store.runtime.begin(critic.run_id, "prompt")
    critic.store.runtime.finish(critic.run_id, "prompt", "acknowledged")
    calls = []
    critic.tick = lambda prompt=None: (
        calls.append(prompt) or {"reasoning_state": "running", "output_id": None}
    )

    def no_new_input(*args):
        raise AssertionError("Already submitted critic prompt must not be re-admitted")

    journey.initial_prompts.verify_critic_input = no_new_input
    journey.initial_prompts.read = no_new_input
    assert journey.advance()["stage"] == "critic"
    assert calls == [None]
