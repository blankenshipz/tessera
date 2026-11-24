"""Synchronous, path-allowlisted control adapter for the stuck-finding loop.

``StuckControl.call(method, path, body=None, key=None)`` is the ``control`` object that
``StuckWorkSource`` and ``StuckOfferHandler`` expect. It reaches exactly two control routes of the
configured project and refuses every other method, path or body with ``ValueError`` before any
request is made:

* ``GET  /projects/{project}/campaign-stuck?status=<status>&limit=<n>`` (list findings), and
* ``POST /projects/{project}/campaign-stuck/{finding}/acknowledge`` with body ``{"note": str}``
  and an idempotency key (mark one finding seen; the campaign itself is untouched).

So the runner built on it cannot resume, cancel, approve, submit, scan or change settings, whatever
its callers ask for. Each call runs the existing async ``ControlClient`` method under its own
``asyncio.run`` (as ``SubmissionActionClient`` does); the client reads the owner-only token file
on every request and this adapter never sees, stores or logs it.

A control failure is re-raised as ``StuckControlError`` carrying only the HTTP status (or None
when the request may not have been answered); no response body is ever relayed.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

_PID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}"
# One finding-id shape for the whole loop: exactly what the control mints ("stuck_" + 32 hex).
FINDING_ID = r"stuck_[a-f0-9]{32}"
_LIST = re.compile(
    rf"/projects/(?P<pid>{_PID})/campaign-stuck"
    r"(?:\?status=(?P<status>open|acknowledged|resolved|all)&limit=(?P<limit>[1-9][0-9]{0,2}))?\Z"
)
_ACK = re.compile(
    rf"/projects/(?P<pid>{_PID})/campaign-stuck/(?P<fid>{FINDING_ID})"
    r"/acknowledge\Z"
)
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
MAX_NOTE = 500


class StuckControlError(Exception):
    """A control call failed. ``status`` is the HTTP status, or None if no answer is known."""

    def __init__(self, status: int | None):
        super().__init__(
            f"control call failed (HTTP {status})" if status else "control unavailable"
        )
        self.status = status

    @property
    def definite_refusal(self) -> bool:
        """True for an HTTP 4xx: the control answered and refused, so nothing was changed."""
        return self.status is not None and 400 <= self.status < 500


class StuckControl:
    """``client`` is a ``tessera_mcp.client.ControlClient`` (or anything with the same two async
    methods ``list_stuck(status, limit)`` and ``acknowledge_stuck(finding_id, note, key)``)."""

    def __init__(self, client: Any, project_id: str) -> None:
        if not isinstance(project_id, str) or not re.fullmatch(_PID, project_id):
            raise ValueError("project_id is not a plain identifier")
        self._client = client
        self.project_id = project_id

    def call(self, method: str, path: str, body: Any = None, key: str | None = None) -> dict:
        if method == "GET" and isinstance(path, str) and (m := _LIST.fullmatch(path)):
            self._own(m["pid"])
            if body is not None or key is not None:
                raise ValueError("a listing takes no body or key")
            status, limit = m["status"] or "open", int(m["limit"] or 50)
            if limit > 200:
                raise ValueError("limit is at most 200")
            return self._run(self._client.list_stuck(status, limit))
        if method == "POST" and isinstance(path, str) and (m := _ACK.fullmatch(path)):
            self._own(m["pid"])
            if (
                not isinstance(body, dict)
                or set(body) != {"note"}
                or not isinstance(body["note"], str)
                or len(body["note"]) > MAX_NOTE
            ):
                raise ValueError('an acknowledgement body is exactly {"note": str}')
            if not isinstance(key, str) or not key:
                raise ValueError("an acknowledgement needs an idempotency key")
            return self._run(self._client.acknowledge_stuck(m["fid"], body["note"], key))
        raise ValueError("route not allowed for the stuck-finding loop")

    def _own(self, pid: str) -> None:
        if pid != self.project_id:
            raise ValueError("route is outside the configured project")

    @staticmethod
    def _run(coroutine) -> dict:
        try:
            answer = asyncio.run(coroutine)
        except Exception as error:  # noqa: BLE001 - only the status survives, never a body
            match = _HTTP_STATUS.search(str(error))
            raise StuckControlError(int(match[1]) if match else None) from None
        if not isinstance(answer, dict):
            raise StuckControlError(None)
        return answer
