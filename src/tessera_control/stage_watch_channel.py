"""In-process channel for stage watch reports from a driver that holds the worker principal.

A remote stage (Salad) is watched beside its provider-side driver: the campaign executor,
which already authenticated its worker credential and talks to the control stores directly
(``StoreControl``). ``InProcessWatchChannel`` gives that executor's ``StageWatchRunner`` the
worker channel's ``post`` without an HTTP hop: it accepts only the watch-reports route and
records each report through ``StageWatchStore.report_stage_watch`` exactly as the HTTP route
does, with the same body validation, the same digest and the same refusal mapping:

- a ``WatchRefused`` raises ``ControlChannelError`` with ``REASON_STATUS[reason]`` and the
  closed ``refusal_reason``; the shared idempotency conflict is ``key_conflict`` (409);
- any other ``StoreError`` gets the API's global status (404, 409, 403 or 400) and no reason;
- a body the route's schema refuses is 422 with no reason; anything unexpected is 500.

So the watcher's settle and retry rules (``classify_status``) behave as they do over HTTP. The
channel holds no credential beyond the ``worker`` principal its caller already authenticated,
and it can reach nothing but this one append-only, operational-only route.

``SaladRouteWatch`` and ``build_salad_route_watch`` are the executor-side glue (ADR 0111):
one ``StageWatchRunner`` per executor, ticked from a daemon thread while a route's child
process runs. Off unless the executor config enables it; this module does not import the
watcher itself (only ``build_salad_route_watch`` and ``SaladRouteWatch.begin`` do).
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tessera_worker.control_channel import _SAFE_PATH, MAX_REQUEST_BYTES, ControlChannelError

from .stage_watch import KEY_CONFLICT_DETAIL, StageWatchStore, WatchRefused
from .stage_watch_limits import REASON_STATUS
from .stage_watch_models import ReportStageWatch
from .store import Conflict, Forbidden, NotFound, StoreError, canonical_digest

_WATCH_PATH = re.compile(r"/worker/operations/([0-9a-f]{32})/watch-reports\Z")
_MAX_KEY_CHARS = 128


def _refused(status: int, reason: str | None = None) -> ControlChannelError:
    return ControlChannelError("control rejected request", status, refusal_reason=reason)


class InProcessWatchChannel:
    """``post(path, body, key, timeout_seconds=None)`` for the watch-reports route only."""

    def __init__(self, store: StageWatchStore, worker: dict[str, Any]) -> None:
        if not isinstance(store, StageWatchStore):
            raise ValueError("InProcessWatchChannel needs a StageWatchStore.")
        if not isinstance(worker, dict) or not isinstance(worker.get("id"), str):
            raise ValueError("InProcessWatchChannel needs an authenticated worker principal.")
        self._store = store
        self._worker = worker

    def post(
        self,
        path: str,
        body: dict[str, Any],
        key: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Record one report in-process; return the stored response or raise like HTTP would.

        ``timeout_seconds`` is accepted for the channel interface; the call is a local
        SQLite transaction bounded by the store's own busy timeout.
        """
        if (
            not isinstance(path, str)
            or not _SAFE_PATH.fullmatch(path)
            or "%" in path
            or (match := _WATCH_PATH.fullmatch(path)) is None
        ):
            raise ValueError("Control request path is not the watch-reports route.")
        operation_id = match.group(1)
        if not isinstance(key, str) or not key.strip() or len(key) > _MAX_KEY_CHARS:
            # The API's idempotency-key dependency answers a plain StoreError (400).
            raise _refused(400)
        if not isinstance(body, dict):
            raise ValueError("Control request body must be a JSON object.")
        try:
            encoded = json.dumps(body, allow_nan=False, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError):
            raise ValueError("Control request body is not JSON serializable.") from None
        if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("Control request body exceeds its bound.")
        try:
            # What the route sees: the JSON body through the route's closed model.
            payload = ReportStageWatch.model_validate(json.loads(encoded)).model_dump(mode="json")
        except ValidationError:
            raise _refused(422) from None
        try:
            _status, response = self._store.report_stage_watch(
                self._worker, operation_id, key, canonical_digest(payload), payload
            )
        except WatchRefused as error:
            raise _refused(REASON_STATUS[error.reason], error.reason) from None
        except StoreError as error:
            if isinstance(error, Conflict) and str(error) == KEY_CONFLICT_DETAIL:
                raise _refused(REASON_STATUS["key_conflict"], "key_conflict") from None
            if isinstance(error, NotFound):
                raise _refused(404) from None
            if isinstance(error, Conflict):
                raise _refused(409) from None
            if isinstance(error, Forbidden):
                raise _refused(403) from None
            raise _refused(400) from None
        except Exception:
            # An unexpected failure is a 500 over HTTP: transient for the watcher.
            raise _refused(500) from None
        if not isinstance(response, dict):
            raise ControlChannelError("control response must be a JSON object", 500)
        return dict(response)


# -- executor-side glue for a Salad route (ADR 0111) -------------------------------------------

SALAD_PROVIDER = "salad"
# Bounded waits after the route's child process returns.
TICKER_JOIN_SECONDS = 10.0
FINAL_FLUSH_TICKS = 2


def _no_docker(*_args: Any, **_kwargs: Any) -> Any:
    """A remote route has no Docker probe; the runner never calls this for ``salad``."""
    raise RuntimeError("no local Docker probe for a remote route")


class SaladRouteWatch:
    """Watch one Salad route at a time beside the executor's blocking child process.

    Descriptive only: it starts a remote watcher (``on_start_remote``), ticks it from one
    daemon thread while the child runs, then ends it with a bounded flush. It has no stop,
    cancel or retry path, holds no management credential, and every call is caught, so it can
    never raise into the executor or change a route's result. The runner is touched by the
    ticker thread only while the child runs and by the calling thread only before the thread
    starts and after it was joined; if a join times out the watch is disabled for this
    process (pending reports resume on the next start from the durable outbox).
    """

    def __init__(self, runner: Any, poll_seconds: float) -> None:
        self.runner = runner
        self.poll_seconds = float(poll_seconds)
        self.errors = 0
        self._thread: Any = None
        self._disabled = False

    def begin(
        self,
        *,
        operation_id: str,
        lease_id: str,
        epoch: int,
        stage_key: str,
        deadline_seconds: int,
    ) -> Path | None:
        """Start the watcher for a route about to run; its snapshot path, else None."""
        try:
            from tessera_worker.stage_watch_runner import RemoteOperationInfo

            if self._disabled:
                return None
            path = self.runner.signals_path(operation_id)
            if path is None:
                return None
            self.runner.on_start_remote(
                RemoteOperationInfo(
                    operation_id=operation_id,
                    lease_id=lease_id,
                    epoch=epoch,
                    stage_key=stage_key,
                    provider=SALAD_PROVIDER,
                    deadline_seconds=deadline_seconds,
                    plan_digest=None,
                )
            )
            if operation_id not in self.runner.active_operations:
                return None  # refused (no kill switch, unknown provider, ...): no watch
            return Path(path)
        except Exception:
            self.errors += 1
            return None

    @contextmanager
    def ticking(self) -> Iterator[None]:
        """Tick the runner every ``poll_seconds`` from a daemon thread while the body runs."""
        stop = threading.Event()
        thread = None
        try:
            if not self._disabled:
                thread = threading.Thread(
                    target=self._tick_until, args=(stop,), name="salad-stage-watch", daemon=True
                )
                thread.start()
                self._thread = thread
        except Exception:
            self.errors += 1
            thread = None
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                try:
                    thread.join(TICKER_JOIN_SECONDS)
                    if thread.is_alive():
                        self._disabled = True  # never touch the runner from two threads
                except Exception:
                    self.errors += 1
                    self._disabled = True

    def end(self, operation_id: str) -> None:
        """The child returned: end the watcher, flush a few times, then let it go."""
        if self._disabled:
            return
        try:
            self.runner.on_end(operation_id)
            for _ in range(FINAL_FLUSH_TICKS):
                if operation_id not in self.runner.active_operations:
                    break
                self.runner.tick()
            if operation_id in self.runner.active_operations:
                # Nothing observes this route any more, so its snapshot only goes stale: forget
                # the watcher rather than sample it on later ticks. Pending reports stay in the
                # outbox and resume (flush only) on the next start.
                self.runner.close()
        except Exception:
            self.errors += 1

    def _tick_until(self, stop: threading.Event) -> None:
        while not stop.wait(self.poll_seconds):
            try:
                self.runner.tick()
            except Exception:
                self.errors += 1


def build_salad_route_watch(
    block: Any,
    *,
    state_dir: Path | str,
    worker: dict[str, Any],
    poll_seconds: float,
    clock: Any = None,
) -> SaladRouteWatch | None:
    """The executor's route watch for an enabled, startable block, else None. Never raises.

    Imports the watcher only here, on the enabled branch.
    """
    try:
        if block is None or not block.enabled or block.kill_switch_path is None:
            return None
        from tessera_worker.stage_watch_config import StageWatchConfig
        from tessera_worker.stage_watch_runner import (
            StageWatchRunner,
            register_probe_factory,
            snapshot_file_probe_factory,
        )

        config = StageWatchConfig(**block.watch_config_fields())
        register_probe_factory(SALAD_PROVIDER, snapshot_file_probe_factory())
        runner = StageWatchRunner(
            config,
            state_dir=state_dir,
            channel=InProcessWatchChannel(StageWatchStore(Path(state_dir)), worker),
            docker_run=_no_docker,
            clock=clock,
        )
        if runner.directory is None:
            return None
        return SaladRouteWatch(runner, poll_seconds)
    except Exception:
        return None
