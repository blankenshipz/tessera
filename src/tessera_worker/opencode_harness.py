"""Synthetic, pinned OpenCode harness adapter proof of concept.

This module has no HTTP client, provider integration, credential handling, or
control-plane writes.  A trusted supervisor supplies an in-process client and
continues to own authority, dispatch, artifacts, and result acceptance.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, TypeAlias

SYNTHETIC_SESSION_FIXTURE = "tessera-opencode-poc-v1"
MAX_CONTEXT_BYTES = 8 * 1024
MAX_EVENT_BYTES = 4 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")
_SECRET_KEY = re.compile(r"(?:secret|token|credential|authorization|password|api[_-]?key)", re.I)
_PROMPT_KINDS = frozenset({"permission", "tool", "mcp", "provider-auth"})
_KNOWN_EVENT_KINDS = _PROMPT_KINDS | frozenset({"status", "session.terminal"})

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class OpenCodeHarnessError(Exception):
    """The synthetic adapter cannot safely establish or observe an effect."""


class VersionCompatibilityError(OpenCodeHarnessError):
    """OpenCode did not report the exact pinned version."""


class AuthorityRejectedError(OpenCodeHarnessError):
    """The caller's authority callback failed closed before a session started."""


def _identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a bounded identifier.")


@dataclass(frozen=True)
class TesseraBinding:
    """The fields that fence every session and translated observation."""

    organization_id: str
    project_id: str
    task_id: str
    operation_id: str
    epoch: int
    harness_instance_id: str
    session_id: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "organization_id",
            "project_id",
            "task_id",
            "operation_id",
            "harness_instance_id",
        ):
            _identifier(getattr(self, field), field)
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("epoch must be a non-negative integer.")
        if self.session_id is not None:
            _identifier(self.session_id, "session_id")


@dataclass(frozen=True)
class BoundedPlanMessage:
    """One bounded plan revision and user-visible message; neither is persisted here."""

    plan_revision: str
    message: str

    def __post_init__(self) -> None:
        _identifier(self.plan_revision, "plan_revision")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be non-empty text.")
        if len(self.message.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise ValueError("message exceeds the synthetic adapter bound.")


@dataclass(frozen=True)
class OpenCodeEvent:
    """A fixture event with upstream attribution claims supplied by OpenCode."""

    organization_id: str
    project_id: str
    task_id: str
    operation_id: str
    epoch: int
    harness_instance_id: str
    session_id: str
    cursor: int
    event_id: str
    kind: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        for field in (
            "organization_id",
            "project_id",
            "task_id",
            "operation_id",
            "harness_instance_id",
            "session_id",
            "event_id",
            "kind",
        ):
            _identifier(getattr(self, field), field)
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("event epoch must be a non-negative integer.")
        if type(self.cursor) is not int or self.cursor < 0:
            raise ValueError("event cursor must be a non-negative integer.")
        if not isinstance(self.payload, Mapping):
            raise ValueError("event payload must be a mapping.")


class OpenCodeClient(Protocol):
    """The deliberately small injected client surface used by this POC."""

    def version(self) -> str | None: ...

    def create_session(self, request: Mapping[str, object]) -> str: ...

    def send_message(self, session_id: str, request: Mapping[str, object]) -> None: ...

    def abort(self, session_id: str) -> bool: ...

    def events(self, after_cursor: int | None) -> Iterable[OpenCodeEvent]: ...


@dataclass(frozen=True)
class AttributedEvent:
    """A bounded observation, never an artifact or result acceptance."""

    binding: TesseraBinding
    upstream_session_id: str
    cursor: int
    event_id: str
    kind: str
    payload: dict[str, JSONValue]
    acceptance: str = "none"


@dataclass(frozen=True)
class QuarantinedEvent:
    """Secret-free metadata for an event that could not affect active state."""

    reason: str
    session_id: str
    event_id: str
    cursor: int


@dataclass(frozen=True)
class ScopedDecisionRequest:
    """A recorded callback notification, not an upstream permission response."""

    binding: TesseraBinding
    upstream_session_id: str
    event_id: str
    cursor: int
    prompt_kind: str


@dataclass(frozen=True)
class CancellationStatus:
    requested: bool
    abort_acknowledged: bool
    termination_confirmed: bool
    unresolved: bool


@dataclass(frozen=True)
class UpstreamEffect:
    """A secret-free synthetic journal entry for a privileged upstream call."""

    operation_id: str
    boundary: str
    outcome: str
    session_id: str | None = None


@dataclass(frozen=True)
class ReplayState:
    """Restartable replay metadata; ``after_cursor`` is exclusive and contiguous."""

    binding: TesseraBinding
    after_cursor: int | None
    seen_event_ids: tuple[str, ...]
    cancellation_requested: bool
    abort_acknowledged: bool
    termination_confirmed: bool


@dataclass
class _Operation:
    binding: TesseraBinding
    high_water_cursor: int | None = None
    seen_event_ids: dict[str, int] | None = None
    seen_cursors: dict[int, str] | None = None
    pending_events: dict[int, OpenCodeEvent] | None = None
    cancellation_requested: bool = False
    abort_acknowledged: bool = False
    termination_confirmed: bool = False

    def __post_init__(self) -> None:
        self.seen_event_ids = {}
        self.seen_cursors = {}
        self.pending_events = {}


class OpenCodeHarnessAdapter:
    """Translate synthetic OpenCode fixture calls without changing Tessera authority."""

    def __init__(
        self,
        client: OpenCodeClient,
        *,
        pinned_version: str,
        authority_is_current: Callable[[TesseraBinding, BoundedPlanMessage], bool],
        decision_callback: Callable[[ScopedDecisionRequest], object] | None = None,
        replay_states: Iterable[ReplayState] = (),
    ) -> None:
        if not isinstance(pinned_version, str) or not _VERSION.fullmatch(pinned_version):
            raise ValueError("pinned_version must be an exact semantic version.")
        self._client = client
        self._pinned_version = pinned_version
        self._authority_is_current = authority_is_current
        self._decision_callback = decision_callback
        self._operations: dict[str, _Operation] = {}
        self.quarantine: list[QuarantinedEvent] = []
        self.journal: list[UpstreamEffect] = []
        for replay_state in replay_states:
            self.restore(replay_state)

    def start(self, binding: TesseraBinding, context: BoundedPlanMessage) -> TesseraBinding:
        """Create one deterministic fixture session after version and authority checks."""
        if binding.session_id is not None:
            raise OpenCodeHarnessError("A new operation cannot supply a session identity.")
        if binding.operation_id in self._operations:
            raise OpenCodeHarnessError("Operation already has a synthetic session.")
        self._require_current_authority(binding, context)
        reported_version = self._client.version()
        if not isinstance(reported_version, str) or reported_version != self._pinned_version:
            raise VersionCompatibilityError(
                "OpenCode version is missing or incompatible with the pin."
            )
        self._require_current_authority(binding, context)
        try:
            session_id = self._client.create_session(
                {
                    "fixture": SYNTHETIC_SESSION_FIXTURE,
                    "operation_id": binding.operation_id,
                }
            )
        except Exception as error:
            # A lost create reply may have made a session.  Reserve this operation
            # identity and require reconciliation rather than another create.
            self._operations[binding.operation_id] = _Operation(binding=binding)
            self._journal(binding, "session", "uncertain")
            raise OpenCodeHarnessError("Session creation is uncertain; reconcile it.") from error
        try:
            _identifier(session_id, "session_id")
        except ValueError as error:
            self._operations[binding.operation_id] = _Operation(binding=binding)
            self._journal(binding, "session", "uncertain")
            raise OpenCodeHarnessError("Session identity is uncertain; reconcile it.") from error
        bound = replace(binding, session_id=session_id)
        self._operations[binding.operation_id] = _Operation(binding=bound)
        self._journal(bound, "session", "acknowledged")
        try:
            self._require_current_authority(bound, context)
        except AuthorityRejectedError:
            self._journal(bound, "message", "blocked")
            raise
        try:
            self._client.send_message(
                session_id,
                {
                    "parts": [{"type": "text", "text": context.message}],
                    "context": {"plan_revision": context.plan_revision},
                },
            )
        except Exception as error:
            # Keep the acknowledged session and never resend under another identity.
            self._journal(bound, "message", "uncertain")
            raise OpenCodeHarnessError("Message delivery is uncertain; reconcile it.") from error
        self._journal(bound, "message", "acknowledged")
        return bound

    def active_binding(self, operation_id: str) -> TesseraBinding:
        _identifier(operation_id, "operation_id")
        return self._operation(operation_id).binding

    def poll(self, operation_id: str) -> list[AttributedEvent]:
        """Replay after the exclusive contiguous cursor, holding gaps for later delivery."""
        operation = self._operation(operation_id)
        translated: list[AttributedEvent] = []
        for event in self._client.events(after_cursor=operation.high_water_cursor):
            if event.session_id != operation.binding.session_id:
                self._quarantine(event, "unknown or foreign session")
                continue
            mismatch = self._binding_mismatch(event, operation.binding)
            if mismatch:
                self._quarantine(event, f"binding mismatch: {mismatch}")
                continue
            if event.kind not in _KNOWN_EVENT_KINDS:
                self._quarantine(event, "unknown event kind")
                continue
            assert (
                operation.seen_event_ids is not None
                and operation.seen_cursors is not None
                and operation.pending_events is not None
            )
            known_cursor = operation.seen_event_ids.get(event.event_id)
            if known_cursor is not None:
                if known_cursor != event.cursor:
                    self._quarantine(event, "event identity reused at another cursor")
                continue
            known_event_id = operation.seen_cursors.get(event.cursor)
            if known_event_id is not None:
                if known_event_id != event.event_id:
                    self._quarantine(event, "cursor reused by another event")
                continue
            if (
                operation.high_water_cursor is not None
                and event.cursor <= operation.high_water_cursor
            ):
                self._quarantine(event, "delayed event before replay cursor")
                continue
            pending = operation.pending_events.get(event.cursor)
            if pending is not None:
                if pending.event_id != event.event_id:
                    self._quarantine(event, "cursor reused by another event")
                continue
            if any(
                pending.event_id == event.event_id for pending in operation.pending_events.values()
            ):
                self._quarantine(event, "event identity reused at another cursor")
                continue
            try:
                _bounded_payload(event.payload)
            except ValueError:
                self._quarantine(event, "unsafe or oversized event payload")
                continue
            operation.pending_events[event.cursor] = event
        self._drain_contiguous_events(operation, translated)
        return translated

    def reconnect(self, operation_id: str) -> list[AttributedEvent]:
        """Resume the existing operation's stream from its last accepted cursor."""
        return self.poll(operation_id)

    def replay_state(self, operation_id: str) -> ReplayState:
        """Return state a supervisor must retain before adapter restart."""
        operation = self._operation(operation_id)
        assert operation.seen_event_ids is not None
        return ReplayState(
            binding=operation.binding,
            after_cursor=operation.high_water_cursor,
            seen_event_ids=tuple(sorted(operation.seen_event_ids)),
            cancellation_requested=operation.cancellation_requested,
            abort_acknowledged=operation.abort_acknowledged,
            termination_confirmed=operation.termination_confirmed,
        )

    def restore(self, replay_state: ReplayState) -> None:
        """Restore an acknowledged session without creating or messaging a new one."""
        binding = replay_state.binding
        if binding.session_id is None:
            raise ValueError("Only an acknowledged session can be restored.")
        if binding.operation_id in self._operations:
            raise OpenCodeHarnessError("Operation already has replay state.")
        if replay_state.after_cursor is not None and replay_state.after_cursor < 0:
            raise ValueError("replay cursor must be non-negative.")
        operation = _Operation(binding=binding, high_water_cursor=replay_state.after_cursor)
        operation.seen_event_ids = {
            event_id: replay_state.after_cursor or 0 for event_id in replay_state.seen_event_ids
        }
        operation.cancellation_requested = replay_state.cancellation_requested
        operation.abort_acknowledged = replay_state.abort_acknowledged
        operation.termination_confirmed = replay_state.termination_confirmed
        self._operations[binding.operation_id] = operation

    def cancel(self, operation_id: str) -> CancellationStatus:
        operation = self._operation(operation_id)
        operation.cancellation_requested = True
        try:
            self._require_current_authority(
                operation.binding, BoundedPlanMessage("cancel", "synthetic cancellation request")
            )
        except AuthorityRejectedError:
            self._journal(operation.binding, "abort", "blocked")
            return self.cancellation_status(operation_id)
        try:
            operation.abort_acknowledged = (
                self._client.abort(operation.binding.session_id or "") is True
            )
        except Exception:
            # Delivery is uncertain, but cancellation must remain visibly unresolved.
            operation.abort_acknowledged = False
            self._journal(operation.binding, "abort", "uncertain")
        else:
            self._journal(
                operation.binding,
                "abort",
                "acknowledged" if operation.abort_acknowledged else "unacknowledged",
            )
        return self.cancellation_status(operation_id)

    def cancellation_status(self, operation_id: str) -> CancellationStatus:
        operation = self._operation(operation_id)
        return CancellationStatus(
            requested=operation.cancellation_requested,
            abort_acknowledged=operation.abort_acknowledged,
            termination_confirmed=operation.termination_confirmed,
            unresolved=operation.cancellation_requested and not operation.termination_confirmed,
        )

    def _operation(self, operation_id: str) -> _Operation:
        _identifier(operation_id, "operation_id")
        try:
            return self._operations[operation_id]
        except KeyError as error:
            raise OpenCodeHarnessError("Unknown operation.") from error

    def _quarantine(self, event: OpenCodeEvent, reason: str) -> None:
        self.quarantine.append(
            QuarantinedEvent(reason, event.session_id, event.event_id, event.cursor)
        )

    def _require_current_authority(
        self, binding: TesseraBinding, context: BoundedPlanMessage
    ) -> None:
        try:
            authorized = self._authority_is_current(binding, context)
        except Exception as error:
            raise AuthorityRejectedError("Authority check failed closed.") from error
        if authorized is not True:
            raise AuthorityRejectedError("Authority is expired or revised.")

    def _journal(self, binding: TesseraBinding, boundary: str, outcome: str) -> None:
        self.journal.append(
            UpstreamEffect(binding.operation_id, boundary, outcome, binding.session_id)
        )

    def _drain_contiguous_events(
        self, operation: _Operation, translated: list[AttributedEvent]
    ) -> None:
        assert operation.pending_events is not None
        expected = 0 if operation.high_water_cursor is None else operation.high_water_cursor + 1
        while (event := operation.pending_events.pop(expected, None)) is not None:
            payload = _bounded_payload(event.payload)
            operation.high_water_cursor = event.cursor
            assert operation.seen_event_ids is not None and operation.seen_cursors is not None
            operation.seen_event_ids[event.event_id] = event.cursor
            operation.seen_cursors[event.cursor] = event.event_id
            translated_event = AttributedEvent(
                binding=operation.binding,
                upstream_session_id=event.session_id,
                cursor=event.cursor,
                event_id=event.event_id,
                kind=event.kind,
                payload=payload,
            )
            translated.append(translated_event)
            self._observe_prompt(operation, translated_event)
            self._observe_terminal(operation, translated_event)
            expected += 1

    @staticmethod
    def _binding_mismatch(event: OpenCodeEvent, binding: TesseraBinding) -> str | None:
        for field in (
            "organization_id",
            "project_id",
            "task_id",
            "operation_id",
            "epoch",
            "harness_instance_id",
        ):
            if getattr(event, field) != getattr(binding, field):
                return field
        return None

    def _observe_prompt(self, operation: _Operation, event: AttributedEvent) -> None:
        if self._decision_callback is not None and event.kind in _PROMPT_KINDS:
            # The callback result is deliberately ignored: it cannot grant anything upstream.
            self._decision_callback(
                ScopedDecisionRequest(
                    binding=operation.binding,
                    upstream_session_id=event.upstream_session_id,
                    event_id=event.event_id,
                    cursor=event.cursor,
                    prompt_kind=event.kind,
                )
            )

    @staticmethod
    def _observe_terminal(operation: _Operation, event: AttributedEvent) -> None:
        if (
            operation.cancellation_requested
            and event.kind == "session.terminal"
            and event.payload.get("state") == "cancelled"
            and event.payload.get("authoritative") is True
        ):
            operation.termination_confirmed = True


def _bounded_payload(payload: Mapping[str, object]) -> dict[str, JSONValue]:
    safe = _safe_value(payload, depth=0)
    assert isinstance(safe, dict)
    encoded = json.dumps(safe, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_EVENT_BYTES:
        raise ValueError("event payload is too large")
    return safe


def _safe_value(value: object, *, depth: int) -> JSONValue:
    if depth > 2:
        raise ValueError("event payload nesting is too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 512:
            raise ValueError("event text is too large")
        return value
    if isinstance(value, Mapping):
        if len(value) > 16:
            raise ValueError("event object has too many fields")
        safe: dict[str, JSONValue] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not _IDENTIFIER.fullmatch(key)
                or _SECRET_KEY.search(key)
            ):
                raise ValueError("event payload has an unsafe key")
            safe[key] = _safe_value(item, depth=depth + 1)
        return safe
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        if len(value) > 16:
            raise ValueError("event list has too many values")
        return [_safe_value(item, depth=depth + 1) for item in value]
    raise ValueError("event payload has an unsupported value")
