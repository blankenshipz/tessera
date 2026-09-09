"""Narrow fake-provider dispatch seam used by the development worker only.

This deliberately models a bounded provider receipt, not execution success,
accounting, artifact acceptance, or a real provider integration.  The fake is
injected by tests/fixtures and has no network, credential, or cloud path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from .cpu import CpuOperation
from .supervisor import Lease, SupervisorError

ProviderState = Literal["unknown", "running", "exited"]


class ProviderError(SupervisorError):
    """A provider result is unavailable and must be reconciled, not replayed."""


class ProviderReportError(ProviderError):
    """A durable provider receipt could not be delivered to control."""

    reporting_failure = True


@dataclass(frozen=True)
class ProviderReceipt:
    provider_kind: Literal["fake"]
    provider_version: str
    operation_id: str
    provider_id: str
    lookup_key: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ProviderObservation:
    state: ProviderState
    provider_id: str | None
    receipt: ProviderReceipt | None = None
    termination_confirmed: bool = False


class Provider(Protocol):
    """The only provider contract admitted in this vertical slice."""

    def submit(self, operation_id: str, lookup_key: str) -> ProviderReceipt: ...

    def observe(self, lookup_key: str, provider_id: str | None = None) -> ProviderObservation: ...

    def cancel(self, lookup_key: str, provider_id: str | None = None) -> ProviderObservation: ...


class FakeProvider:
    """Deterministic injected provider with controllable lost replies.

    Submission is deduplicated by the stable operation identity.  A configured
    lost reply still creates the fake provider operation, so recovery can only
    observe its lookup key; it cannot submit again.
    """

    kind = "fake"
    version = "fake-provider-v1"

    def __init__(self) -> None:
        self.operations: dict[str, ProviderReceipt] = {}
        self.states: dict[str, ProviderState] = {}
        self.submit_calls: list[str] = []
        self.observe_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.lose_submit_reply = False
        self.lose_observe_reply = False
        self.lose_cancel_reply = False
        self.cancel_delayed = False

    def submit(self, operation_id: str, lookup_key: str) -> ProviderReceipt:
        self.submit_calls.append(operation_id)
        receipt = self.operations.get(lookup_key)
        if receipt is None:
            provider_id = "fake-" + operation_id
            payload = {
                "lookup_key": lookup_key,
                "operation_id": operation_id,
                "provider_id": provider_id,
                "provider_kind": self.kind,
                "provider_version": self.version,
            }
            receipt = ProviderReceipt(
                self.kind, self.version, operation_id, provider_id, lookup_key, payload
            )
            self.operations[lookup_key] = receipt
            self.states[lookup_key] = "running"
        if self.lose_submit_reply:
            raise ProviderError("Fake provider submit reply was lost.")
        return receipt

    def observe(self, lookup_key: str, provider_id: str | None = None) -> ProviderObservation:
        self.observe_calls.append(lookup_key)
        if self.lose_observe_reply:
            raise ProviderError("Fake provider observation reply was lost.")
        receipt = self.operations.get(lookup_key)
        if receipt is None or (provider_id is not None and provider_id != receipt.provider_id):
            return ProviderObservation("unknown", None)
        state = self.states[lookup_key]
        return ProviderObservation(state, receipt.provider_id, receipt, state == "exited")

    def cancel(self, lookup_key: str, provider_id: str | None = None) -> ProviderObservation:
        self.cancel_calls.append(lookup_key)
        if self.lose_cancel_reply:
            raise ProviderError("Fake provider cancel reply was lost.")
        receipt = self.operations.get(lookup_key)
        if receipt is None or (provider_id is not None and provider_id != receipt.provider_id):
            return ProviderObservation("unknown", None)
        if not self.cancel_delayed:
            self.states[lookup_key] = "exited"
        return self.observe(lookup_key, receipt.provider_id)

    def complete(self, lookup_key: str) -> None:
        """Test-only deterministic completion; it does not execute user work."""
        if lookup_key in self.operations:
            self.states[lookup_key] = "exited"


class ProviderDispatch:
    """Persist fake submit intent/receipt before and after the provider side effect."""

    def __init__(self, directory: Path, channel: Any, provider: Provider) -> None:
        self.path = Path(directory) / "provider-dispatch.sqlite3"
        self.channel = channel
        self.provider = provider
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS dispatches (
                operation_id TEXT PRIMARY KEY, lookup_key TEXT NOT NULL UNIQUE,
                lease_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                submit_intent INTEGER NOT NULL CHECK (submit_intent IN (0, 1)),
                receipt_json TEXT, receipt_key TEXT NOT NULL, receipt_delivered INTEGER NOT NULL,
                report_payload_json TEXT, report_sequence INTEGER NOT NULL DEFAULT 0
                    CHECK (receipt_delivered IN (0, 1)))"""
            )

    def before_start(self, operation: CpuOperation, lease: Lease) -> None:
        """Submit once after supervisor start intent/recheck, then persist receipt."""
        row = self._bind(operation, lease)
        receipt = self._receipt(row)
        if receipt is None:
            if row["submit_intent"]:
                observation = self.provider.observe(row["lookup_key"])
                if observation.receipt is None:
                    raise ProviderError("Fake provider submit remains unresolved.")
                receipt = observation.receipt
            else:
                self._mark_submit_intent(operation.operation_id)
                receipt = self.provider.submit(operation.operation_id, row["lookup_key"])
            self._store_receipt(operation.operation_id, receipt)
        self._deliver(operation.operation_id, receipt, "running", False)

    def cancel(self, operation: CpuOperation, lease: Lease) -> None:
        """Dispatch cancellation only from the worker; control revocation is inert."""
        row = self._bind(operation, lease)
        receipt = self._receipt(row)
        if receipt is None:
            # A cancel racing a lost submit has no authority to submit.  Observe
            # by lookup key and leave an unknown result visible if it is absent.
            observation = self.provider.observe(row["lookup_key"])
            if observation.receipt is None:
                raise ProviderError("Fake provider cancellation is unresolved.")
            receipt = observation.receipt
            self._store_receipt(operation.operation_id, receipt)
        observation = self.provider.cancel(receipt.lookup_key, receipt.provider_id)
        self._deliver(
            operation.operation_id,
            receipt,
            observation.state,
            observation.termination_confirmed,
        )

    def reconcile(self, operation: CpuOperation, lease: Lease) -> ProviderObservation:
        row = self._bind(operation, lease)
        receipt = self._receipt(row)
        observation = self.provider.observe(
            row["lookup_key"], receipt.provider_id if receipt else None
        )
        if observation.receipt is not None:
            self._store_receipt(operation.operation_id, observation.receipt)
            self._deliver(
                operation.operation_id,
                observation.receipt,
                observation.state,
                observation.termination_confirmed,
            )
        return observation

    def reconcile_stable(self, operation: CpuOperation) -> None:
        """Observe an uncertain submit using only the already persisted lookup key."""
        row = self._row(operation.operation_id)
        receipt = self._receipt(row)
        observation = self.provider.observe(
            row["lookup_key"], receipt.provider_id if receipt else None
        )
        if observation.receipt is None:
            raise ProviderError("Fake provider observation remains unresolved.")
        self._store_receipt(operation.operation_id, observation.receipt)
        self._deliver(
            operation.operation_id,
            observation.receipt,
            observation.state,
            observation.termination_confirmed,
        )

    def cancel_stable(self, operation: CpuOperation) -> None:
        row = self._row(operation.operation_id)
        receipt = self._receipt(row)
        if receipt is None:
            observation = self.provider.observe(row["lookup_key"])
            if observation.receipt is None:
                raise ProviderError("Fake provider cancellation is unresolved.")
            receipt = observation.receipt
            self._store_receipt(operation.operation_id, receipt)
        observation = self.provider.cancel(receipt.lookup_key, receipt.provider_id)
        self._deliver(
            operation.operation_id,
            receipt,
            observation.state,
            observation.termination_confirmed,
        )

    def complete_stable(self, operation: CpuOperation) -> None:
        """Advance the deterministic fake only for a synthetic observed exit."""
        row = self._row(operation.operation_id)
        complete = getattr(self.provider, "complete", None)
        if complete is None:
            raise ProviderError("Injected provider cannot reconcile a synthetic exit.")
        complete(row["lookup_key"])
        self.reconcile_stable(operation)

    def provider_id(self, operation: CpuOperation) -> str | None:
        receipt = self._receipt(self._row(operation.operation_id))
        return receipt.provider_id if receipt is not None else None

    def _bind(self, operation: CpuOperation, lease: Lease) -> sqlite3.Row:
        lookup_key = operation.container_name
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dispatches WHERE operation_id=?", (operation.operation_id,)
            ).fetchone()
            if row is None:
                key = "fake-receipt-" + hashlib.sha256(operation.operation_id.encode()).hexdigest()
                connection.execute(
                    """INSERT INTO dispatches
                    (operation_id, lookup_key, lease_id, epoch, submit_intent, receipt_json,
                     receipt_key, receipt_delivered, report_payload_json, report_sequence)
                    VALUES (?, ?, ?, ?, 0, NULL, ?, 0, NULL, 0)""",
                    (operation.operation_id, lookup_key, lease.id, lease.epoch, key),
                )
                row = connection.execute(
                    "SELECT * FROM dispatches WHERE operation_id=?", (operation.operation_id,)
                ).fetchone()
            if (row["lookup_key"], row["lease_id"], row["epoch"]) != (
                lookup_key,
                lease.id,
                lease.epoch,
            ):
                raise SupervisorError("Provider dispatch identity or authority conflicts.")
            return row

    @staticmethod
    def _decode(encoded: str) -> ProviderReceipt:
        value = json.loads(encoded)
        return ProviderReceipt(**value)

    def _receipt(self, row: sqlite3.Row) -> ProviderReceipt | None:
        return self._decode(row["receipt_json"]) if row["receipt_json"] is not None else None

    def _mark_submit_intent(self, operation_id: str) -> None:
        with self._connect() as connection:
            if (
                connection.execute(
                    "UPDATE dispatches SET submit_intent=1 "
                    "WHERE operation_id=? AND submit_intent=0",
                    (operation_id,),
                ).rowcount
                != 1
            ):
                raise ProviderError("Fake provider submit intent is already fenced.")

    def _store_receipt(self, operation_id: str, receipt: ProviderReceipt) -> None:
        encoded = json.dumps(receipt.__dict__, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM dispatches WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise SupervisorError("Provider dispatch disappeared.")
            if row["receipt_json"] not in {None, encoded}:
                raise SupervisorError("Provider receipt identity conflicts.")
            connection.execute(
                "UPDATE dispatches SET receipt_json=? WHERE operation_id=?", (encoded, operation_id)
            )

    def _deliver(
        self, operation_id: str, receipt: ProviderReceipt, state: ProviderState, confirmed: bool
    ) -> None:
        payload = {
            "lease_id": self._lease(operation_id),
            "expected_epoch": self._epoch(operation_id),
            "provider_kind": receipt.provider_kind,
            "provider_version": receipt.provider_version,
            "provider_id": receipt.provider_id,
            "receipt_payload": receipt.payload,
            "receipt_digest": hashlib.sha256(
                json.dumps(receipt.payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "observed_state": state,
            "observed_at": datetime.now(UTC).isoformat(),
            "termination_confirmed": confirmed,
        }
        row = self._row(operation_id)
        if row["receipt_delivered"] == 0 and row["report_payload_json"] is not None:
            payload = json.loads(row["report_payload_json"])
        else:
            sequence = row["report_sequence"] + 1
            key = (
                "fake-receipt-" + hashlib.sha256(f"{operation_id}:{sequence}".encode()).hexdigest()
            )
            with self._connect() as connection:
                connection.execute(
                    """UPDATE dispatches SET receipt_key=?, receipt_delivered=0,
                    report_payload_json=?, report_sequence=? WHERE operation_id=?""",
                    (
                        key,
                        json.dumps(payload, sort_keys=True, separators=(",", ":")),
                        sequence,
                        operation_id,
                    ),
                )
            row = self._row(operation_id)
        try:
            self.channel.post(
                f"/worker/external-operations/{operation_id}/receipt", payload, row["receipt_key"]
            )
        except Exception as error:
            raise ProviderReportError("Fake provider receipt report is unresolved.") from error
        with self._connect() as connection:
            connection.execute(
                "UPDATE dispatches SET receipt_delivered=1 WHERE operation_id=?", (operation_id,)
            )

    def _row(self, operation_id: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dispatches WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise SupervisorError("Provider dispatch disappeared.")
        return row

    def _lease(self, operation_id: str) -> str:
        return self._row(operation_id)["lease_id"]

    def _epoch(self, operation_id: str) -> int:
        return self._row(operation_id)["epoch"]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


__all__ = [
    "FakeProvider",
    "Provider",
    "ProviderDispatch",
    "ProviderError",
    "ProviderReportError",
    "ProviderObservation",
    "ProviderReceipt",
]
