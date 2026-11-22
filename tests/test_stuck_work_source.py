"""Stuck findings as standing-loop work (fakes only; nothing is acted on)."""

from __future__ import annotations

import pytest
from test_standing_loop import make

from tessera_agent.stuck_work_source import KIND, StuckWorkSource

DIGEST = "a" * 64


def finding(n=1, **over):
    value = {
        "id": f"stuck_{n:032x}",
        "campaign_id": "campaign_1",
        "stage_key": "train",
        "code": "campaign_blocked",
        "suggested_action": "inspect_stop_reason_then_resume_or_cancel",
        "status": "open",
        "first_seen_at": "2026-11-22T00:00:00Z",
        "last_seen_at": "2026-11-22T01:00:00Z",
        "occurrences": 1,
        "evidence": {"status": "blocked"},
        "receipt_sha256": DIGEST,
        "acknowledged_by": None,
        "acknowledged_at": None,
        "note": None,
    }
    value.update(over)
    return value


class Control:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def call(self, method, path, body=None, key=None):
        self.calls.append((method, path, body, key))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def source(answer, **kw):
    control = Control(answer)
    return StuckWorkSource(control, "project_1", **kw), control


def test_open_findings_become_items_and_only_a_read_is_made():
    src, control = source({"project_id": "project_1", "findings": [finding(1), finding(2)]})
    items = src.observe()
    assert [i["id"] for i in items] == [f"stuck_{1:032x}:1", f"stuck_{2:032x}:1"]
    assert items[0] == {
        "id": f"stuck_{1:032x}:1",
        "kind": KIND,
        "code": "campaign_blocked",
        "suggested_action": "inspect_stop_reason_then_resume_or_cancel",
        "campaign_id": "campaign_1",
        "stage_key": "train",
        "receipt_sha256": DIGEST,
    }
    assert control.calls == [
        ("GET", "/projects/project_1/campaign-stuck?status=open&limit=50", None, None)
    ]
    assert src() == items  # callable as the loop's observe


def test_acknowledged_and_resolved_findings_are_not_offered_and_a_recurrence_is_new_work():
    src, _ = source(
        {
            "findings": [
                finding(1, status="acknowledged"),
                finding(2, status="resolved"),
                finding(3, occurrences=2),
            ]
        }
    )
    (item,) = src.observe()
    assert item["id"] == f"stuck_{3:032x}:2"


@pytest.mark.parametrize(
    "answer",
    [
        RuntimeError("down"),
        None,
        [],
        "x",
        {},
        {"findings": "no"},
        {"findings": [None, 3, "x", {}]},
    ],
)
def test_any_failure_or_malformed_answer_is_no_work_never_an_exception(answer):
    assert source(answer)[0].observe() == []


@pytest.mark.parametrize(
    "bad",
    [
        {"code": "made_up_code"},
        {"id": "../x"},
        {"occurrences": 0},
        {"occurrences": "1"},
        {"occurrences": True},
        {"campaign_id": "a/b"},
        {"receipt_sha256": "short"},
        {"suggested_action": "Do Something"},
        {"stage_key": "x/y"},
    ],
)
def test_a_malformed_finding_is_dropped_and_nothing_is_invented(bad):
    src, _ = source({"findings": [finding(1, **bad), finding(2)]})
    assert [i["id"] for i in src.observe()] == [f"stuck_{2:032x}:1"]


def test_only_allowlisted_fields_are_copied_and_a_missing_stage_key_is_none():
    extra = finding(1, stage_key=None, evidence={"secret": "x"}, note="do not copy")
    (item,) = source({"findings": [extra]})[0].observe()
    assert item["stage_key"] is None
    assert set(item) == {
        "id",
        "kind",
        "code",
        "suggested_action",
        "campaign_id",
        "stage_key",
        "receipt_sha256",
    }


def test_duplicates_collapse_and_constructor_validates():
    src, _ = source({"findings": [finding(1), finding(1)]})
    assert len(src.observe()) == 1
    with pytest.raises(ValueError):
        StuckWorkSource(Control({}), "a/b")
    with pytest.raises(ValueError):
        StuckWorkSource(Control({}), "p", limit=0)


def test_the_standing_loop_starts_one_item_and_does_not_repeat_it(tmp_path):
    src, _ = source({"findings": [finding(1)]})
    started = []
    loop, events, _, _ = make(tmp_path, [], lambda item, **kw: started.append(item))
    loop.observe = src.observe
    first = loop.tick()
    assert first["started"] == f"stuck_{1:032x}:1" and len(started) == 1
    assert started[0]["kind"] == KIND and started[0]["code"] == "campaign_blocked"
    again = loop.tick()  # the same finding is finished work for the loop, not repeated
    assert again["started"] is None and len(started) == 1
