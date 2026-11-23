"""Agent-loop action that consumes person-approved campaign submissions (ADR 0143).

The action only calls ``submit`` on approvals a person already recorded. It has no way to
approve, create or widen anything. Events carry ids and stable reasons only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from tessera_agent.standing_loop import _API_KEY_VAR, ApiKeyPresent, KillSwitch


class SubmissionClient(Protocol):
    def list_pending(self, project_id: str) -> Any:
        """The control list response: {"submission_approvals": [{id, revision, ...}]}."""

    def submit(self, approval_id: str, expected_revision: int, idempotency_key: str) -> Mapping:
        """Submit one approval. Raise ``SubmitRefused(reason)`` or return the response."""


class SubmitRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SubmitPolicy:
    project_id: str
    max_submissions_per_tick: int = 1
    max_attempts_per_tick: int = 5


class CampaignSubmitAction:
    def __init__(
        self,
        client: SubmissionClient,
        kill_switch: KillSwitch,
        record: Callable[[dict], None],
        policy: SubmitPolicy,
        environ: Mapping[str, str] | None = None,
    ):
        import os

        env = os.environ if environ is None else environ
        if _API_KEY_VAR in env:
            raise ApiKeyPresent(f"{_API_KEY_VAR} is set; refusing to run")
        if policy.max_submissions_per_tick < 1:
            raise ValueError("max_submissions_per_tick must be at least 1")
        if policy.max_attempts_per_tick < 1:
            raise ValueError("max_attempts_per_tick must be at least 1")
        self._client = client
        self._kill = kill_switch
        self._record = record
        self._policy = policy
        # Records refused recently (per process): tried after fresh ones so one persistently
        # refused record cannot starve the rest. Cleared once every pending record is in it.
        self._refused: set[tuple[str, int]] = set()

    def tick(self) -> list[dict]:
        events: list[dict] = []

        def emit(event: dict) -> None:
            events.append(event)
            self._record(event)

        if self._kill.engaged():
            emit({"event": "submit_skipped", "reason": "kill_switch"})
            return events
        try:
            listed = self._client.list_pending(self._policy.project_id)
        except Exception as error:  # noqa: BLE001 - recorded by type only
            emit({"event": "list_failed", "reason": type(error).__name__})
            return events
        items = listed.get("submission_approvals") if isinstance(listed, Mapping) else None
        if not isinstance(items, list):
            emit({"event": "list_malformed"})
            return events
        records = [i for i in items if isinstance(i, Mapping)]
        keys = {(i.get("id"), i.get("revision")) for i in records}
        if keys and keys <= self._refused:
            self._refused.clear()  # a full cycle of refusals: start over
        records.sort(key=lambda i: (i.get("id"), i.get("revision")) in self._refused)
        submitted = attempted = 0
        for item in records:
            if (
                submitted >= self._policy.max_submissions_per_tick
                or attempted >= self._policy.max_attempts_per_tick
            ):
                break
            approval_id, revision = item.get("id"), item.get("revision")
            if not isinstance(approval_id, str) or not isinstance(revision, int):
                continue
            if self._kill.engaged():
                emit(
                    {"event": "submit_skipped", "reason": "kill_switch", "approval_id": approval_id}
                )
                break
            attempted += 1  # each record is tried at most once per tick
            key = f"agent_loop:{approval_id}:{revision}"
            try:
                result = self._client.submit(approval_id, revision, key)
            except SubmitRefused as refusal:
                self._refused.add((approval_id, revision))
                emit(
                    {
                        "event": "submit_refused",
                        "approval_id": approval_id,
                        "revision": revision,
                        "reason": refusal.reason,
                    }
                )
                continue  # never retried in this tick
            except Exception as error:  # noqa: BLE001 - recorded by type only
                self._refused.add((approval_id, revision))
                emit(
                    {
                        "event": "submit_failed",
                        "approval_id": approval_id,
                        "revision": revision,
                        "reason": type(error).__name__,
                    }
                )
                continue
            submitted += 1
            self._refused.discard((approval_id, revision))
            emit(
                {
                    "event": "submitted",
                    "approval_id": approval_id,
                    "revision": revision,
                    "campaign_id": (result or {}).get("campaign_id"),
                }
            )
        return events
