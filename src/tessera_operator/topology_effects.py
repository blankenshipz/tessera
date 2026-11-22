"""Unregistered fixed topology seam. Native admission surrounds EACH effect call.

Every prepare returns the next exact argv/category/digest, never executes it.
effect(operation, digest) dispatches ONLY that retained packet once; no argv,
path, script, approval flag or caller observation input exists. A crash after
claim is unknown acknowledgement, not permission to retry. Reconcile only reads.
Uses the existing operator target and private operation/plan layout. No MCP/CLI
registration, installation, permission issuer, helper Pod or runtime fallback.
"""

import fcntl
import hashlib
import json
import os
import re
import selectors
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from . import maintenance_topology as topology
from .maintenance_witness import read_json
from .schema_supervised import digest

LIMIT = topology.LIMIT
VERSION = "topology-fixed-effects-v1"
# Only structural fields used by the collector; never persist env/annotations,
# commands, args, kubeconfig contents, stderr or raw Kubernetes responses.
FIELDS = set(
    """metadata uid resourceVersion ownerReferences controller spec
volumeName volumes name persistentVolumeClaim claimName template containers
volumeMounts mountPath subPath subPathExpr nodeName hostPath path claimRef namespace
nodeAffinity required nodeSelectorTerms matchExpressions key operator values status
phase nodeInfo machineID items""".split()
)


def project(value):
    if isinstance(value, dict):
        return {k: project(v) for k, v in value.items() if k in FIELDS}
    if isinstance(value, list):
        return [project(v) for v in value]
    return value


def bounded_child(argv, *, timeout, max_bytes):
    """Private runner only; no shell, bounded pipes and own-child shutdown.

    Not exported as a request interface. Native tool admission must already have
    admitted the exact enclosing effect packet; Python does not grant permission.
    No external stderr or exception text leaves this boundary.
    """
    deadline = time.monotonic() + timeout
    output = bytearray()
    stderr_bytes = 0
    with subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    ) as child:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ, "out")
                selector.register(child.stderr, selectors.EVENT_READ, "err")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    for key, _ in selector.select(min(remaining, 0.1)):
                        data = os.read(key.fileobj.fileno(), 8192)
                        if not data:
                            selector.unregister(key.fileobj)
                        elif key.data == "out":
                            output.extend(data)
                        else:
                            stderr_bytes += len(data)
                        if len(output) > max_bytes or stderr_bytes > max_bytes:
                            raise BufferError
                code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
                if code != 0:
                    raise ChildProcessError
        except BaseException:
            if child.poll() is None:
                child.kill()
            child.wait()
            raise
    return output.decode("utf-8", "strict")


class Pending(Exception):
    pass


class TopologyEffects:
    def __init__(self, *, clock=time.time):
        self.clock = clock

    @contextmanager
    def opened(self, operation, *, read_only=False):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", operation):
            topology.deny("invalid operation")
        root = topology.target.SOURCE / "private/local-control-upgrade/supervised" / operation
        topology.local_identity(root)
        directory = root / "topology-effects"
        if not read_only:
            directory.mkdir(mode=0o700, exist_ok=True)
        if directory.exists():
            topology.local_identity(directory)
        fd = None
        try:
            if not read_only or (directory / "lock").exists():
                flags = os.O_RDONLY if read_only else os.O_CREAT | os.O_RDWR
                fd = os.open(directory / "lock", flags | os.O_NOFOLLOW, 0o600)
            try:
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                topology.deny("topology effect owned; preserve owner")
            yield root, directory
        finally:
            if fd is not None:
                os.close(fd)

    def put(self, directory, name, value):
        raw = json.dumps(value, sort_keys=True).encode()
        if len(raw) > LIMIT:
            topology.deny("effect receipt exceeds bound")
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def get(self, directory, name):
        return read_json(directory / name)[0]

    def context(self, root, directory, *, read_only=False):
        saved = self.get(root / "phases", "plan.json")
        plan = saved["value"]
        if digest(plan) != saved["sha256"] or plan["operation"] != root.name:
            topology.deny("retained operation plan drift")
        local = {p: topology.local_identity(root / "custody" / p) for p in ("backup", "validate")}
        if not (directory / "session.json").exists():
            if self.clock() >= plan["deadline"]:
                topology.deny("retained topology expired; do not replay")
            if read_only:
                return plan, None
            self.put(
                directory,
                "session.json",
                dict(
                    plan=digest(plan),
                    local=local,
                    started=self.clock(),
                    expires=min(plan["deadline"], self.clock() + topology.TTL),
                    implementation=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    collector=hashlib.sha256(Path(topology.__file__).read_bytes()).hexdigest(),
                ),
            )
        session = self.get(directory, "session.json")
        if (
            session["plan"] != digest(plan)
            or session["local"] != local
            or session["implementation"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            or session["collector"]
            != hashlib.sha256(Path(topology.__file__).read_bytes()).hexdigest()
        ):
            topology.deny("topology session input or identity drift")
        if self.clock() >= session["expires"]:
            topology.deny("retained topology expired; do not replay")
        return plan, session

    def prepare(self, operation):
        """Disclosed local writes: session, next packet, or finalized receipt."""
        return self._inspect(operation, read_only=False)

    def _inspect(self, operation, *, read_only):
        with self.opened(operation, read_only=read_only) as (root, directory):
            plan, session = self.context(root, directory, read_only=read_only)
            if session is None:
                return dict(state="needs_prepare")
            if (root / "topology.json").exists():
                receipt = self.get(root, "topology.json")
                if receipt["plan_sha256"] != digest(plan) or self.clock() >= receipt["valid_until"]:
                    topology.deny("retained topology result drift or expiry")
                return dict(state="complete", receipt=receipt)
            index = 0
            previous = digest(session)

            def observe(argv, *, timeout, max_bytes):
                nonlocal index, previous
                # argv exists only in the internal collector. No external request
                # can supply it. Replayed sequence must match byte for byte.
                category = (
                    "metadata"
                    if argv[0] == "kubectl"
                    else ("inspect" if argv[3] == "inspect" else "nodeexec")
                )
                packet = dict(
                    version=VERSION,
                    operation=operation,
                    plan=digest(plan),
                    target=plan["target"],
                    index=index,
                    category=category,
                    argv=list(argv),
                    timeout=timeout,
                    max_bytes=max_bytes,
                    expires=session["expires"],
                    previous=previous,
                    implementation=session["implementation"],
                    collector=session["collector"],
                )
                packet["digest"] = digest(packet)
                stem = f"{index:03d}"
                if index >= 48:
                    topology.deny("observation sequence exceeds bound")
                index += 1
                if not (directory / (stem + ".packet.json")).exists():
                    if read_only:
                        raise Pending(None)
                    self.put(directory, stem + ".packet.json", packet)
                elif self.get(directory, stem + ".packet.json") != packet:
                    topology.deny("retained observation sequence drift")
                if not (directory / (stem + ".result.json")).exists():
                    raise Pending(packet)
                result = self.get(directory, stem + ".result.json")
                if result["packet"] != packet["digest"] or result["state"] != "captured":
                    topology.deny("effect failed or unknown; never retry")
                if digest(result["output"]) != result["output_digest"]:
                    topology.deny("retained output drift")
                previous = digest(result)
                return result["output"]

            try:
                receipt = topology.TopologyCollector(observe, clock=self.clock).collect(
                    plan, observed_at=session["started"], publish=not read_only
                )
                if read_only:
                    return dict(state="needs_finalize")
                return dict(state="complete", receipt=receipt)
            except Pending as pending:
                packet = pending.args[0]
                if packet is None:
                    return dict(state="needs_prepare")
                claim = directory / f"{packet['index']:03d}.claim.json"
                return dict(state="unknown_ack" if claim.exists() else "prepared", packet=packet)

    def effect(self, operation, packet_digest):
        """Exactly one admitted effect, not a loop. Unknown claims never execute."""
        pending = self.prepare(operation)
        if pending["state"] != "prepared" or pending["packet"]["digest"] != packet_digest:
            topology.deny("effect not prepared or acknowledgement unknown")
        packet = pending["packet"]
        with self.opened(operation) as (root, directory):
            self.context(root, directory)
            stem = f"{packet['index']:03d}"
            if self.get(directory, stem + ".packet.json") != packet:
                topology.deny("effect packet drift")
            self.put(
                directory, stem + ".claim.json", dict(packet=packet_digest, claimed_at=self.clock())
            )
            try:
                output = bounded_child(
                    packet["argv"],
                    timeout=min(packet["timeout"], packet["expires"] - self.clock()),
                    max_bytes=packet["max_bytes"],
                )
                if self.clock() >= packet["expires"]:
                    raise TimeoutError
                if packet["category"] == "metadata":
                    output = json.dumps(project(json.loads(output)), sort_keys=True)
                if not output or len(output.encode()) > LIMIT - 4096:
                    raise BufferError
                result = dict(
                    state="captured",
                    packet=packet_digest,
                    output=output,
                    output_digest=digest(output),
                    captured_at=self.clock(),
                )
            except Exception as error:
                # Preserve claim, failure identity and no external diagnostics.
                result = dict(
                    state="failed",
                    packet=packet_digest,
                    reason=type(error).__name__,
                    captured_at=self.clock(),
                )
            self.put(directory, stem + ".result.json", result)
            return {k: v for k, v in result.items() if k != "output"}

    def reconcile(self, operation):
        """Read-only: no mkdir/lock creation/session/packet/finalization/dispatch.

        Only prepare may advance durable local state. Retained successful output
        is revalidated, not recollected or relabeled current after expiry.
        """
        return self._inspect(operation, read_only=True)
