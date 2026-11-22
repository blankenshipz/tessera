"""Opt-in fixture adapter using capability-journal custody, not capability authority.

The spawn supervisor owns every Docker effect and its own monotonic lease. Parent
loss cannot stop its clock. Supervisor/daemon loss still requires explicit recovery;
neither process liveness nor a kill reply is termination evidence.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

from tessera_control.store import canonical_digest

from .capability_effects import BackendBlocked
from .capability_journal import CapabilityJournal, JournalConflict
from .local_docker_contract import (
    FixturePlan,
    LocalFixtureConfig,
    authenticate,
    capture_archive,
    log_records,
    message,
    require_class_match,
)
from .local_docker_engine import DockerCLI, LocalDockerEngine, safe_engine_error
from .local_docker_namespace import ATTESTATION, STRONG_CLASS, WEAK_CLASS, NamespaceUnknown, require


def unknown_network(error):
    """Finite gate, plus a bounded sanitized interface-name list when the verifier set one."""
    result = dict(state="unknown", gate=str(error))
    if error.interfaces is not None:
        result["interfaces"] = list(error.interfaces)
    if error.sysfs_other is not None:
        result["sysfs_other"] = list(error.sysfs_other)
    return result


class FixtureJournal(CapabilityJournal):
    """Additive local-only tables; never fabricate download/install broker receipts."""

    def __init__(self, root):
        super().__init__(root)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS local_fixture_intent (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    package TEXT NOT NULL, digest TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS local_fixture_events (
                    sequence INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS local_fixture_output (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    manifest TEXT NOT NULL, content BLOB NOT NULL);
            """)
            for table in ("local_fixture_intent", "local_fixture_events", "local_fixture_output"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{action} BEFORE {action} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT,'immutable fixture'); END"
                    )

    def claim(self, plan):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM local_fixture_intent").fetchone():
                raise JournalConflict("fixture already claimed; recovery only, never retry")
            # Do not share a runtime root with a broker/activation journal.
            if db.execute("SELECT 1 FROM operations").fetchone():
                raise JournalConflict("fixture requires separate local journal")
            db.execute(
                "INSERT INTO local_fixture_intent VALUES (1,?,?)",
                (plan.model_dump_json(), plan.digest),
            )

    def plan(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM local_fixture_intent").fetchone()
        if row is None:
            raise JournalConflict("missing fixture intent")
        plan = FixturePlan.model_validate_json(row["package"])
        if plan.digest != row["digest"]:
            raise JournalConflict("fixture intent mismatch")
        return plan

    def event(self, key, payload, *, content=None):
        plan = self.plan()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT payload FROM local_fixture_events WHERE event_key=?", (key,)
            ).fetchone()
            if old:
                value = json.loads(old[0])
                if value["observation"] != payload:
                    raise JournalConflict("conflicting fixture receipt")
                if content is not None:
                    retained = db.execute("SELECT content FROM local_fixture_output").fetchone()
                    if retained is None or retained[0] != content:
                        raise JournalConflict("conflicting fixture bytes")
                return value
            previous = db.execute(
                "SELECT * FROM local_fixture_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            value = dict(
                observed_at=datetime.now(UTC).isoformat(),
                monotonic_ns=time.monotonic_ns(),
                sequence=previous["sequence"] + 1 if previous else 0,
                previous_digest=previous["digest"] if previous else "0" * 64,
                package_digest=plan.digest,
                operation_id=plan.operation_id,
                org_id=plan.org_id,
                project_id=plan.project_id,
                nonce=plan.nonce,
                observation=payload,
            )
            if content is not None:
                manifest = dict(
                    package_digest=plan.digest,
                    operation_id=plan.operation_id,
                    org_id=plan.org_id,
                    project_id=plan.project_id,
                    nonce=plan.nonce,
                    root="/output",
                    path="result.txt",
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    classification="quarantined_unreleased",
                )
                value["manifest_digest"] = canonical_digest(manifest)
                db.execute(
                    "INSERT INTO local_fixture_output VALUES (1,?,?)",
                    (json.dumps(manifest, sort_keys=True), content),
                )
            db.execute(
                "INSERT INTO local_fixture_events VALUES (?,?,?,?)",
                (
                    value["sequence"],
                    key,
                    json.dumps(value, sort_keys=True),
                    canonical_digest(value),
                ),
            )
        return value

    def records(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM local_fixture_events ORDER BY sequence").fetchall()
        previous = "0" * 64
        result = {}
        for index, row in enumerate(rows):
            value = json.loads(row["payload"])
            if (
                value["sequence"] != index
                or value["previous_digest"] != previous
                or canonical_digest(value) != row["digest"]
                or value["package_digest"] != self.plan().digest
            ):
                raise JournalConflict("fixture receipt integrity")
            previous = row["digest"]
            result[row["event_key"]] = value
        return result

    def report(self):
        records = self.records()
        observations = {k: v["observation"] for k, v in records.items()}
        with self.connect() as db:
            output = db.execute("SELECT * FROM local_fixture_output").fetchone()
        if output:
            manifest = json.loads(output["manifest"])
            if (
                manifest["sha256"] != hashlib.sha256(output["content"]).hexdigest()
                or manifest["size"] != len(output["content"])
                or canonical_digest(manifest) != records["custody"]["manifest_digest"]
            ):
                raise JournalConflict("fixture custody integrity")
        plan = self.plan()
        attestation = plan.attestation
        for key in ("network_preflight", "network"):
            recorded = observations.get(key)
            if recorded is None:
                continue
            for field, expected in attestation.items():
                # Historical strong records predate the class fields; a weak plan
                # never has that excuse. A present value must equal the plan's.
                if (field in recorded or plan.attestation_class != STRONG_CLASS) and recorded.get(
                    field
                ) != expected:
                    raise JournalConflict("fixture attestation class mismatch")
        container_cleaned = any(v.get("cleanup_confirmed") is True for v in observations.values())
        volume_cleaned = any(
            v.get("volume_cleanup_confirmed") is True for v in observations.values()
        )
        return dict(
            profile=self.plan().profile,
            **attestation,
            package_digest=self.plan().digest,
            operation_id=self.plan().operation_id,
            nonce=self.plan().nonce,
            container_name=self.plan().name,
            image=self.plan().image,
            state=observations.get("terminal", {}).get("state", "unknown"),
            activation="blocked",
            automatic_retry=False,
            termination_confirmed=any(
                v.get("termination_confirmed") is True for v in observations.values()
            ),
            container_cleanup_confirmed=container_cleaned,
            volume_name=self.plan().output_volume_name,
            volume_created="volume_created" in observations,
            volume_cleanup_confirmed=volume_cleaned,
            # Removal is owed only once a volume create was even attempted.
            cleanup_confirmed=container_cleaned
            and (volume_cleaned or "volume_intent" not in observations),
            evidence_digest=canonical_digest(records),
            records=records,
        )


def supervise(
    plan, journal, engine, receive, *, clock=time.monotonic, sleep=time.sleep, preclaimed=False
):
    """Independent supervisor core. Test doubles inject time/engine, not live evidence."""
    if preclaimed:
        if journal.plan() != plan or "armed" in journal.records():
            raise JournalConflict("supervisor cannot reuse an armed operation")
    else:
        journal.claim(plan)
    journal.event(
        "armed",
        dict(
            supervisor_pid=os.getpid(),
            runtime_seconds=plan.runtime_seconds,
            lease_seconds=3,
            docker_call_seconds=2,
        ),
    )
    attest = plan.attestation
    expected_state = ATTESTATION[plan.attestation_class]["state"]
    image_id = None
    cid = None
    possible = False
    volume_possible = False
    deadline = clock() + plan.runtime_seconds
    heartbeat = clock()
    sequence = 1
    state, reason = "blocked", "prerequisite_unknown"
    try:
        image_id = engine.preflight(plan)
        journal.event("image", dict(image_id=image_id))
        namespace_ready = engine.namespace_preflight()
        require(namespace_ready.get("state") == "available", "namespace_preflight_unknown")
        journal.event("network_preflight", dict(namespace_ready) | attest)
        if clock() >= deadline:
            raise BackendBlocked("preflight deadline")
        # /output is an exact-named tmpfs-typed local volume so the daemon archive API
        # can read it. Intent is durable before the effect; a lost reply never retries.
        journal.event(
            "volume_intent",
            dict(volume=plan.output_volume_name, options=plan.output_volume_options),
        )
        volume_possible = True
        volume = engine.create_volume(plan)
        journal.event(
            "volume_created",
            dict(
                volume=plan.output_volume_name,
                driver=volume.get("Driver"),
                options=volume.get("Options"),
                labels_sha256=canonical_digest(volume.get("Labels") or {}),
            ),
        )
        if clock() >= deadline:
            raise BackendBlocked("volume deadline")
        journal.event("create_intent", dict(container_name=plan.name, image_id=image_id))
        possible = True  # Lost create response is not permission to create again.
        cid = engine.create(plan, image_id)
        journal.event("created", dict(container_id=cid))
        # Arm precedes create/start. At most one call each, even on missing replies.
        if clock() >= deadline:
            raise BackendBlocked("create deadline")
        journal.event("start_intent", dict(container_id=cid))
        engine.run(["start", cid])
        released = False
        while True:
            now = clock()
            action = receive(sequence)
            if action == "heartbeat":
                sequence += 1
                heartbeat = now
            elif action == "cancel":
                state, reason = "cancelled", "cancel"
                break
            elif action == "lost":
                state, reason = "unknown", "broker_loss"
                break
            if now >= deadline or now - heartbeat >= 3:
                state, reason = "unknown", "deadline" if now >= deadline else "heartbeat_loss"
                break
            observed = engine.inspect(plan, image_id, cid)
            raw_logs = engine.run(["logs", cid], limit=4096)
            try:
                logs = log_records(raw_logs, plan)
            except BackendBlocked:
                journal.event(
                    "log_rejected",
                    dict(sha256=hashlib.sha256(raw_logs).hexdigest(), size=len(raw_logs)),
                )
                raise
            if logs:
                journal.event("limits", logs[0])
                if logs[0]["stage"] == "limits_unknown":
                    state, reason = "blocked", "filesystem_or_resource_limits_unknown"
                    break
                if not released:
                    try:
                        proof = engine.verify_network(plan, image_id, cid)
                        # Exact equality on state and (when reported) class: a weak
                        # proof can never satisfy the strong class, nor the reverse.
                        require(proof.get("state") == expected_state, "namespace_proof_unknown")
                        # Historical strong proofs carry no class field; weak always does.
                        require(
                            proof.get(
                                "attestation_class",
                                STRONG_CLASS if plan.attestation_class == STRONG_CLASS else None,
                            )
                            == plan.attestation_class,
                            "namespace_proof_unknown",
                        )
                    except NamespaceUnknown as error:
                        journal.event("network", unknown_network(error) | attest)
                        state, reason = "blocked", "network_namespace_unknown"
                        break
                    journal.event("network", dict(proof) | attest)
                    # Recheck the independent deadline after all blocking Docker reads.
                    if clock() >= deadline or clock() - heartbeat >= 3:
                        state, reason = "unknown", "deadline"
                        break
                    engine.signal(cid, "go")
                    released = True
                    journal.event("released", dict(container_id=cid))
            if len(logs) == 2:
                journal.event("logs", dict(records=logs))
                # The volume is removed after stop. Freeze ALL writers before exact-root
                # capture. Never call Docker cp with a host destination or extract.
                engine.run(["pause", cid])
                paused = engine.inspect(plan, image_id, cid)
                if not paused["State"].get("Paused"):
                    raise BackendBlocked("writers not frozen")
                if plan.attestation_class == WEAK_CLASS:
                    # Weak class re-checks the engine-declared readback before custody.
                    engine.verify_security(paused)
                raw_archive = engine.run(["cp", cid + ":/output", "-"])
                try:
                    content = capture_archive(raw_archive)
                except BackendBlocked:
                    journal.event(
                        "capture_rejected",
                        dict(sha256=hashlib.sha256(raw_archive).hexdigest(), size=len(raw_archive)),
                    )
                    raise
                journal.event(
                    "custody", dict(writers="docker-paused", container_id=cid), content=content
                )
                engine.run(["unpause", cid])
                engine.signal(cid, "finish")
                for _ in range(10):
                    observed = engine.inspect(plan, image_id, cid)
                    if not observed["State"]["Running"]:
                        break
                    if clock() >= deadline:
                        raise TimeoutError
                    sleep(0.05)
                actual = observed["State"]
                if actual["Running"] or actual["Status"] != "exited":
                    raise TimeoutError
                if actual["ExitCode"] != logs[-1]["exit_code"]:
                    raise BackendBlocked("exit mismatch")
                journal.event(
                    "exit",
                    dict(
                        container_id=cid, exit_code=actual["ExitCode"], termination_confirmed=True
                    ),
                )
                state = "completed" if actual["ExitCode"] == 0 else "failed"
                reason = "exit"
                break
            if not observed["State"]["Running"]:
                raise BackendBlocked("unexpected early exit")
            sleep(0.05)
    except NamespaceUnknown as error:
        journal.event("network", unknown_network(error) | attest)
        state, reason = "blocked", "network_namespace_preflight_unknown"
    except BaseException:
        state, reason = (
            ("unknown", "lost_reply_or_invalid_evidence")
            if possible
            else ("blocked", "prerequisite_unknown")
        )
    finally:
        # Unknown is committed BEFORE kill; later independent inspect may confirm
        # termination, but never rewrites an uncertain execution as success.
        try:
            try:
                journal.event("outcome_intent", dict(state=state, reason=reason))
            finally:
                if possible:
                    if preclaimed:
                        # A guardian can take over after our death. Commit the shared
                        # stop claim before dispatch so it cannot duplicate a lost kill.
                        # Unlike the legacy single-owner emergency path, a failed
                        # durable claim leaves the first stop to that independent owner.
                        journal.event(
                            "kill_pending", dict(state="unknown", reason="awaiting_stop_readback")
                        )
                        terminate(journal, engine, plan, image_id, cid, "stop")
                    else:
                        try:
                            journal.event(
                                "kill_pending",
                                dict(state="unknown", reason="awaiting_stop_readback"),
                            )
                        finally:
                            terminate(journal, engine, plan, image_id, cid, "stop")
        finally:
            if volume_possible:
                release_volume(journal, engine, plan, "stop")
        if possible and not journal.report()["termination_confirmed"]:
            state = "unknown"
        journal.event("terminal", dict(state=state, reason=reason))
    return journal.report()


def terminate(journal, engine, plan, image_id, cid, key):
    try:
        journal.event(key + "_intent", dict(container_id=cid, image_id=image_id))
    except Exception:
        # Persistence failure must not disable the independent kill path. The
        # earlier immutable create intent already binds this exact operation.
        pass
    try:
        observed = engine.stop(plan, image_id, cid)
        cid = observed["Id"]
        journal.event(
            key + "_confirmed",
            dict(
                container_id=cid,
                exit_code=observed["State"]["ExitCode"],
                status=observed["State"]["Status"],
                termination_confirmed=True,
                engine_error=safe_engine_error(observed["State"].get("Error", "")),
            ),
        )
        engine.cleanup(plan, image_id, cid)
        journal.event(key + "_cleanup", dict(container_id=cid, cleanup_confirmed=True))
    except Exception:
        try:
            journal.event(key + "_unknown", dict(termination_or_cleanup="unknown"))
        except Exception:
            pass  # Preserve interrupted intent; a fresh owner must recover it.


def release_volume(journal, engine, plan, key):
    """Remove exactly the plan's output volume, after the container is gone. Never raises.

    Intent is journaled first; confirmation needs an exact-name list that no longer
    shows the volume. An in-use or unreadable volume stays recorded as unknown.
    """
    try:
        journal.event(key + "_volume_intent", dict(volume=plan.output_volume_name))
    except Exception:
        pass  # Persistence failure must not disable the removal itself.
    try:
        engine.remove_volume(plan)
        journal.event(
            key + "_volume_removed",
            dict(volume=plan.output_volume_name, volume_cleanup_confirmed=True),
        )
    except Exception:
        try:
            journal.event(key + "_volume_unknown", dict(volume_cleanup="unknown"))
        except Exception:
            pass


def recover(root, config, *, engine=None):
    if not config.enabled:
        raise BackendBlocked("explicit local fixture opt-in required")
    journal = FixtureJournal(root)
    try:
        plan = journal.plan()
        records = journal.records()
        if "terminal" not in records:
            journal.event("terminal", dict(state="unknown", reason="supervisor_restart"))
        report = journal.report()
        if not report["container_cleanup_confirmed"] and "create_intent" in records:
            # A new process has the exclusive lock. It may stop the historical
            # operation only, never call create/start or reset its lifetime.
            engine = engine or LocalDockerEngine(DockerCLI(config, journal.root / "docker-config"))
            image = records["image"]["observation"]["image_id"]
            cid = records.get("created", {}).get("observation", {}).get("container_id")
            if "recovery_intent" not in records:
                terminate(journal, engine, plan, image, cid, "recovery")
        if "volume_intent" in records and not journal.report()["volume_cleanup_confirmed"]:
            # An orphaned exact-named volume only; never any other volume.
            engine = engine or LocalDockerEngine(DockerCLI(config, journal.root / "docker-config"))
            release_volume(journal, engine, plan, "recovery")
        return journal.report()
    finally:
        journal.close()


def _child(root, plan, config, connection, key, *, guard=None, preclaimed=False):
    # Detached from caller's terminal/process group; not a broker thread or daemon.
    os.setsid()
    require_class_match(plan, config)
    journal = FixtureJournal(root)
    try:
        docker_config = journal.root / "docker-config"
        docker_config.mkdir(mode=0o700)
        cli = DockerCLI(config, docker_config)

        def run(args, **kwargs):
            # Lost guardian authority fences forward work, not historical cleanup.
            gated = args[0] in {
                "info",
                "image",
                "create",
                "start",
                "logs",
                "exec",
                "pause",
                "cp",
            } or args[:2] == ["volume", "create"]
            if guard and gated:
                guard()
            result = cli(args, **kwargs)
            if guard and gated:
                guard()
            return result

        # Strong (default) construction is unchanged; the weak class is explicit.
        engine = (
            LocalDockerEngine(run)
            if plan.attestation_class == STRONG_CLASS
            else LocalDockerEngine(run, attestation_class=plan.attestation_class)
        )

        def receive(sequence):
            if guard:
                guard()
            if not connection.poll():
                return None
            try:
                return authenticate(connection.recv_bytes(1024), key, plan, sequence)
            except Exception:
                return "lost"

        supervise(plan, journal, engine, receive, preclaimed=preclaimed)
    finally:
        connection.close()
        journal.close()


class LocalFixtureAdapter:
    """Explicit local fixture seam. Cannot satisfy CapabilityEffects.preflight."""

    def __init__(self, config=None):
        self.config = config or LocalFixtureConfig()

    supervisor = staticmethod(_child)

    def preflight(self, body):
        raise BackendBlocked("fixture evidence cannot activate download/install capabilities")

    def launch(self, plan, root):
        if not self.config.enabled:
            raise BackendBlocked("explicit local fixture opt-in required")
        # Validate before any filesystem/process/engine effect, even for Python callers.
        plan = FixturePlan.model_validate_json(plan.model_dump_json())
        require_class_match(plan, self.config)
        root = Path(root)
        if root.exists() or root.is_symlink() or root.absolute() != root.resolve():
            raise BackendBlocked("fresh private evidence path required; use recovery")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        key = secrets.token_bytes(32)
        process = context.Process(
            target=self.supervisor, args=(root, plan, self.config, child, key)
        )
        process.start()
        child.close()
        return FixtureHandle(plan, process, parent, key)


class FixtureHandle:
    def __init__(self, plan, process, connection, key):
        self.plan, self.process, self.connection, self.key = plan, process, connection, key
        self.sequence = 1

    def send(self, action="heartbeat"):
        self.connection.send_bytes(message(self.key, self.plan, self.sequence, action))
        self.sequence += 1

    def disconnect(self):
        self.connection.close()
