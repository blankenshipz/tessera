"""Deliver definitive termination evidence for a grant the worker stopped driving (ADR 0160).

The development poller records a ``result.json`` (or ``abandoned.json``) once its one-shot
service ends a grant, and never drives that grant again. When the service ended without a
confirmed exit (a renewal or report failure, an unconfirmed prepare or cancel), control kept
an unknown attempt and an unresolved grant: nothing ever told it that the container later
exited or disappeared, and ``/execution-grants/{id}/reconcile`` and the campaign evidence
paths refuse without that.

``TerminationEvidenceSweep`` closes that gap for local Docker only. For each such grant it
reads the durable runtime descriptor (grant, lease, epoch, operation and plan identity) and the
local observation journal (the container identities this lease reported), inspects the exact
container, and reports through the ordinary observation route, as the worker that held the
lease:

* the container exists, carries this operation's labels, and has exited (exit code known):
  a confirmed exit with that code, exactly as the live service would have reported it;
* the container identity control already accepted from this lease is gone (Docker answers
  "no such container" for that id and for the operation's container name): a confirmed exit
  with ``termination_evidence: container_absent``; control re-checks the identity and that the
  lease ended (ADR 0160).

Every doubt holds: no recorded or delivered identity (``container_identity_unrecorded``), two
identities or a container whose labels or id differ (``container_identity_conflict``,
``container_identity_mismatch``), a container that is running, created, paused or restarting
(``container_may_run``), or Docker not answering (``docker_unavailable``). Nothing is started,
killed or removed here. The reason is written to a local ``termination-evidence.json`` marker
(ids, states and times only); final reasons are not re-checked, the others are retried with
backoff.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .control_channel import ControlChannelError, classify_control_failure
from .cpu import CpuError
from .daemon import backoff_delay

SCHEMA = "tessera.worker.termination-evidence.v1"
MARKER = "termination-evidence.json"
INTENT = "termination-intent.json"
OBSERVATION_PATH = "/worker/observations"
CONTAINER_ABSENT = "container_absent"
MAX_PER_SWEEP = 8
BACKOFF_MAX_SECONDS = 900.0
MAX_DESCRIPTOR_BYTES = 4 * 1024 * 1024
# Held reasons that a later look cannot change: the grant is not re-checked.
FINAL_REASONS = frozenset(
    {
        "container_identity_unrecorded",
        "container_identity_conflict",
        "container_identity_mismatch",
        "descriptor_unreadable",
    }
)
_NO_SUCH = ("No such container", "No such object")

RunDocker = Callable[[Sequence[str]], Any]


@dataclass(frozen=True)
class Identity:
    lease_id: str
    epoch: int
    operation_id: str
    plan_digest: str

    @property
    def container_name(self) -> str:
        return f"tessera-op-{self.operation_id}"


@dataclass(frozen=True)
class Finding:
    """``exited``, ``absent`` or ``held`` (with a reason); never inferred beyond Docker's reply."""

    kind: str
    reason: str
    provider_id: str | None = None
    exit_code: int | None = None


def read_identity(runtime_dir: Path) -> Identity | None:
    """The lease and operation identity the runtime descriptor fixed, else None."""
    path = runtime_dir / "runtime.json"
    try:
        descriptor_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor_fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return None
            encoded = source.read(MAX_DESCRIPTOR_BYTES + 1)
        if len(encoded) > MAX_DESCRIPTOR_BYTES:
            return None
        package = json.loads(encoded)["control"]["package"]
        identity = Identity(
            lease_id=package["lease_id"],
            epoch=package["epoch"],
            operation_id=package["operation_id"],
            plan_digest=package["plan_digest"],
        )
    except (KeyError, OSError, TypeError, UnicodeDecodeError, ValueError):
        return None
    if (
        not isinstance(identity.lease_id, str)
        or not 1 <= len(identity.lease_id) <= 200
        or type(identity.epoch) is not int
        or identity.epoch < 1
        or not isinstance(identity.operation_id, str)
        or len(identity.operation_id) != 32
        or not isinstance(identity.plan_digest, str)
        or len(identity.plan_digest) != 64
    ):
        return None
    return identity


@dataclass(frozen=True)
class Journal:
    """What the local observation journal says this operation reported."""

    reported_ids: frozenset[str]
    delivered_ids: frozenset[str]
    exit_delivered: bool


def read_journal(runtime_dir: Path, operation_id: str) -> Journal:
    path = runtime_dir / "runtime-observations.sqlite3"
    if not path.is_file() or path.is_symlink():
        return Journal(frozenset(), frozenset(), False)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """SELECT provider_id, completed, observed_state, termination_confirmed
            FROM reports WHERE operation_id=?""",
            (operation_id,),
        ).fetchall()
    finally:
        connection.close()
    reported = frozenset(row[0] for row in rows if row[0])
    delivered = frozenset(row[0] for row in rows if row[0] and row[1] == 1)
    exit_delivered = any(row[1] == 1 and row[2] == "exited" and row[3] == 1 for row in rows)
    return Journal(reported, delivered, exit_delivered)


def _inspect(run: RunDocker, reference: str, identity: Identity) -> tuple[str, dict | None]:
    """``absent``, ``found`` (with the inspect record), ``mismatch`` or ``unavailable``."""
    try:
        reply = run(["inspect", "--type", "container", reference])
    except CpuError:
        return "unavailable", None
    if reply.returncode:
        stderr = reply.stderr or ""
        # A stopped daemon or an authorization failure is never absence.
        return (
            ("absent", None)
            if any(text in stderr for text in _NO_SUCH)
            else (
                "unavailable",
                None,
            )
        )
    try:
        items = json.loads(reply.stdout)
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            return "unavailable", None
        info = items[0]
        labels = info["Config"]["Labels"] or {}
        if not isinstance(info["Id"], str) or not isinstance(info["State"], dict):
            return "unavailable", None
    except (KeyError, TypeError, ValueError):
        return "unavailable", None
    if (
        labels.get("tessera.operation") != identity.operation_id
        or labels.get("tessera.plan") != identity.plan_digest
    ):
        return "mismatch", info
    return "found", info


def assess(run: RunDocker, identity: Identity, journal: Journal) -> Finding:
    """Classify the container from Docker's own reply; any doubt is a held finding."""
    if len(journal.reported_ids) > 1:
        return Finding("held", "container_identity_conflict")
    recorded = next(iter(journal.reported_ids), None)
    replies = []
    if recorded is not None:
        replies.append(_inspect(run, recorded, identity))
    replies.append(_inspect(run, identity.container_name, identity))
    if any(status == "unavailable" for status, _ in replies):
        return Finding("held", "docker_unavailable")
    if any(status == "mismatch" for status, _ in replies):
        return Finding("held", "container_identity_mismatch")
    found = [info for status, info in replies if status == "found"]
    ids = {info["Id"] for info in found if info is not None}
    if len(ids) > 1 or (recorded is not None and ids and ids != {recorded}):
        return Finding("held", "container_identity_conflict")
    if found:
        info = found[0]
        assert info is not None
        state = info["State"]
        exit_code = state.get("ExitCode")
        if (
            state.get("Status") in {"exited", "dead"}
            and state.get("Running") is False
            and state.get("Restarting") in {None, False}
            and state.get("Paused") in {None, False}
            and type(exit_code) is int
        ):
            return Finding("exited", "container_exited", info["Id"], exit_code)
        return Finding("held", "container_may_run", info["Id"])
    # Docker answered "no such container" for every reference.
    if recorded is None or recorded not in journal.delivered_ids:
        # Never recorded, or never accepted by control: absence proves nothing about which
        # container ran, so it is not termination evidence.
        return Finding("held", "container_identity_unrecorded")
    return Finding("absent", CONTAINER_ABSENT, recorded)


def _write_private(directory: Path, name: str, document: dict[str, Any]) -> None:
    temporary = directory / f"{name}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as target:
        json.dump(document, target, sort_keys=True)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, directory / name)


def _read_private(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink():
            return None
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def marker_state(grant_dir: Path) -> dict[str, Any] | None:
    return _read_private(grant_dir / MARKER)


def settled(grant_dir: Path) -> bool:
    """True once evidence was delivered or a final reason holds the grant."""
    marker = marker_state(grant_dir)
    return marker is not None and (
        marker.get("status") == "reported" or marker.get("final") is True
    )


class TerminationEvidenceSweep:
    """One bounded pass over ended grants; reports as the worker that held each lease."""

    def __init__(
        self,
        channel: Any,
        *,
        run: RunDocker,
        clock: Callable[[], float],
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        poll_seconds: float = 2.0,
        jitter: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self.channel = channel
        self.run = run
        self.clock = clock
        self.wall_clock = wall_clock
        self.poll_seconds = poll_seconds
        self.jitter = jitter
        # grant id -> (consecutive holds or failures, clock time of the next look). In memory:
        # a restarted poller looks once more, then backs off again.
        self._backoff: dict[str, tuple[int, float]] = {}

    def due(self, grant_id: str) -> bool:
        return self._backoff.get(grant_id, (0, 0.0))[1] <= self.clock()

    def sweep(self, grant_dirs: Sequence[Path]) -> list[dict[str, Any]]:
        results = []
        for grant_dir in grant_dirs:
            if len(results) >= MAX_PER_SWEEP:
                break
            if not self.due(grant_dir.name) or settled(grant_dir):
                continue
            results.append({"grant_id": grant_dir.name, **self.check(grant_dir)})
        return results

    def _defer(self, grant_id: str) -> None:
        count = self._backoff.get(grant_id, (0, 0.0))[0] + 1
        delay = backoff_delay(count, self.poll_seconds, BACKOFF_MAX_SECONDS, self.jitter())
        self._backoff[grant_id] = (count, self.clock() + delay)

    def _record(self, grant_dir: Path, status: str, reason: str, **fields: Any) -> dict[str, Any]:
        document = {
            "schema": SCHEMA,
            "status": status,
            "reason": reason,
            "final": status == "held" and reason in FINAL_REASONS,
            "checked_at": self.wall_clock().isoformat(),
            **fields,
        }
        _write_private(grant_dir, MARKER, document)
        if status == "held" and not document["final"]:
            self._defer(grant_dir.name)
        else:
            self._backoff.pop(grant_dir.name, None)
        return {"status": status, "reason": reason}

    def check(self, grant_dir: Path) -> dict[str, Any]:
        runtime_dir = grant_dir / "workspace" / "runtime"
        if (runtime_dir / "provider-dispatch.sqlite3").exists():
            # A remote provider's operation is not a local container; its own path applies.
            return {"status": "skipped", "reason": "remote_provider"}
        identity = read_identity(runtime_dir)
        if identity is None:
            return self._record(grant_dir, "held", "descriptor_unreadable")
        try:
            journal = read_journal(runtime_dir, identity.operation_id)
        except sqlite3.Error:
            return self._record(grant_dir, "held", "journal_unreadable")
        base = {"operation_id": identity.operation_id, "lease_id": identity.lease_id}
        if journal.exit_delivered:
            return self._record(grant_dir, "reported", "exit_already_delivered", **base)
        intent = _read_private(grant_dir / INTENT)
        if intent is None or intent.get("operation_id") != identity.operation_id:
            finding = assess(self.run, identity, journal)
            if finding.kind == "held":
                return self._record(
                    grant_dir, "held", finding.reason, provider_id=finding.provider_id, **base
                )
            intent = self._intent(identity, finding)
            _write_private(grant_dir, INTENT, intent)
        try:
            self.channel.post(OBSERVATION_PATH, intent["payload"], intent["key"])
        except (ConnectionError, OSError) as error:
            kind = classify_control_failure(error)
            status = error.status if isinstance(error, ControlChannelError) else None
            if kind == "permanent":
                # Control refused this evidence (for example the lease has not ended yet):
                # look again later with a fresh observation.
                (grant_dir / INTENT).unlink(missing_ok=True)
            return self._record(
                grant_dir,
                "held",
                "control_refused" if kind == "permanent" else "delivery_pending",
                control_status=status,
                **base,
            )
        (grant_dir / INTENT).unlink(missing_ok=True)
        payload = intent["payload"]
        return self._record(
            grant_dir,
            "reported",
            payload.get("termination_evidence") or "container_exited",
            provider_id=payload["provider_id"],
            exit_code=payload.get("exit_code"),
            observed_at=payload["observed_at"],
            **base,
        )

    def _intent(self, identity: Identity, finding: Finding) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "lease_id": identity.lease_id,
            "expected_epoch": identity.epoch,
            "operation_id": identity.operation_id,
            "observed_state": "exited",
            "observed_at": self.wall_clock().isoformat(),
            "provider_id": finding.provider_id,
            "termination_confirmed": True,
        }
        if finding.kind == "exited":
            payload["exit_code"] = finding.exit_code
        else:
            payload["termination_evidence"] = CONTAINER_ABSENT
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        key = (
            "termination-evidence-"
            + hashlib.sha256(f"{identity.operation_id}:{encoded}".encode()).hexdigest()
        )
        return {"operation_id": identity.operation_id, "payload": payload, "key": key}


__all__ = [
    "CONTAINER_ABSENT",
    "MARKER",
    "Finding",
    "Identity",
    "TerminationEvidenceSweep",
    "assess",
    "read_identity",
    "read_journal",
    "settled",
]
