"""External native-service custody and Linux-only bounded sensor primitives.

Nothing starts on import. Run service composition under a separate Linux service
manager with a hard timeout; never inside the broker. CNI and filesystem-specific
native drivers remain explicit deployment dependencies, not installed/discovered
here. Missing native facilities are unknown, including stock local-path storage.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import stat
import sys
import time

from .capability_activation import canonical, stamp
from .capability_kube_client import CollectionError, decode, encoded
from .capability_witness import exact, require, safe_payload
from .capability_witness_native import NATIVE_REQUEST, NATIVE_RESPONSE, OPERATIONS


class NativeSupervisorService:
    """Authenticated one-use native RPC core for `serve_witness` with required mTLS.

    The deployment supplies the exact fresh Challenge out of band from its
    authenticated WitnessDaemon, and a fixed NativeDriver. There is no report
    import, dynamic plugin, arbitrary command or credential acquisition endpoint.
    Every operation, including read/fault/kill/cleanup, is consumed before dispatch.
    A missing reply or restart never replays it. Driver evidence is retained only
    after secret rejection; inability to retain it remains unknown.
    """

    def __init__(
        self,
        *,
        target,
        request,
        driver,
        journal,
        request_key,
        response_key,
        authority,
        now,
        monotonic=time.monotonic,
    ):
        require(
            type(request_key) is bytes
            and type(response_key) is bytes
            and min(len(request_key), len(response_key)) >= 32
            and request_key != response_key
        )
        require(
            request.kind == target.pin.kind
            and request.plan_digest == target.profile.plan_digest
            and request.cluster_digest == target.profile.cluster_digest
        )
        self.target, self.pin, self.request, self.driver = target, target.pin, request, driver
        self.journal, self.authority, self.now, self.clock = journal, authority, now, monotonic
        self._request_key, self._response_key = request_key, response_key
        self.deadline = self.clock() + min(
            target.profile.max_seconds, (stamp(request.deadline) - now()).total_seconds()
        )
        with journal.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS native_calls (
                    nonce TEXT NOT NULL, sequence INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    request BLOB NOT NULL, PRIMARY KEY(nonce,sequence), UNIQUE(nonce,fingerprint));
                CREATE TABLE IF NOT EXISTS native_replies (
                    nonce TEXT NOT NULL, sequence INTEGER NOT NULL, response BLOB NOT NULL,
                    PRIMARY KEY(nonce,sequence));
            """)
            for table in ("native_calls", "native_replies"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action} BEFORE {action} "
                        f"ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'retained supervisor evidence'); END"
                    )

    def handle(self, body):
        try:
            require(type(body) is bytes and len(body) <= self.target.profile.max_bytes)
            envelope = decode(body)
            exact(envelope, "binding signature")
            b = envelope["binding"]
            raw = safe_payload(b, secrets=(self._request_key, self._response_key))
            require(
                hmac.compare_digest(
                    envelope["signature"],
                    hmac.new(self._request_key, NATIVE_REQUEST + raw, "sha256").hexdigest(),
                )
            )
            exact(b, "target_digest request_digest challenge nonce sequence operation arguments")
            require(
                b["target_digest"] == self.target.profile.digest
                and b["request_digest"] == hashlib.sha256(canonical(self.request)).hexdigest()
                and b["challenge"] == self.request.challenge
                and b["nonce"] == self.request.run_nonce
                and b["operation"] in OPERATIONS
                and type(b["sequence"]) is int
            )
            fingerprint = hashlib.sha256(encoded([b["operation"], b["arguments"]])).hexdigest()
            with self.journal.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                count = db.execute(
                    "SELECT COUNT(*) FROM native_calls WHERE nonce=?", (b["nonce"],)
                ).fetchone()[0]
                require(b["sequence"] == count + 1 <= self.target.profile.max_calls)
                db.execute(
                    "INSERT INTO native_calls VALUES (?,?,?,?)",
                    (b["nonce"], b["sequence"], fingerprint, raw),
                )
            state, data = "unknown", {}
            try:
                require(
                    self.clock() < self.deadline
                    and self.now() < stamp(self.request.deadline)
                    and self.authority(self.request) is True
                )
                data = self.driver.execute(
                    b["operation"], self.request, b["arguments"], deadline=self.deadline
                )
                safe_payload(data, secrets=(self._request_key, self._response_key))
                require(self.clock() < self.deadline and self.authority(self.request) is True)
                state = "observed"
            except Exception:
                data = {}  # Never sign/retain upstream errors or partially unsafe raw output.
            payload = {"binding": b, "state": state, "data": data}
            raw = safe_payload(payload, secrets=(self._request_key, self._response_key))
            with self.journal.connect() as db:
                size = db.execute(
                    "SELECT COALESCE(SUM(length(response)),0) FROM native_replies WHERE nonce=?",
                    (b["nonce"],),
                ).fetchone()[0]
                require(size + len(raw) <= self.target.profile.max_bytes)
                db.execute(
                    "INSERT INTO native_replies VALUES (?,?,?)", (b["nonce"], b["sequence"], raw)
                )
            return encoded(
                {
                    "payload": payload,
                    "signature": hmac.new(
                        self._response_key, NATIVE_RESPONSE + raw, "sha256"
                    ).hexdigest(),
                }
            )
        except Exception:
            raise CollectionError("unavailable") from None


class LinuxCgroupFiles:
    """Read a supervisor-owned cgroup directory FD, never a workload path.

    The CRI driver must independently bind this FD's inode/mount ID to the exact
    container ID and Pod UID. This reader alone cannot assert CRI identity or
    cgroup2fs and does not create/set quotas. The FD remains in supervisor custody.
    """

    FILES = (
        "cpu.max",
        "cpu.stat",
        "memory.max",
        "memory.peak",
        "memory.events",
        "pids.max",
        "pids.peak",
        "pids.events",
    )

    def __init__(self, directory_fd, *, monotonic=time.monotonic):
        require(sys.platform == "linux" and stat.S_ISDIR(os.fstat(directory_fd).st_mode))
        self.fd, self.clock = directory_fd, monotonic

    def snapshot(self, *, deadline):
        result = {}
        for name in self.FILES:
            require(self.clock() < deadline)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
            try:
                require(stat.S_ISREG(os.fstat(fd).st_mode))
                value = os.read(fd, 4097)
                require(len(value) <= 4096 and self.clock() < deadline)
                result[name] = value.decode("ascii")
            finally:
                os.close(fd)
        return result


class LinuxQuotaSentinel:
    """One byte/inode at an independently measured hard boundary, never host fill.

    A quota-enabled filesystem driver supplies `query`: an ioctl/quotactl-backed
    snapshot for this exact owned mount/project. Capacity/statvfs/requested Pod
    limits are NOT implementations of query. An unavailable driver blocks before
    write. The fixture supervisor prepares a dedicated bounded boundary file;
    arbitrary paths, symlinks, nonempty unowned directories and quotas on shared
    research storage are not accepted. This primitive neither installs quotas nor
    attempts unbounded exhaustion to discover a limit.
    """

    def __init__(self, *, directory_fd, boundary_fd, query, nonce, monotonic=time.monotonic):
        require(
            sys.platform == "linux"
            and len(nonce) == 64
            and all(c in "0123456789abcdef" for c in nonce)
        )
        require(
            stat.S_ISDIR(os.fstat(directory_fd).st_mode)
            and stat.S_ISREG(os.fstat(boundary_fd).st_mode)
        )
        self.directory, self.boundary, self.query = directory_fd, boundary_fd, query
        self.name, self.clock = "sentinel-" + nonce, monotonic

    def observe(self, *, resource, hard_limit, deadline):
        require(
            resource in {"output-bytes", "output-inodes", "ephemeral-bytes"}
            and type(hard_limit) is int
            and 0 < hard_limit <= 64 * 1024 * 1024
        )
        require(self.clock() < deadline)
        before = self.query(self.directory, deadline=deadline)
        exact(before, "mount quota")
        unit = "inodes" if resource == "output-inodes" else "bytes"
        mount, quota = before["mount"], before["quota"]
        require(
            mount["dedicated"] is True
            and mount["empty_at_prepare"] is True
            and quota["enforcement"] == "hard"
            and quota["project_id"] == mount["project_id"]
            and quota[unit + "_hard"] == quota[unit + "_used"] == hard_limit
        )
        require(self.clock() < deadline)
        code = None
        successful = False
        try:
            if unit == "inodes":
                fd = os.open(
                    self.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self.directory,
                )
                successful = True
                os.close(fd)
                # Unexpected successful create remains an owned unknown fixture
                # for exact supervisor cleanup; do not delete by path here.
            else:
                os.lseek(self.boundary, 0, os.SEEK_END)
                os.write(self.boundary, b"x")
                successful = True
                os.fsync(self.boundary)  # delayed-allocation errors matter too
        except OSError as exc:
            code = exc.errno
        after = self.query(self.directory, deadline=deadline)
        require(
            self.clock() < deadline and not successful and code == errno.EDQUOT and after == before
        )
        return {
            "resource": resource,
            "mount": mount,
            "quota_before": quota,
            "quota_after": after["quota"],
            "syscall": {"attempted": 1, "result": -1, "errno": "EDQUOT"},
        }


class MonotonicFixtureWatchdog:
    """Independent service-side lease/kill experiment; no broker clock is used.

    Driver handles come from the native service's owned fixture journal. `inspect`
    must read node process/cgroup and filesystem state, not Pod deletion or fixture
    stdout. `kill` revokes the fixture credential and signals its exact container
    once. Unknown or failed kills are never retried, even after service restart.
    A service manager must enforce the deadline for blocked driver calls.
    """

    def __init__(self, *, journal, driver, boot_id, monotonic=time.monotonic, sleep=time.sleep):
        self.journal, self.driver, self.boot_id = journal, driver, boot_id
        self.clock, self.sleep, self.arms = monotonic, sleep, {}
        with journal.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS native_watchdog_kills "
                "(fixture TEXT PRIMARY KEY, boot TEXT NOT NULL)"
            )

    def arm(self, fixture, challenge):
        require(fixture not in self.arms)
        # A restart loses the heartbeat evidence; a durable previous kill is not
        # resurrected as an armed lease or replayed as a new experiment.
        with self.journal.connect() as db:
            require(
                db.execute(
                    "SELECT 1 FROM native_watchdog_kills WHERE fixture=?", (fixture,)
                ).fetchone()
                is None
            )
        self.arms[fixture] = (challenge, self.clock())
        return {"challenge": challenge, "sequence": 1, "armed": True}

    def terminate_and_observe(self, fixture, *, challenge, trigger, lease, grace, quiet, deadline):
        require(
            fixture in self.arms
            and self.arms[fixture][0] == challenge
            and trigger in {"broker-suspended", "revoked"}
        )
        _, hb = self.arms.pop(fixture)
        triggered = self.clock()
        require(
            hb <= triggered < deadline and 2 <= lease <= 5 and 0 <= grace <= 2 and 1 <= quiet <= 2
        )
        self.driver.fault(fixture, trigger, deadline=deadline)
        if trigger == "broker-suspended":
            remaining = hb + lease - self.clock()
            if remaining > 0:
                require(self.clock() + remaining + quiet < deadline)
                self.sleep(remaining)
        require(self.clock() < deadline)
        with self.journal.connect() as db:
            db.execute("INSERT INTO native_watchdog_kills VALUES (?,?)", (fixture, self.boot_id))
        killed = self.clock()
        self.driver.kill(fixture, deadline=deadline)  # one request only, never retry
        before = self.driver.inspect(fixture, deadline=deadline)
        confirmed = self.clock()
        require(
            before["exit_code"] == 137
            and before["populated"] == 0
            and before["credential_state"] == "revoked"
            and before["writes"] >= 1
            and confirmed - triggered <= lease + grace
            and confirmed + quiet < deadline
        )
        self.sleep(quiet)
        after = self.driver.inspect(fixture, deadline=deadline)
        end = self.clock()
        require(after == before and confirmed + quiet <= end < deadline)
        return {
            "clock": {
                "boot_id": self.boot_id,
                "heartbeat_ns": int(hb * 10**9),
                "trigger_ns": int(triggered * 10**9),
                "kill_ns": int(killed * 10**9),
                "confirmed_ns": int(confirmed * 10**9),
                "quiet_ns": int(end * 10**9),
            },
            "before": before,
            "after": after,
            "kill_requests": 1,
        }
