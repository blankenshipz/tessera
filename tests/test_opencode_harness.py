from __future__ import annotations

from dataclasses import replace

import pytest

from tessera_worker.opencode_harness import (
    SYNTHETIC_SESSION_FIXTURE,
    AuthorityRejectedError,
    BoundedPlanMessage,
    OpenCodeEvent,
    OpenCodeHarnessAdapter,
    OpenCodeHarnessError,
    TesseraBinding,
    VersionCompatibilityError,
)


class FakeOpenCodeClient:
    """In-process fixture: it has no endpoint, provider, credential, or network path."""

    def __init__(self, version: str | None = "1.2.3", *, abort_reply: bool = True) -> None:
        self.version_value = version
        self.abort_reply = abort_reply
        self.calls: list[str] = []
        self.created: list[dict[str, object]] = []
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.aborts: list[str] = []
        self.event_cursors: list[int | None] = []
        self.stream: list[OpenCodeEvent] = []
        self.abort_error = False
        self.create_error = False
        self.message_error = False

    def version(self) -> str | None:
        self.calls.append("version")
        return self.version_value

    def create_session(self, request: dict[str, object]) -> str:
        self.calls.append("create_session")
        self.created.append(request)
        if self.create_error:
            raise RuntimeError("synthetic lost create reply")
        return "synthetic-session-1"

    def send_message(self, session_id: str, request: dict[str, object]) -> None:
        self.calls.append("send_message")
        self.messages.append((session_id, request))
        if self.message_error:
            raise RuntimeError("synthetic lost message reply")

    def abort(self, session_id: str) -> bool:
        self.calls.append("abort")
        self.aborts.append(session_id)
        if self.abort_error:
            raise RuntimeError("synthetic abort delivery failure")
        return self.abort_reply

    def events(self, after_cursor: int | None) -> list[OpenCodeEvent]:
        self.calls.append("events")
        self.event_cursors.append(after_cursor)
        return [item for item in self.stream if after_cursor is None or item.cursor > after_cursor]


def binding() -> TesseraBinding:
    return TesseraBinding(
        "org-1",
        "project-1",
        "task-1",
        "operation-1",
        4,
        "harness-1",
        "attempt-1",
        "operation-authority-1",
        "attempt-authority-1",
    )


def context() -> BoundedPlanMessage:
    return BoundedPlanMessage("plan-7", "Synthetic bounded plan message")


def event(
    binding_value: TesseraBinding,
    *,
    cursor: int = 0,
    event_id: str = "event-1",
    kind: str = "status",
    payload: dict[str, object] | None = None,
) -> OpenCodeEvent:
    return OpenCodeEvent(
        organization_id=binding_value.organization_id,
        project_id=binding_value.project_id,
        task_id=binding_value.task_id,
        operation_id=binding_value.operation_id,
        epoch=binding_value.epoch,
        harness_instance_id=binding_value.harness_instance_id,
        session_id=binding_value.session_id or "synthetic-session-1",
        cursor=cursor,
        event_id=event_id,
        kind=kind,
        payload=payload or {"state": "observed"},
    )


def start(
    client: FakeOpenCodeClient, **kwargs: object
) -> tuple[OpenCodeHarnessAdapter, TesseraBinding]:
    adapter = OpenCodeHarnessAdapter(
        client,
        pinned_version="1.2.3",
        authority_is_current=kwargs.pop("authority_is_current", lambda _binding, _context: True),
        **kwargs,
    )
    return adapter, adapter.start(binding(), context())


def test_start_is_pinned_bounded_synthetic_and_has_no_provider_surface() -> None:
    client = FakeOpenCodeClient()
    adapter, bound = start(client)

    assert bound.session_id == "synthetic-session-1"
    assert adapter.active_binding("operation-1") == bound
    assert client.created == [{"fixture": SYNTHETIC_SESSION_FIXTURE, "operation_id": "operation-1"}]
    assert client.messages == [
        (
            "synthetic-session-1",
            {
                "parts": [{"type": "text", "text": "Synthetic bounded plan message"}],
                "context": {"plan_revision": "plan-7"},
            },
        )
    ]
    assert client.calls == ["version", "create_session", "send_message"]
    assert not hasattr(adapter, "accept_artifact")
    assert not hasattr(adapter, "accept_result")


@pytest.mark.parametrize("pinned", [None, "", "latest", "1.2", "v1.2.3"])
def test_rejects_missing_or_unpinned_version_configuration(pinned: str | None) -> None:
    with pytest.raises(ValueError, match="exact semantic"):
        OpenCodeHarnessAdapter(
            FakeOpenCodeClient(), pinned_version=pinned, authority_is_current=lambda _a, _b: True
        )


@pytest.mark.parametrize("reported", [None, "1.2.4"])
def test_rejects_missing_or_incompatible_upstream_version_before_session(
    reported: str | None,
) -> None:
    client = FakeOpenCodeClient(reported)
    adapter = OpenCodeHarnessAdapter(
        client, pinned_version="1.2.3", authority_is_current=lambda _a, _b: True
    )
    with pytest.raises(VersionCompatibilityError, match="missing or incompatible"):
        adapter.start(binding(), context())
    assert client.calls == ["version"]


def test_rejects_invalid_binding_context_and_prebound_session() -> None:
    with pytest.raises(ValueError):
        TesseraBinding(
            "bad org",
            "project-1",
            "task-1",
            "operation-1",
            4,
            "harness-1",
            "attempt-1",
            "operation-authority-1",
            "attempt-authority-1",
        )
    with pytest.raises(ValueError):
        TesseraBinding(
            "org-1",
            "project-1",
            "task-1",
            "operation-1",
            True,
            "harness-1",
            "attempt-1",
            "operation-authority-1",
            "attempt-authority-1",
        )
    with pytest.raises(ValueError):
        BoundedPlanMessage("plan-7", "x" * (8192 + 1))
    adapter = OpenCodeHarnessAdapter(
        FakeOpenCodeClient(), pinned_version="1.2.3", authority_is_current=lambda _a, _b: True
    )
    with pytest.raises(OpenCodeHarnessError, match="cannot supply"):
        adapter.start(replace(binding(), session_id="foreign-session"), context())


@pytest.mark.parametrize(
    "authority", [lambda _a, _b: False, lambda _a, _b: (_ for _ in ()).throw(RuntimeError())]
)
def test_expired_or_revised_authority_blocks_start_without_client_calls(authority: object) -> None:
    client = FakeOpenCodeClient()
    adapter = OpenCodeHarnessAdapter(
        client,
        pinned_version="1.2.3",
        authority_is_current=authority,  # type: ignore[arg-type]
    )
    with pytest.raises(AuthorityRejectedError):
        adapter.start(binding(), context())
    assert client.calls == []


def test_authority_is_rechecked_before_version_session_and_message() -> None:
    client = FakeOpenCodeClient()
    checks: list[str] = []

    def authority(_binding: TesseraBinding, _context: BoundedPlanMessage) -> bool:
        checks.append("check")
        return len(checks) < 3

    adapter = OpenCodeHarnessAdapter(client, pinned_version="1.2.3", authority_is_current=authority)
    with pytest.raises(AuthorityRejectedError):
        adapter.start(binding(), context())

    assert checks == ["check", "check", "check"]
    assert client.calls == ["version", "create_session"]
    assert adapter.active_binding("operation-1").session_id == "synthetic-session-1"
    assert [(entry.boundary, entry.outcome) for entry in adapter.journal] == [
        ("session", "acknowledged"),
        ("message", "blocked"),
    ]


@pytest.mark.parametrize("boundary", ["create", "message"])
def test_uncertain_session_or_message_effect_is_journaled_and_never_retried(boundary: str) -> None:
    client = FakeOpenCodeClient()
    if boundary == "create":
        client.create_error = True
    else:
        client.message_error = True
    adapter = OpenCodeHarnessAdapter(
        client, pinned_version="1.2.3", authority_is_current=lambda _a, _b: True
    )

    with pytest.raises(OpenCodeHarnessError, match="uncertain"):
        adapter.start(binding(), context())
    with pytest.raises(OpenCodeHarnessError, match="already has"):
        adapter.start(binding(), context())

    assert [entry.outcome for entry in adapter.journal] == (
        ["uncertain"] if boundary == "create" else ["acknowledged", "uncertain"]
    )


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("organization_id", "org-2"),
        ("project_id", "project-2"),
        ("task_id", "task-2"),
        ("operation_id", "operation-2"),
        ("epoch", 5),
        ("harness_instance_id", "harness-2"),
    ],
)
def test_binding_mismatches_are_quarantined_without_advancing_state(
    field: str, bad_value: str | int
) -> None:
    client = FakeOpenCodeClient()
    adapter, bound = start(client)
    client.stream = [
        replace(
            event(
                bound,
                kind="session.terminal",
                payload={"state": "cancelled", "authoritative": True},
            ),
            **{field: bad_value},
        )
    ]
    adapter.cancel("operation-1")

    assert adapter.poll("operation-1") == []
    assert adapter.quarantine[-1].reason == f"binding mismatch: {field}"
    assert adapter.cancellation_status("operation-1").termination_confirmed is False
    assert client.event_cursors == [None]


def test_unknown_session_is_quarantined_and_cannot_confirm_cancellation() -> None:
    client = FakeOpenCodeClient()
    adapter, bound = start(client)
    client.stream = [
        replace(
            event(
                bound,
                kind="session.terminal",
                payload={"state": "cancelled", "authoritative": True},
            ),
            session_id="unknown-session",
        )
    ]
    adapter.cancel("operation-1")

    assert adapter.poll("operation-1") == []
    assert adapter.quarantine[-1].reason == "unknown or foreign session"
    assert adapter.cancellation_status("operation-1").unresolved is True


def test_unknown_event_kind_is_quarantined_without_advancing_replay() -> None:
    client = FakeOpenCodeClient()
    adapter, bound = start(client)
    client.stream = [event(bound, kind="upstream-new-event")]

    assert adapter.poll("operation-1") == []
    assert adapter.quarantine[-1].reason == "unknown event kind"
    assert client.event_cursors == [None]


def test_orders_delayed_events_and_reconnects_from_contiguous_cursor_without_new_session() -> None:
    client = FakeOpenCodeClient()
    adapter, bound = start(client)
    newer = event(bound, cursor=9, event_id="event-9")
    older = event(bound, cursor=4, event_id="event-4")
    client.stream = [newer, older, replace(newer, event_id="duplicate-id"), newer]

    assert adapter.poll("operation-1") == []
    assert client.event_cursors == [None]
    client.stream.extend(
        [event(bound, cursor=0, event_id="event-0"), event(bound, cursor=1, event_id="event-1")]
    )
    assert [item.cursor for item in adapter.reconnect("operation-1")] == [0, 1]
    assert client.event_cursors == [None, None]
    assert client.created == [{"fixture": SYNTHETIC_SESSION_FIXTURE, "operation_id": "operation-1"}]


def test_replay_state_restarts_at_exclusive_contiguous_cursor_and_handles_gap() -> None:
    client = FakeOpenCodeClient()
    adapter, bound = start(client)
    client.stream = [
        event(bound, cursor=0, event_id="event-0"),
        event(bound, cursor=2, event_id="event-2"),
    ]
    assert [item.cursor for item in adapter.poll("operation-1")] == [0]

    restarted = OpenCodeHarnessAdapter(
        client,
        pinned_version="1.2.3",
        authority_is_current=lambda _a, _b: True,
        replay_states=[adapter.replay_state("operation-1")],
    )
    assert restarted.reconnect("operation-1") == []
    assert client.event_cursors[-1] == 0
    client.stream.append(event(bound, cursor=1, event_id="event-1"))
    assert [item.cursor for item in restarted.reconnect("operation-1")] == [1, 2]
    assert client.event_cursors[-1] == 0


def test_prompt_is_a_scoped_observation_not_an_authority_grant() -> None:
    client = FakeOpenCodeClient()
    decisions = []
    adapter, bound = start(
        client, decision_callback=lambda request: decisions.append(request) or "grant"
    )
    client.stream = [event(bound, kind="provider-auth", payload={"provider": "fixture"})]

    translated = adapter.poll("operation-1")
    assert translated[0].acceptance == "none"
    assert [request.prompt_kind for request in decisions] == ["provider-auth"]
    assert client.calls == ["version", "create_session", "send_message", "events"]
    assert client.aborts == []


def test_secret_or_oversized_event_payload_is_quarantined_without_recording_payload() -> None:
    client = FakeOpenCodeClient()
    adapter, bound = start(client)
    client.stream = [
        event(bound, payload={"api_token": "do-not-record"}),
        event(bound, cursor=2, event_id="event-2", payload={"note": "x" * 513}),
    ]

    assert adapter.poll("operation-1") == []
    assert len(adapter.quarantine) == 2
    assert "do-not-record" not in repr(adapter.quarantine)


def test_abort_acknowledgement_is_unresolved_until_matching_authoritative_terminal_event() -> None:
    client = FakeOpenCodeClient(abort_reply=True)
    adapter, bound = start(client)
    assert adapter.cancel("operation-1").unresolved is True
    assert adapter.cancellation_status("operation-1").termination_confirmed is False
    client.stream = [
        event(
            bound,
            kind="session.terminal",
            payload={"state": "cancelled", "authoritative": False},
        )
    ]
    adapter.poll("operation-1")
    assert adapter.cancellation_status("operation-1").unresolved is True

    client.stream = [
        event(
            bound,
            cursor=1,
            event_id="event-2",
            kind="session.terminal",
            payload={"state": "cancelled", "authoritative": True},
        )
    ]
    adapter.poll("operation-1")
    assert adapter.cancellation_status("operation-1").termination_confirmed is True
    assert adapter.cancellation_status("operation-1").unresolved is False
    assert client.aborts == ["synthetic-session-1"]


def test_abort_delivery_failure_stays_requested_and_unresolved() -> None:
    client = FakeOpenCodeClient()
    client.abort_error = True
    adapter, _bound = start(client)

    status = adapter.cancel("operation-1")
    assert status.requested is True
    assert status.abort_acknowledged is False
    assert status.unresolved is True


def test_authority_invalidation_blocks_abort_and_journals_unresolved_cancellation() -> None:
    client = FakeOpenCodeClient()
    allowed = True
    adapter, _bound = start(client, authority_is_current=lambda _a, _b: allowed)
    allowed = False

    status = adapter.cancel("operation-1")

    assert status.requested is True
    assert status.unresolved is True
    assert client.aborts == []
    assert adapter.journal[-1].boundary == "abort"
    assert adapter.journal[-1].outcome == "blocked"
