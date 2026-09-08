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


@dataclass
class _Operation:
    binding: TesseraBinding
    high_water_cursor: int | None = None
    seen_event_ids: set[str] | None = None
    seen_cursors: set[int] | None = None
    cancellation_requested: bool = False
    abort_acknowledged: bool = False
    termination_confirmed: bool = False

    def __post_init__(self) -> None:
        self.seen_event_ids = set()
        self.seen_cursors = set()


class OpenCodeHarnessAdapter:
    """Translate synthetic OpenCode fixture calls without changing Tessera authority."""

    def __init__(
        self,
        client: OpenCodeClient,
        *,
        pinned_version: str,
        authority_is_current: Callable[[TesseraBinding, BoundedPlanMessage], bool],
        decision_callback: Callable[[ScopedDecisionRequest], object] | None = None,
    ) -> None:
        if not isinstance(pinned_version, str) or not _VERSION.fullmatch(pinned_version):
            raise ValueError("pinned_version must be an exact semantic version.")
        self._client = client
        self._pinned_version = pinned_version
        self._authority_is_current = authority_is_current
        self._decision_callback = decision_callback
        self._operations: dict[str, _Operation] = {}
        self.quarantine: list[QuarantinedEvent] = []

    def start(self, binding: TesseraBinding, context: BoundedPlanMessage) -> TesseraBinding:
        """Create one deterministic fixture session after version and authority checks."""
        if binding.session_id is not None:
            raise OpenCodeHarnessError("A new operation cannot supply a session identity.")
        if binding.operation_id in self._operations:
            raise OpenCodeHarnessError("Operation already has a synthetic session.")
        try:
            authorized = self._authority_is_current(binding, context)
        except Exception as error:
            raise AuthorityRejectedError("Authority check failed closed.") from error
        if authorized is not True:
            raise AuthorityRejectedError("Authority is expired or revised.")
        reported_version = self._client.version()
        if not isinstance(reported_version, str) or reported_version != self._pinned_version:
            raise VersionCompatibilityError(
                "OpenCode version is missing or incompatible with the pin."
            )

        session_id = self._client.create_session(
            {
                "fixture": SYNTHETIC_SESSION_FIXTURE,
                "operation_id": binding.operation_id,
            }
        )
        _identifier(session_id, "session_id")
        bound = replace(binding, session_id=session_id)
        # Preserve the identity if send_message fails: its external effect is not retried.
        self._operations[binding.operation_id] = _Operation(binding=bound)
        self._client.send_message(
            session_id,
            {
                "parts": [{"type": "text", "text": context.message}],
                "context": {"plan_revision": context.plan_revision},
            },
        )
        return bound

    def active_binding(self, operation_id: str) -> TesseraBinding:
        _identifier(operation_id, "operation_id")
        return self._operation(operation_id).binding

    def poll(self, operation_id: str) -> list[AttributedEvent]:
        """Read from the operation high-water cursor without creating a session."""
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
            assert operation.seen_event_ids is not None and operation.seen_cursors is not None
            if event.event_id in operation.seen_event_ids or event.cursor in operation.seen_cursors:
                continue
            try:
                payload = _bounded_payload(event.payload)
            except ValueError:
                self._quarantine(event, "unsafe or oversized event payload")
                continue
            operation.seen_event_ids.add(event.event_id)
            operation.seen_cursors.add(event.cursor)
            if operation.high_water_cursor is None or event.cursor > operation.high_water_cursor:
                operation.high_water_cursor = event.cursor
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
        return translated

    def reconnect(self, operation_id: str) -> list[AttributedEvent]:
        """Resume the existing operation's stream from its last accepted cursor."""
        return self.poll(operation_id)

    def cancel(self, operation_id: str) -> CancellationStatus:
        operation = self._operation(operation_id)
        operation.cancellation_requested = True
        try:
            operation.abort_acknowledged = (
                self._client.abort(operation.binding.session_id or "") is True
            )
        except Exception:
            # Delivery is uncertain, but cancellation must remain visibly unresolved.
            operation.abort_acknowledged = False
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
