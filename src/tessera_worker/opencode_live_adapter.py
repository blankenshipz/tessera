"""Live OpenCode session adapter behind the existing Tessera harness boundary.

The journal is local recovery evidence, not control authority.  It persists an
intent before each externally visible OpenCode call and keeps uncertainty rather
than creating a replacement session/message/abort request.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .opencode_harness import (
    AttributedEvent,
    AuthorityRejectedError,
    BoundedPlanMessage,
    CancellationStatus,
    OpenCodeHarnessError,
    QuarantinedEvent,
    TesseraBinding,
    _bounded_payload,
)
from .opencode_http import OpenCodeHttpClient, OpenCodeHttpError, OpenCodeSseEvent

_KNOWN_EVENT_TYPES = frozenset(
    {"session.created", "session.updated", "session.status", "session.idle", "session.error"}
)


class LiveOpenCodeHarnessAdapter:
    """Translate only pinned OpenCode sessions, messages, aborts, and SSE events.

    The callback is a supervisor-owned live authority recheck.  No callback
    result is ever sent to OpenCode as a permission, credential, provider, shell,
    MCP, or model instruction.
    """

    def __init__(
        self,
        client: OpenCodeHttpClient,
        journal_dir: Path,
        *,
        authority_is_current: Callable[[TesseraBinding, str], bool],
    ) -> None:
        if not isinstance(client, OpenCodeHttpClient):
            raise ValueError("Live adapter requires the narrow OpenCode HTTP client.")
        self._client = client
        self._authority_is_current = authority_is_current
        self._database = self._prepare_journal(Path(journal_dir))
        self._connection = sqlite3.connect(self._database)
        self._connection.execute("PRAGMA journal_mode=DELETE")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._initialize()
        self.quarantine: list[QuarantinedEvent] = []

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> LiveOpenCodeHarnessAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self, binding: TesseraBinding, context: BoundedPlanMessage) -> TesseraBinding:
        """Verify the pin, then create and message exactly one operation session."""
        if binding.session_id is not None:
            raise OpenCodeHarnessError("A new operation cannot supply a session identity.")
        if self._operation(binding.operation_id) is not None:
            raise OpenCodeHarnessError("Operation already has a local OpenCode journal.")
        self._require_instance_binding(binding)
        self._require_authority(binding, "inspect")
        try:
            self._client.verify_contract()
        except OpenCodeHttpError as error:
            raise OpenCodeHarnessError("Pinned OpenCode contract verification failed.") from error
        self._require_authority(binding, "create")
        self._insert_operation(binding)
        try:
            session_id = self._client.create_session(binding.operation_id)
        except OpenCodeHttpError as error:
            self._set_effect(binding.operation_id, "create", "uncertain")
            raise OpenCodeHarnessError(
                "OpenCode session creation is uncertain; reconcile it."
            ) from error
        bound = replace(binding, session_id=session_id)
        self._set_created(bound)
        try:
            self._require_authority(bound, "message")
        except AuthorityRejectedError:
            self._set_effect(bound.operation_id, "message", "blocked")
            raise
        self._set_effect(bound.operation_id, "message", "pending")
        try:
            self._client.send_message(session_id, context.message, bound.operation_id)
        except OpenCodeHttpError as error:
            self._set_effect(bound.operation_id, "message", "uncertain")
            raise OpenCodeHarnessError(
                "OpenCode message delivery is uncertain; reconcile it."
            ) from error
        self._set_effect(bound.operation_id, "message", "acknowledged")
        return bound

    def active_binding(self, operation_id: str) -> TesseraBinding:
        row = self._required_operation(operation_id)
        self._require_stored_instance_binding(row)
        return TesseraBinding(
            row[1],
            row[2],
            row[3],
            row[0],
            row[4],
            row[5],
            row[7],
            row[8],
            row[9],
            session_id=row[6],
        )

    def poll(self, operation_id: str) -> list[AttributedEvent]:
        binding = self.active_binding(operation_id)
        if binding.session_id is None:
            raise OpenCodeHarnessError("OpenCode session creation remains uncertain.")
        self._require_authority(binding, "events")
        try:
            # A restarted adapter has no trusted in-memory compatibility result.
            # Revalidate before each independently opened event stream.
            self._client.verify_contract()
        except OpenCodeHttpError as error:
            raise OpenCodeHarnessError("Pinned OpenCode contract verification failed.") from error
        self._require_authority(binding, "events")
        # Deliver durable, unacknowledged observations before opening another
        # stream.  A consumer persists these observations before acknowledging
        # their cursor, so a crash between delivery and that persistence only
        # causes a deduplicable redelivery.
        pending = self._drain_routed(binding)
        if pending:
            return pending
        after_cursor = self._stream_replay_cursor()
        try:
            events = self._client.events(after_cursor)
        except OpenCodeHttpError as error:
            raise OpenCodeHarnessError("OpenCode event stream is unavailable or unsafe.") from error
        for event in events:
            self._ingest_event(event)
        return self._drain_routed(binding)

    def reconnect(self, operation_id: str) -> list[AttributedEvent]:
        return self.poll(operation_id)

    def acknowledge_through(self, operation_id: str, cursor: int) -> None:
        """Advance replay only after downstream persistence has committed.

        The local stream-event journal permits downstream consumers to
        deduplicate a repeated delivery.  This acknowledgement is deliberately
        separate from ``poll``: a process crash before it cannot make the
        upstream cursor skip an observation that was not persisted downstream.
        """
        if type(cursor) is not int or cursor < 0:
            raise ValueError("OpenCode acknowledgement cursor must be a non-negative integer.")
        row = self._required_operation(operation_id)
        self._require_stored_instance_binding(row)
        high_water = row[13]
        if high_water is not None and cursor <= high_water:
            return
        stream_cursor = self._stream_replay_cursor()
        if stream_cursor is None or cursor > stream_cursor:
            raise OpenCodeHarnessError(
                "OpenCode replay acknowledgement must cover durable contiguous stream events."
            )
        acknowledged = self._connection.execute(
            """SELECT COUNT(*) FROM stream_events
            WHERE operation_id=? AND cursor>? AND cursor<=?""",
            (operation_id, -1 if high_water is None else high_water, cursor),
        ).fetchone()
        endpoint = self._connection.execute(
            "SELECT 1 FROM stream_events WHERE operation_id=? AND cursor=?",
            (operation_id, cursor),
        ).fetchone()
        if acknowledged is None or acknowledged[0] == 0 or endpoint is None:
            raise OpenCodeHarnessError(
                "OpenCode replay acknowledgement must cover durable routed events."
            )
        self._connection.execute(
            "UPDATE operations SET high_water_cursor=? WHERE operation_id=?",
            (cursor, operation_id),
        )
        self._connection.commit()

    def cancel(self, operation_id: str) -> CancellationStatus:
        binding = self.active_binding(operation_id)
        self._connection.execute(
            "UPDATE operations SET cancellation_requested=1 WHERE operation_id=?", (operation_id,)
        )
        self._connection.commit()
        if binding.session_id is None:
            return self.cancellation_status(operation_id)
        try:
            self._require_authority(binding, "abort")
        except AuthorityRejectedError:
            self._set_effect(operation_id, "abort", "blocked")
            return self.cancellation_status(operation_id)
        try:
            self._client.verify_contract()
        except OpenCodeHttpError:
            self._set_effect(operation_id, "abort", "uncertain")
            return self.cancellation_status(operation_id)
        try:
            self._require_authority(binding, "abort")
        except AuthorityRejectedError:
            self._set_effect(operation_id, "abort", "blocked")
            return self.cancellation_status(operation_id)
        self._set_effect(operation_id, "abort", "pending")
        try:
            self._client.abort(binding.session_id, operation_id)
        except OpenCodeHttpError:
            self._set_effect(operation_id, "abort", "uncertain")
        else:
            self._connection.execute(
                "UPDATE operations SET abort_acknowledged=1 WHERE operation_id=?", (operation_id,)
            )
            self._connection.commit()
            self._set_effect(operation_id, "abort", "acknowledged")
        return self.cancellation_status(operation_id)

    def cancellation_status(self, operation_id: str) -> CancellationStatus:
        row = self._required_operation(operation_id)
        requested, acknowledged = bool(row[14]), bool(row[15])
        # An OpenCode abort/session event cannot prove provider termination.
        return CancellationStatus(requested, acknowledged, False, requested)

    def effect_outcome(self, operation_id: str, boundary: str) -> str:
        if boundary not in {"create", "message", "abort"}:
            raise ValueError("Unknown OpenCode effect boundary.")
        row = self._required_operation(operation_id)
        return row[{"create": 10, "message": 11, "abort": 12}[boundary]]

    def _ingest_event(self, event: OpenCodeSseEvent) -> None:
        """Persist a shared-stream event before assigning it to its session.

        OpenCode cursor IDs identify the stream, not an individual session.  A
        known other session therefore cannot become an unrecoverable gap for the
        operation that happened to poll the stream.
        """
        fingerprint = event.fingerprint()
        known = self._connection.execute(
            "SELECT fingerprint FROM stream_ingestion WHERE cursor=?", (event.cursor,)
        ).fetchone()
        if known is not None:
            if known[0] != fingerprint:
                self._quarantine(event, "cursor reused by another event")
            return
        known_fingerprint = self._connection.execute(
            "SELECT cursor FROM stream_ingestion WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if known_fingerprint is not None:
            self._quarantine(event, "event identity reused at another cursor")
            return
        try:
            kind, session_id, payload = self._translate_event(event)
        except ValueError:
            self._quarantine(event, "unsafe or malformed event")
            return
        if kind not in _KNOWN_EVENT_TYPES:
            self._quarantine(event, "unknown event kind")
            return
        operations = self._connection.execute(
            "SELECT operation_id, high_water_cursor FROM operations WHERE session_id=?",
            (session_id,),
        ).fetchall()
        if not operations:
            self._quarantine(event, "unknown or foreign session")
            return
        self._record_ingestion(event, fingerprint, "routed")
        for operation_id, high_water in operations:
            if high_water is not None and event.cursor <= high_water:
                self._quarantine(event, "delayed event before replay cursor")
                continue
            self._connection.execute(
                """INSERT INTO stream_events
                (operation_id, cursor, fingerprint, kind, session_id, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    operation_id,
                    event.cursor,
                    fingerprint,
                    kind,
                    session_id,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                ),
            )
        self._connection.commit()

    def _record_ingestion(self, event: OpenCodeSseEvent, fingerprint: str, outcome: str) -> None:
        self._connection.execute(
            "INSERT INTO stream_ingestion (cursor, fingerprint, outcome) VALUES (?, ?, ?)",
            (event.cursor, fingerprint, outcome),
        )
        self._advance_stream_replay_cursor()

    def _advance_stream_replay_cursor(self) -> None:
        cursor = self._stream_replay_cursor()
        expected = 0 if cursor is None else cursor + 1
        while (
            self._connection.execute(
                "SELECT 1 FROM stream_ingestion WHERE cursor=?", (expected,)
            ).fetchone()
            is not None
        ):
            self._connection.execute(
                "UPDATE stream_state SET replay_cursor=? WHERE singleton=1", (expected,)
            )
            expected += 1

    def _stream_replay_cursor(self) -> int | None:
        row = self._connection.execute(
            "SELECT replay_cursor FROM stream_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise OpenCodeHarnessError("OpenCode stream journal is unavailable.")
        return row[0]

    @staticmethod
    def _translate_event(event: OpenCodeSseEvent) -> tuple[str, str, dict[str, object]]:
        # The pinned OpenCode `/event` schema uses a typed envelope.  We retain
        # only the schema event type and properties, never raw event text.
        if event.event_name != "message":
            raise ValueError("unsupported SSE event name")
        kind = event.data.get("type")
        properties = event.data.get("properties")
        if not isinstance(kind, str) or not isinstance(properties, dict):
            raise ValueError("malformed event envelope")
        session_id = properties.get("sessionID")
        if not isinstance(session_id, str):
            raise ValueError("event lacks a session identity")
        payload = _bounded_payload(properties)
        return kind, session_id, payload

    def _drain_routed(self, binding: TesseraBinding) -> list[AttributedEvent]:
        row = self._required_operation(binding.operation_id)
        stream_cursor = self._stream_replay_cursor()
        if stream_cursor is None:
            return []
        translated: list[AttributedEvent] = []
        events = self._connection.execute(
            """SELECT cursor, fingerprint, kind, session_id, payload_json FROM stream_events
            WHERE operation_id=? AND cursor>? AND cursor<=? ORDER BY cursor""",
            (binding.operation_id, -1 if row[13] is None else row[13], stream_cursor),
        ).fetchall()
        for event in events:
            payload = json.loads(event[4])
            translated.append(
                AttributedEvent(
                    binding=binding,
                    upstream_session_id=event[3],
                    cursor=event[0],
                    event_id=event[1],
                    kind=event[2],
                    payload=payload,
                )
            )
        return translated

    def _quarantine(self, event: OpenCodeSseEvent, reason: str) -> None:
        self.quarantine.append(
            QuarantinedEvent(reason, "untrusted", event.fingerprint(), event.cursor)
        )

    def _require_authority(self, binding: TesseraBinding, action: str) -> None:
        try:
            allowed = self._authority_is_current(binding, action)
        except Exception as error:
            raise AuthorityRejectedError("Authority check failed closed.") from error
        if allowed is not True:
            raise AuthorityRejectedError("Authority is expired or revised.")

    def _require_instance_binding(self, binding: TesseraBinding) -> None:
        if binding.harness_instance_id != self._client.harness_instance.harness_instance_id:
            raise OpenCodeHarnessError(
                "Operation harness instance does not match the approved endpoint."
            )

    def _require_stored_instance_binding(self, row: tuple[object, ...]) -> None:
        instance = self._client.harness_instance
        expected = (
            instance.harness_instance_id,
            self._client.origin,
            instance.server_identity,
            instance.server_incarnation,
            instance.build_sha256,
            self._client.pin.server_version,
            self._client.pin.schema_sha256,
            instance.config_sha256,
            instance.workspace_scope,
        )
        if (row[5], *row[16:24]) != expected:
            raise OpenCodeHarnessError(
                "OpenCode harness instance changed; restart reconciliation is blocked."
            )

    def _insert_operation(self, binding: TesseraBinding) -> None:
        self._connection.execute(
            """INSERT INTO operations
            (operation_id, organization_id, project_id, task_id, epoch,
              harness_instance_id, attempt_id, operation_authority_ref,
              attempt_authority_ref, endpoint, server_identity, server_incarnation,
              build_sha256, server_version, schema_sha256, config_sha256,
              workspace_scope, create_outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                binding.operation_id,
                binding.organization_id,
                binding.project_id,
                binding.task_id,
                binding.epoch,
                binding.harness_instance_id,
                binding.attempt_id,
                binding.operation_authority_ref,
                binding.attempt_authority_ref,
                self._client.origin,
                self._client.harness_instance.server_identity,
                self._client.harness_instance.server_incarnation,
                self._client.harness_instance.build_sha256,
                self._client.pin.server_version,
                self._client.pin.schema_sha256,
                self._client.harness_instance.config_sha256,
                self._client.harness_instance.workspace_scope,
            ),
        )
        self._connection.commit()

    def _set_created(self, binding: TesseraBinding) -> None:
        try:
            self._connection.execute(
                """UPDATE operations SET session_id=?, create_outcome='acknowledged'
                WHERE operation_id=?""",
                (binding.session_id, binding.operation_id),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            self._set_effect(binding.operation_id, "create", "uncertain")
            raise OpenCodeHarnessError(
                "OpenCode session identity conflicts with another operation; reconcile it."
            ) from error

    def _set_effect(self, operation_id: str, boundary: str, outcome: str) -> None:
        try:
            column = {
                "create": "create_outcome",
                "message": "message_outcome",
                "abort": "abort_outcome",
            }[boundary]
        except KeyError as error:
            raise ValueError("Unknown OpenCode effect boundary.") from error
        self._connection.execute(
            f"UPDATE operations SET {column}=? WHERE operation_id=?",
            (outcome, operation_id),
        )
        self._connection.commit()

    def _operation(self, operation_id: str) -> tuple[object, ...] | None:
        return self._connection.execute(
            """SELECT operation_id, organization_id, project_id, task_id, epoch,
            harness_instance_id, session_id, attempt_id, operation_authority_ref,
            attempt_authority_ref, create_outcome, message_outcome, abort_outcome,
            high_water_cursor, cancellation_requested, abort_acknowledged, endpoint,
            server_identity, server_incarnation, build_sha256, server_version,
            schema_sha256, config_sha256, workspace_scope
            FROM operations WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()

    def _required_operation(self, operation_id: str) -> tuple[object, ...]:
        row = self._operation(operation_id)
        if row is None:
            raise OpenCodeHarnessError("Unknown operation.")
        return row

    def _initialize(self) -> None:
        self._connection.executescript(
            """CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                harness_instance_id TEXT NOT NULL,
                session_id TEXT,
                attempt_id TEXT NOT NULL,
                operation_authority_ref TEXT NOT NULL,
                attempt_authority_ref TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                server_identity TEXT NOT NULL,
                server_incarnation TEXT NOT NULL,
                build_sha256 TEXT NOT NULL,
                server_version TEXT NOT NULL,
                schema_sha256 TEXT NOT NULL,
                config_sha256 TEXT NOT NULL,
                workspace_scope TEXT NOT NULL,
                create_outcome TEXT NOT NULL,
                message_outcome TEXT NOT NULL DEFAULT 'none',
                abort_outcome TEXT NOT NULL DEFAULT 'none',
                high_water_cursor INTEGER,
                cancellation_requested INTEGER NOT NULL DEFAULT 0,
                abort_acknowledged INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS stream_events (
                operation_id TEXT NOT NULL,
                cursor INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL,
                session_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (operation_id, cursor),
                UNIQUE (operation_id, fingerprint));
            CREATE UNIQUE INDEX IF NOT EXISTS operations_by_session
                ON operations(session_id) WHERE session_id IS NOT NULL;"""
        )
        self._connection.executescript(
            """CREATE TABLE IF NOT EXISTS stream_ingestion (
                cursor INTEGER PRIMARY KEY,
                fingerprint TEXT NOT NULL UNIQUE,
                outcome TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stream_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                replay_cursor INTEGER);
            INSERT OR IGNORE INTO stream_state (singleton, replay_cursor) VALUES (1, NULL);"""
        )
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(operations)").fetchall()
        }
        required_columns = {
            "attempt_id",
            "operation_authority_ref",
            "attempt_authority_ref",
            "endpoint",
            "server_identity",
            "server_incarnation",
            "build_sha256",
            "server_version",
            "schema_sha256",
            "config_sha256",
            "workspace_scope",
        }
        if not required_columns <= columns:
            raise ValueError(
                "OpenCode live journal lacks immutable harness binding; it cannot be resumed."
            )
        self._connection.commit()

    @staticmethod
    def _prepare_journal(directory: Path) -> Path:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or stat.S_IMODE(directory.stat().st_mode) != 0o700
        ):
            raise ValueError("OpenCode live journal directory must be private (0700).")
        database = directory / "opencode-live.sqlite3"
        if database.exists():
            if (
                database.is_symlink()
                or not database.is_file()
                or stat.S_IMODE(database.stat().st_mode) != 0o600
            ):
                raise ValueError("OpenCode live journal must be a private regular file.")
        else:
            descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        return database
