"""Synthetic V2 response bodies exercising real bounded projection, no generation."""

import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tessera_agent.research_consumer_store import ConsumerBlocked
from tessera_agent.v2_public_output import V2FinalOutputReader


def fixture_reader():
    messages = [
        {
            "id": "msg_initial",
            "type": "assistant",
            "agent": "planner",
            "model": {"id": "model"},
            "time": {"created": 1, "completed": 2},
            "finish": "stop",
            "content": [{"type": "text", "text": "Historical initial answer"}],
        },
        {
            "id": "msg_continuation",
            "type": "user",
            "time": {"created": 3},
            "content": [{"type": "text", "text": "Admitted synthetic critic prompt"}],
        },
        {
            "id": "msg_fresh",
            "type": "assistant",
            "agent": "planner",
            "model": {"id": "model"},
            "time": {"created": 4, "completed": 5},
            "finish": "stop",
            "content": [
                {"type": "reasoning", "text": "PRIVATE"},
                {"type": "text", "text": "Fresh public planner answer"},
            ],
        },
    ]
    row = {
        "session_id": "ses_same",
        "expected_prompt_digest": hashlib.sha256(b"Admitted synthetic critic prompt").hexdigest(),
    }
    harness = SimpleNamespace(
        verify=lambda: None,
        profile={"agent": "planner", "model": {"id": "model"}},
        endpoint=SimpleNamespace(call=lambda *a: {"data": deepcopy(messages), "cursor": {}}),
    )
    return V2FinalOutputReader(harness), messages, row


def test_second_turn_reads_exact_prompt_and_fresh_assistant_not_original_final():
    reader, messages, row = fixture_reader()
    final = reader.read_after_prompt(row, "msg_continuation", 2)
    assert final.content == b"Fresh public planner answer" and final.source_sequence == 2
    assert "PRIVATE" not in repr(final)


@pytest.mark.parametrize(
    "change", ["missing_prompt", "changed_prompt", "no_fresh", "new_user", "time_collision"]
)
def test_unknown_turn_never_substitutes_previous_completed_output(change):
    reader, messages, row = fixture_reader()
    if change == "missing_prompt":
        messages.pop(1)
    elif change == "changed_prompt":
        messages[1]["content"][0]["text"] = "Different synthetic prompt"
    elif change == "no_fresh":
        messages.pop()
    elif change == "new_user":
        messages.append({"id": "msg_foreign", "type": "user", "time": {"created": 6}})
    else:
        messages[2]["time"]["created"] = 3
    with pytest.raises(ConsumerBlocked):
        reader.read_after_prompt(row, "msg_continuation", 2)
