"""At-most-once capability runtime. Default effects are synthetic, never ambient.

The supervisor owns the journal and authenticated authority callback. Workloads
can submit only a signed package and pinned lock bytes, never commands or paths.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime

from tessera_control.store import canonical_digest

from .capability_contract import check_broker_connection, check_lockfile_bytes
from .capability_effects import CHUNK_BYTES, BackendBlocked, FakeCapabilityEffects
from .capability_journal import JournalConflict


class EffectDenied(ValueError):
    def __init__(self, reason):
        self.reason = reason


class CapabilityBroker:
    def __init__(
        self,
        journal,
        *,
        authority,
        identity,
        effects=None,
        now=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
    ):
        self.journal = journal
        self.authority = authority
        # Configured supervisor identity, never copied from submitted package.
        self.identity = tuple(identity)  # org, project, worker
        self.effects = effects if effects is not None else FakeCapabilityEffects()
        self.now, self.monotonic = now, monotonic
        self._running = threading.Lock()
        journal.attach()
        journal.recover(now().isoformat())

    def run(self, package, *, lock=b""):
        if not self._running.acquire(blocking=False):
            raise JournalConflict("broker concurrency limit")
        try:
            return self._run(package, lock)
        finally:
            self._running.release()

    def _run(self, package, lock):
        # Invalid/unverified input is never logged verbatim (including exceptions).
        body = package.decode()
        if (body.org_id, body.project_id, body.worker_id) != self.identity:
            raise EffectDenied("policy")
        try:
            if self.authority(package) is not True:
                raise EffectDenied("policy")
        except Exception:
            raise EffectDenied("policy") from None
        started = self.monotonic()
        initial_wall = self.now()
        action = body.binding.action
        content_hash = hashlib.sha256()
        size = 0
        phase = "admission"
        response_digest = None
        response_status = None
        inventory_digest = None
        installed_packages = []
        exit_code = None
        effect_possible = False
        termination = False
        request_digest = canonical_digest(
            {
                "action": action.model_dump(mode="json"),
                "package_digest": package.digest,
                "max_concurrency": 1,
            }
        )

        def receipt(state, reason="none"):
            return dict(
                state=state,
                phase=phase,
                reason=reason,
                timestamp=self.now().isoformat(),
                elapsed_ms=max(0, int((self.monotonic() - started) * 1000)),
                bytes_retained=size,
                request_digest=request_digest,
                response_digest=response_digest,
                response_status=response_status,
                effect_digest=content_hash.hexdigest(),
                exit_code=exit_code,
                termination_confirmed=termination,
                evidence=body.mode,
                inventory_digest=inventory_digest,
                installed_packages=installed_packages,
                max_bytes=action.max_bytes,
                max_seconds=action.max_seconds,
                action_id=action.id,
                artifact_name=action.name,
                artifact_version=action.version,
                artifact_sha256=action.sha256,
                lockfile_sha256=action.lockfile_sha256,
            )

        self.journal.claim(body, package, receipt("requested"))

        def check():
            if self.journal.cancelled(body.operation_id):
                raise EffectDenied("cancel")
            if (
                self.now() < initial_wall
                or self.now() >= datetime.fromisoformat(body.expires_at)
                or self.monotonic() - started >= action.max_seconds
            ):
                raise EffectDenied("timeout")
            try:
                if self.authority(package) is not True:
                    raise EffectDenied("policy")
            except Exception:
                raise EffectDenied("policy") from None

            # A delayed authority response must not extend the signed deadline.
            if (
                self.now() >= datetime.fromisoformat(body.expires_at)
                or self.monotonic() - started >= action.max_seconds
            ):
                raise EffectDenied("timeout")

        def timeout():
            return min(
                1.0,
                action.max_seconds - (self.monotonic() - started),
                (datetime.fromisoformat(body.expires_at) - self.now()).total_seconds(),
            )

        try:
            check()
            self.authority.claim(package, self.journal.identity)
            self.effects.preflight(body)
            if action.kind == "package":
                # This v1 lock is a complete single-artifact closure, not pip text,
                # URLs supplied at use time, dependency discovery, or build hooks.
                if type(lock) is not bytes or len(lock) > 64 * 1024:
                    raise EffectDenied("policy")
                try:
                    check_lockfile_bytes(body.binding, lock)
                    parsed_lock = json.loads(lock)
                except ValueError:
                    raise EffectDenied("policy") from None
                expected = [
                    {
                        "name": action.name,
                        "version": action.version,
                        "artifact_sha256": action.sha256,
                    }
                ]
                if (
                    parsed_lock != expected
                    or action.manager not in {"pip", "uv"}
                    or not action.url.endswith(".whl")
                    or not action.filesystem_root.startswith("/workspace/packages")
                ):
                    raise EffectDenied("policy")
            self.journal.append(body.operation_id, receipt("approved"))
            phase = "download"
            check()
            self.journal.append(body.operation_id, receipt("started"))
            effect_possible = True  # Persisted intent precedes the external call.
            addresses = self.effects.resolve(body, timeout=timeout())
            check()
            try:
                check_broker_connection(body.binding, action.url, list(addresses))
            except ValueError:
                raise EffectDenied("mismatch") from None
            check()
            response = self.effects.open(body, addresses=addresses, timeout=timeout())
            check()
            if type(response.status) is int and 100 <= response.status <= 599:
                response_status = response.status
            # Never retain raw headers, exception text, redirects, bodies or tokens
            # in receipts. Even a malicious response URL is only hashed.
            response_digest = canonical_digest(
                {
                    "url": response.url,
                    "status": response.status,
                    "content_type": response.content_type,
                    "size": response.size,
                    "addresses": response.addresses,
                }
            )
            try:
                check_broker_connection(body.binding, response.url, list(response.addresses))
            except ValueError:
                raise EffectDenied("mismatch") from None
            if (
                response.addresses != addresses
                or response.status != 200
                or response.content_type != action.content_type
                or type(response.size) is not int
                or response.size != action.expected_size
            ):
                raise EffectDenied("mismatch")
            # Read one extra byte at the declared end to detect a lying length.
            while True:
                check()
                chunk = self.effects.read(
                    size, limit=min(CHUNK_BYTES, action.expected_size - size + 1), timeout=timeout()
                )
                if type(chunk) is not bytes:
                    raise EffectDenied("mismatch")
                if len(chunk) > CHUNK_BYTES or size + len(chunk) > action.expected_size:
                    raise EffectDenied("mismatch")
                if chunk:
                    # Retain partial bytes and progress atomically even if authority
                    # was revoked while the bounded read was in flight.
                    content_hash.update(chunk)
                    size += len(chunk)
                    self.journal.append(body.operation_id, receipt("progress"), chunk=chunk)
                check()
                if not chunk:
                    break
            termination = self.effects.stop(timeout=1.0) is True
            if not termination:
                raise EffectDenied("lost_reply")
            effect_possible = False
            if size != action.expected_size or content_hash.hexdigest() != action.sha256:
                raise EffectDenied("mismatch")
            check()
            if action.kind == "package":
                phase = "install"
                self.journal.append(body.operation_id, receipt("started"))
                check()
                termination = False
                effect_possible = True
                target = canonical_digest([package.digest, action.filesystem_root])
                installed = self.effects.install(
                    body, lock, target_digest=target, timeout=timeout()
                )
                termination = installed.termination_confirmed is True
                effect_possible = not termination
                check()
                exit_code = installed.exit_code
                expected_inventory = ((action.name, action.version, action.sha256),)
                inventory_digest = canonical_digest(installed.inventory)
                if (
                    installed.inventory != expected_inventory
                    or installed.verified is not True
                    or exit_code != 0
                    or installed.target_digest != target
                ):
                    raise EffectDenied("partial_install")
                if not termination:
                    raise EffectDenied("lost_reply")
                installed_packages = expected
            phase = "publication"
            check()
            # One SQLite transaction: completion receipt and no-overwrite project-
            # scoped custody manifest. Bytes stay quarantined, never released.
            self.journal.append(body.operation_id, receipt("completed"), publish=True)
        except (sqlite3.Error, JournalConflict):
            # A failed durable write must never permit another effect or invent a
            # terminal receipt inconsistent with the retained byte prefix.
            if effect_possible:
                try:
                    self.effects.stop(timeout=1.0)
                except Exception:
                    pass
            raise
        except Exception as error:
            reason = (
                error.reason
                if isinstance(error, EffectDenied)
                else ("backend_unavailable" if isinstance(error, BackendBlocked) else "lost_reply")
            )
            if reason == "cancel":
                self.journal.append(body.operation_id, receipt("cancel_requested", reason))
            if effect_possible:
                try:
                    termination = self.effects.stop(timeout=1.0) is True
                except Exception:
                    termination = False
            else:
                termination = True
            # A lost reply may have applied an install even if stop later confirms
            # termination. It must never become a successful or retryable effect.
            state = (
                "unknown"
                if not termination or reason == "lost_reply"
                else "cancelled"
                if reason == "cancel"
                else "blocked"
                if reason == "backend_unavailable"
                else "failed"
            )
            self.journal.append(body.operation_id, receipt(state, reason))
        return self.journal.bundle(body.operation_id)[0]
