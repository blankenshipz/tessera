"""Exact current consumption API envelope, synthetic response identities."""

import pytest

from tessera_agent.research_consumer_store import ConsumerBlocked
from tessera_agent.research_control_receipts import consumption_reference


def test_consumption_id_is_not_mistaken_for_attempt_or_operation():
    value = consumption_reference(
        {
            "consumption_id": "consumption",
            "authority_grant_id": "grant",
            "action": "attempt:dispatch",
            "result": {"id": "attempt", "operation_id": "operation"},
        },
        "grant",
        "attempt:dispatch",
    )
    assert value == {
        "consumption_id": "consumption",
        "result_id": "attempt",
        "operation_id": "operation",
    }


def test_foreign_or_fabricated_consumption_response_is_not_an_ack():
    with pytest.raises(ConsumerBlocked):
        consumption_reference(
            {"id": "fake", "action": "attempt:admit", "result": {}}, "grant", "attempt:admit"
        )


def test_accepted_task_somewhere_in_multibinding_grant_cannot_admit_another_task():
    from types import SimpleNamespace

    from tessera_agent.research_consumer import ResearchConsumer

    consumer = ResearchConsumer.__new__(ResearchConsumer)
    consumer.kind = "planner_proposal"
    consumer.run_id = "run"
    consumer.store = SimpleNamespace(effect_result=lambda *a: "proposal")

    def read(name, args):
        if name == "tessera_read_proposal":
            return {"promotion": {"proposal_id": "proposal", "task_id": "accepted-task"}}
        return {
            "bounds": {
                "bindings": [
                    {
                        "task_id": "accepted-task",
                        "variant_id": "accepted-variant",
                        "expected_variant_revision": 1,
                        "expected_design_revision": 1,
                        "execution_grant_id": "accepted-execution",
                    },
                    {
                        "task_id": "other-task",
                        "variant_id": "other-variant",
                        "expected_variant_revision": 1,
                        "expected_design_revision": 1,
                        "execution_grant_id": "other-execution",
                    },
                ]
            }
        }

    consumer.mcp = SimpleNamespace(call=read)
    with pytest.raises(ConsumerBlocked, match="accepted_grant_command_changed"):
        consumer.consume_grant(
            "authority",
            {
                "expected_revision": 1,
                "expected_epoch": 1,
                "model": "model",
                "command": {
                    "action": "attempt:admit",
                    "variant_id": "other-variant",
                    "expected_variant_revision": 1,
                    "expected_design_revision": 1,
                    "execution_grant_id": "other-execution",
                },
            },
        )
