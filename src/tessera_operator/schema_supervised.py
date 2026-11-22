"""Unexposed supervised maintenance journal; no native permission is synthesized.

Concrete apply is unregistered: a future installation must put native admission
around EACH effect. Local tests never establish human authority or quiescence.
"""

import fcntl
import hashlib
import inspect
import json
import os
import re
import selectors
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from . import upgrade_support as target
from .stopped_observation import read_db
from .upgrade_support import _write_once

STAGES = (
    "scale_zero",
    "network_policy",
    "maintenance_pod",
    "backup",
    "migrate",
    "validate",
    "activate",
)


class SupervisedDenied(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class SupervisedOperation:
    """Operator-owned plan/profile; request input is ONLY the next phase name.

    Profile compilation/readback are injected by composition, not MCP arguments.
    The local profile must derive command/CAS/script from server-owned inputs.
    It cannot prove permission. Repeated guard checks validate one bound action;
    the sole durable effect claim occurs immediately before the single dispatch.
    """

    def __init__(self, directory, profile, *, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile, self.clock = profile, clock

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SupervisedDenied("operation owned; preserve owner") from None
            yield
        finally:
            os.close(fd)

    def read(self, name):
        return json.loads((self.directory / name).read_text())

    def create(self, plan):
        """Local setup only, NOT an agent-request or approval interface."""
        if not re.fullmatch(r"[a-z0-9-]{1,64}", plan["operation"]):
            raise SupervisedDenied("invalid operation identity")
        if plan["deadline"] <= self.clock():
            raise SupervisedDenied("expired outage window")
        with self.locked():
            _write_once(self.directory / "plan.json", {"value": plan, "sha256": digest(plan)})

    def plan(self):
        saved = self.read("plan.json")
        if digest(saved["value"]) != saved["sha256"]:
            raise SupervisedDenied("retained plan drift")
        return saved["value"]

    def expected(self):
        for stage in STAGES:
            if (self.directory / f"{stage}.claim.json").exists() and not (
                self.directory / f"{stage}.result.json"
            ).exists():
                raise SupervisedDenied("claimed effect uncertain; readback only, never resubmit")
            if not (self.directory / f"{stage}.result.json").exists():
                return stage
        return None

    def binding(self, stage):
        plan = self.plan()
        if stage not in STAGES or stage != self.expected():
            raise SupervisedDenied("phase order mismatch")
        if self.clock() >= plan["deadline"]:
            raise SupervisedDenied("expired outage window; admission remains held")
        previous = (
            None
            if stage == STAGES[0]
            else self.read(f"{STAGES[STAGES.index(stage) - 1]}.result.json")
        )
        evidence_file = self.directory / f"{stage}.evidence.json"
        effective_plan = plan | {
            "phase_receipts": {
                p: self.read(f"{p}.result.json")
                for p in STAGES
                if (self.directory / f"{p}.result.json").exists()
            }
        }
        if evidence_file.exists():
            effective_plan = effective_plan | {
                "phase_evidence": {stage: self.read(evidence_file.name)}
            }
        observed = self.profile.observe(effective_plan, stage)
        # The profile must fail on unavailable fencing/custody evidence; the
        # existing stopped_observation false proof is never upgraded here.
        action = self.profile.compile(effective_plan, stage, observed, previous)
        required = {"target", "db", "image", "script_sha256", "command", "evidence_sha256"}
        if set(action) != required or not action["command"]:
            raise SupervisedDenied("incomplete server-derived action")
        if (
            action["target"] != plan["target"]
            or action["db"]["incarnation"] != plan["db_incarnation"]
        ):
            raise SupervisedDenied("phase identity drift")
        expected_hash = plan["db_sha256"] if previous is None else previous["post_db_sha256"]
        if action["db"]["sha256"] != expected_hash:
            raise SupervisedDenied("DB differs from retained phase receipt")
        return dict(
            operation=plan["operation"],
            plan_sha256=digest(plan),
            phase=stage,
            deadline=plan["deadline"],
            profile_sha256=self.profile.source_digest(),
            **action,
        )

    def prepare(self, stage):
        """Retain immutable exact effect packet; does not approve/dispatch it."""
        with self.locked():
            binding = self.binding(stage)
            packet = {"binding": binding, "action_sha256": digest(binding)}
            _write_once(self.directory / f"{stage}.prepared.json", packet)
            return packet

    def pin_evidence(self, stage, reference):
        """Local owner setup, separate from tool input; no authority is issued."""
        with self.locked():
            if stage != self.expected():
                raise SupervisedDenied("cannot bind evidence out of phase")
            _write_once(self.directory / f"{stage}.evidence.json", reference)

    def apply(self, stage):
        """Future native handler body: ONE effect, never registered by this module.

        Native tool admission must surround this call at installation. This method
        does not mint/inspect permission, infer a research grant, or continue work.
        """
        if type(self.profile) is not KubernetesSupervisedProfile:
            raise SupervisedDenied("native exact-effect concrete profile required")
        return self._apply(stage)

    def apply_disposable(self, stage):
        """Scripted test composition only. Never exposed as a native tool."""
        if not isinstance(self.profile, DisposableProfile):
            raise SupervisedDenied("disposable profile required; native apply remains closed")
        return self._apply(stage)

    def _apply(self, stage):
        with self.locked():
            binding = self.binding(stage)
            packet = self.read(f"{stage}.prepared.json")
            if packet != {"binding": binding, "action_sha256": digest(binding)}:
                raise SupervisedDenied("prepared action drift")
            # Validate SAME action repeatedly; no repeated consumption or grant.
            self.profile.check_bound(binding)
            self.profile.check_bound(binding)
            _write_once(self.directory / f"{stage}.claim.json", packet)
            try:
                result = self.profile.dispatch(binding)
                self.profile.validate_result(binding, result)
            except Exception as exc:
                transport = getattr(self.profile, "transport", None)
                _write_once(
                    self.directory / f"{stage}.failure.json",
                    {
                        "action_sha256": digest(binding),
                        "error_type": type(exc).__name__,
                        "transport": getattr(transport, "last_failure", None),
                        "resubmit": False,
                    },
                )
                raise
            _write_once(self.directory / f"{stage}.result.json", result)
            return result

    def reconcile_remote(self, stage):
        """One fixed read of an already claimed effect; never dispatch again."""
        if type(self.profile) is not KubernetesSupervisedProfile or stage not in STAGES:
            raise SupervisedDenied("concrete reader and known phase required")
        with self.locked():
            packet = self.read(f"{stage}.claim.json")
            if digest(packet["binding"]) != packet["action_sha256"]:
                raise SupervisedDenied("claim drift")
            return self.profile.readback(packet["binding"])

    def reconcile(self, stage):
        """Local retained readback only. No dispatch, adoption, retry or cleanup."""
        if stage not in STAGES:
            raise SupervisedDenied("unknown stage")
        with self.locked():
            self.plan()
            claim = self.directory / f"{stage}.claim.json"
            result = self.directory / f"{stage}.result.json"
            migrated = (self.directory / "migrate.claim.json").exists()
            return dict(
                claim=self.read(claim.name) if claim.exists() else None,
                result=self.read(result.name) if result.exists() else None,
                state="captured"
                if result.exists()
                else "unknown_ack"
                if claim.exists()
                else "unclaimed",
                recovery="compatible58 forward-only; admission held"
                if migrated
                else "preclaim abort requires independently unchanged DB/path/incarnation/fence",
                resubmit=False,
                native_permission=False,
            )


class DisposableProfile:
    """Explicit local test interface, NOT an allow-all/native authority gate."""

    def observe(self, plan, stage):
        raise SupervisedDenied("scripted observer not supplied")

    def compile(self, plan, stage, observed, previous):
        raise SupervisedDenied("scripted compiler not supplied")

    def source_digest(self):
        raise SupervisedDenied("scripted source identity not supplied")

    def check_bound(self, binding):
        raise SupervisedDenied("scripted bound-action check not supplied")

    def dispatch(self, binding):
        raise SupervisedDenied("scripted effect not supplied")

    def validate_result(self, binding, result):
        raise SupervisedDenied("scripted phase readback not supplied")


class BoundedKubernetes:
    """Fixed-context argv/stdin transport. No shell, retries or background work."""

    def __init__(self, *, spawn=None, timeout=30, limit=262144):
        self.spawn = spawn or self.launch
        self.timeout, self.limit = timeout, limit
        self.last_failure = None

    @staticmethod
    def launch(argv, body):
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def call(self, command):
        self.last_failure = None
        body = command.get("stdin", "").encode()
        process = self.spawn(command["argv"], body)
        output = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + self.timeout
        reason = None
        try:
            with selectors.DefaultSelector() as selector:
                for name in output:
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                os.set_blocking(process.stdin.fileno(), False)
                if body:
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
                sent = 0
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    for key, _ in selector.select(remaining):
                        if key.data == "stdin":
                            try:
                                sent += os.write(key.fd, body[sent : sent + 8192])
                            except BrokenPipeError:
                                sent = len(body)
                            if sent == len(body):
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        chunk = os.read(key.fd, min(8192, self.limit - len(output[key.data]) + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        elif len(output[key.data]) + len(chunk) > self.limit:
                            raise OverflowError
                        else:
                            output[key.data].extend(chunk)
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
                if process.returncode:
                    raise ChildProcessError
            return json.loads(output["stdout"])
        except (TimeoutError, subprocess.TimeoutExpired):
            reason = "deadline"
        except OverflowError:
            reason = "stream cap"
        except (ChildProcessError, ValueError, OSError):
            reason = "transport/response"
        finally:
            if reason:
                # Only this newly spawned process group belongs to this call.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                self.last_failure = dict(
                    reason=reason,
                    exit_code=process.returncode,
                    stdout_bytes=len(output["stdout"]),
                    stderr_bytes=len(output["stderr"]),
                )
            for name in ("stdin", "stdout", "stderr"):
                getattr(process, name).close()
        raise SupervisedDenied("bounded transport denied/uncertain; readback only")

    def get(self, kind, name=None):
        if kind not in {
            "deployment",
            "pvc",
            "pv",
            "pods",
            "pod",
            "networkpolicy",
            "resourcequotas",
        }:
            raise SupervisedDenied("unknown read kind")
        if name is not None and not re.fullmatch(r"[a-zA-Z0-9.-]{1,200}", name):
            raise SupervisedDenied("unsafe object name")
        argv = [*target.KUBECTL]
        if kind != "pv":
            argv += ["-n", target.NAMESPACE]
        return self.call({"argv": [*argv, "get", kind, *([name] if name else []), "-o", "json"]})


def exec_command(pod, container, code):
    return {
        "argv": [
            *target.KUBECTL,
            "-n",
            target.NAMESPACE,
            "exec",
            pod,
            "-c",
            container,
            "--",
            "/app/.venv/bin/python",
            "-I",
            "-B",
            "-c",
            code,
        ]
    }


def phase_script(stage, operation, db, deadline, target_pins=None):
    """Pinned DB guard plus durable remote claim/result; no unbounded CLI retries."""
    if stage not in {"backup", "migrate", "validate"}:
        raise SupervisedDenied("unknown script phase")
    commands = {
        "backup": [
            ["backup", "--state-dir", "/state/control", "--output", "/scratch/pre"],
            ["restore", "--backup", "/scratch/pre", "--state-dir", "/scratch/pre-held"],
        ],
        "migrate": [["migrate", "--state-dir", "/state/control"]],
        "validate": [
            ["backup", "--state-dir", "/state/control", "--output", "/scratch/post"],
            ["restore", "--backup", "/scratch/post", "--state-dir", "/scratch/post-held"],
        ],
    }[stage]
    return (
        inspect.getsource(read_db)
        + f"""
import json,os,time,subprocess,sys
from pathlib import Path
expected={db!r}
assert time.time() < {deadline!r}
seen=read_db('/state','control/tessera.sqlite3')
assert all(seen[k]==expected[k] for k in ('schema','incarnation','sha256','device','inode'))
root=Path('/scratch')
def save(name,value):
    with (root/name).open('x') as f:
        json.dump(value,f,sort_keys=True);f.flush();os.fsync(f.fileno())
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
save('{stage}.claim.json',{{'operation':{operation!r},'phase':{stage!r},'db':seen}})
for index,args in enumerate({commands!r}):
    assert time.time() < {deadline!r}
    with (root/('{stage}.'+str(index)+'.log')).open('xb') as log:
        subprocess.run([sys.executable,'-I','-B','-m','tessera_control',*args],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=max(1,min(20,{deadline!r}-time.time())))
after=read_db('/state','control/tessera.sqlite3')
assert after['incarnation']==expected['incarnation']
assert after['schema']=={58 if stage in ("migrate", "validate") else 56}
result={{'operation':{operation!r},'phase':{stage!r},'target':{target_pins!r},'input_db_sha256':seen['sha256'],'db':after}}
if {stage!r} in ('backup','validate'):
    bundle=root/('pre' if {stage!r}=='backup' else 'post')
    manifest_bytes=(bundle/'manifest.json').read_bytes()
    manifest=json.loads(manifest_bytes)
    snapshot=read_db(bundle,'snapshot.sqlite3')
    assert snapshot['sha256']==manifest['sha256']
    assert snapshot['incarnation']==seen['incarnation']
    held=bundle.with_name(bundle.name+'-held')
    held_db=read_db(held,'tessera.sqlite3')
    import sqlite3,hashlib
    c=sqlite3.connect((held/'tessera.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True)
    try: recovery=c.execute('SELECT state,source_backup_digest FROM control_recovery').fetchone()
    finally: c.close()
    assert recovery==('held_restore',manifest['sha256'])
    proof={{'operation':{operation!r},'phase':{stage!r},'target':{target_pins!r},'source_incarnation':seen['incarnation'],'input_db_sha256':seen['sha256'],'post_db_sha256':after['sha256'],'manifest_sha256':hashlib.sha256(manifest_bytes).hexdigest(),'snapshot_sha256':snapshot['sha256'],'held_db':held_db,'state':recovery[0]}}
    save('{stage}.restore-proof.json',proof)
    result['custody']={{'manifest':manifest,'manifest_sha256':proof['manifest_sha256'],'restore_receipt':proof,'restore_receipt_sha256':hashlib.sha256((root/'{stage}.restore-proof.json').read_bytes()).hexdigest()}}
save('{stage}.result.json',result)
print(json.dumps(result))
"""
    )


class KubernetesSupervisedProfile:
    """Concrete fixed compiler, bounded transport and phase readback.

    Evidence references are operator-owned independently produced observations,
    never permission. Their hashes and validity must be pinned by the operation
    owner; this collector does not generate stop/fence/guardian/custody proof.
    """

    def __init__(self, *, transport=None, clock=time.time, handoff=None, hold=None):
        self.transport, self.clock = transport or BoundedKubernetes(), clock
        self.handoff = handoff
        self.hold = hold

    def maintenance_hold(self, plan):
        from .maintenance_hold import MaintenanceHold

        if type(self.hold) is not MaintenanceHold:
            raise SupervisedDenied("captured maintenance hold required")
        retained = self.hold.read("hold.claim")["plan"]
        if retained["operation"] != plan["operation"] or retained["target"] != plan["target"]:
            raise SupervisedDenied("maintenance hold target changed")
        return self.hold, retained

    def source_digest(self):
        return digest(
            [
                hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                inspect.getsource(read_db),
                hashlib.sha256(
                    Path(__file__).with_name("maintenance_hold.py").read_bytes()
                ).hexdigest(),
            ]
        )

    def evidence(self, plan, stage):
        reference = plan["phase_evidence"][stage]
        raw = Path(reference["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise SupervisedDenied("independent evidence changed")
        evidence = json.loads(raw)
        if (
            evidence["target"] != plan["target"]
            or evidence["operation"] != plan["operation"]
            or evidence["phase"] != stage
            or evidence["valid_until"] < plan["deadline"]
            or not evidence["observed_at"] <= self.clock() < evidence["valid_until"]
        ):
            raise SupervisedDenied("stale or mismatched evidence")
        # Structured independently owned fence/guardian witness identities, not
        # caller-provided approved/stopped booleans. Missing witnesses deny.
        for kind in ("admission", "host_writers", "liability"):
            witness = evidence["witnesses"][kind]
            data = Path(witness["path"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != witness["sha256"]:
                raise SupervisedDenied("witness bytes changed")
            if not witness["owner"] or witness["valid_until"] < plan["deadline"]:
                raise SupervisedDenied("witness owner/validity missing")
            record = json.loads(data)
            if (
                record["target"] != plan["target"]
                or record["operation"] != plan["operation"]
                or record["kind"] != kind
                or record["valid_until"] < plan["deadline"]
                or not record["observed_at"] <= self.clock() < record["valid_until"]
            ):
                raise SupervisedDenied("independent witness identity/lease changed")
            if kind == "admission" and record["state"] != "held":
                raise SupervisedDenied("admission not held")
            if kind == "host_writers" and record["unaccounted_writers"] != []:
                raise SupervisedDenied("unaccounted host writers")
            if kind == "liability" and record["exposure_cents"] != evidence["exposure_cents"]:
                raise SupervisedDenied("liability readback mismatch")
        if evidence["exposure_cents"] > 5000 or evidence["exposure_cents"] < 0:
            raise SupervisedDenied("aggregate exposure exceeds cap")
        if stage in {"migrate", "validate", "activate"}:
            custody = evidence["backup_custody"]
            if not custody["custodian"] or not custody["restore_receipt_sha256"]:
                raise SupervisedDenied("backup/restore custody missing")
            for key in ("manifest", "restore_receipt"):
                data = Path(custody[key + "_path"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != custody[key + "_sha256"]:
                    raise SupervisedDenied("custody readback changed")
        return evidence

    def observe(self, plan, stage):
        evidence = self.evidence(plan, stage)
        deploy = self.transport.get("deployment", "tessera-control")
        pvc = self.transport.get("pvc", "tessera-control-state")
        pv = self.transport.get("pv", pvc["spec"]["volumeName"])
        for key, obj in (("deployment_uid", deploy), ("pvc_uid", pvc), ("pv_uid", pv)):
            if obj["metadata"]["uid"] != plan["target"][key]:
                raise SupervisedDenied("live target UID changed")
        if pv["spec"]["claimRef"]["uid"] != pvc["metadata"]["uid"]:
            raise SupervisedDenied("PV claim changed")
        pods = self.transport.get("pods")
        for pod in pods["items"]:
            uses = any(
                v.get("persistentVolumeClaim", {}).get("claimName") == "tessera-control-state"
                for v in pod["spec"].get("volumes", [])
            )
            if uses and pod["metadata"]["uid"] not in evidence["allowed_pod_uids"]:
                raise SupervisedDenied("unrecognized PVC consumer")
        if stage != "scale_zero":
            if deploy["spec"]["replicas"] != 0 or deploy["status"].get("replicas", 0) != 0:
                raise SupervisedDenied("writer replicas remain")
            if deploy["status"]["observedGeneration"] != deploy["metadata"]["generation"]:
                raise SupervisedDenied("stop not observed")
        if stage == "activate":
            self.check_handoff(plan, evidence)
        if stage in {"backup", "migrate", "validate"}:
            pod = self.transport.get("pod", plan["operation"])
            if pod["metadata"]["uid"] != evidence["maintenance_pod_uid"]:
                raise SupervisedDenied("maintenance Pod changed")
            if digest(pod["spec"]) != evidence["maintenance_spec_sha256"]:
                raise SupervisedDenied("maintenance Pod profile changed")
            container = "old" if stage == "backup" else "new"
            statuses = {c["name"]: c for c in pod["status"]["containerStatuses"]}
            if statuses[container]["imageID"] != evidence["container_image_ids"][container]:
                raise SupervisedDenied("maintenance runtime image changed")
            code = (
                inspect.getsource(read_db)
                + "\nimport json;print(json.dumps(read_db('/state','control/tessera.sqlite3')))"
            )
            hold, retained = self.maintenance_hold(plan)
            with hold.db_window(retained):
                db = self.transport.call(exec_command(plan["operation"], container, code))
            if db != evidence["db"]:
                raise SupervisedDenied("independent DB observation changed")
        return dict(deployment=deploy, evidence=evidence, db=evidence["db"])

    def check_handoff(self, plan, evidence):
        from .maintenance_handoff import MaintenanceHandoff

        if type(self.handoff) is not MaintenanceHandoff:
            raise SupervisedDenied("owned terminal maintenance handoff required")
        hold, _ = self.maintenance_hold(plan)
        hold.released(plan)
        result = self.handoff.reconcile()
        if (
            result["operation"] != plan["operation"]
            or not result["deletion_acknowledged"]
            or result["target"] != plan["target"]
            or result["validate_sha256"] != digest(plan["phase_receipts"]["validate"])
            or result["pins"]["uid"] != evidence["maintenance_pod_uid"]
            or result["pins"]["spec_sha256"] != evidence["maintenance_spec_sha256"]
            or evidence["db"] != plan["phase_receipts"]["validate"]["remote"]["db"]
            or evidence["db"]["schema"] != 58
        ):
            raise SupervisedDenied("terminal handoff/compatible58 binding changed")

    @staticmethod
    def check_custody(plan, stage, evidence):
        receipts = plan["phase_receipts"]
        source = "validate" if stage == "activate" else "backup"
        remote = receipts[source]["remote"]
        if (
            remote["operation"] != plan["operation"]
            or remote["phase"] != source
            or remote["target"] != plan["target"]
            or remote["db"]["incarnation"] != plan["db_incarnation"]
        ):
            raise SupervisedDenied("custody phase identity mismatch")
        backup = receipts["backup"]["remote"]
        if backup["input_db_sha256"] != plan["db_sha256"]:
            raise SupervisedDenied("backup preimage lineage mismatch")
        if stage in {"validate", "activate"}:
            migration = receipts["migrate"]["remote"]
            if (
                migration["input_db_sha256"] != backup["db"]["sha256"]
                or migration["operation"] != plan["operation"]
                or migration["target"] != plan["target"]
            ):
                raise SupervisedDenied("migration lineage mismatch")
            if stage == "activate" and remote["input_db_sha256"] != migration["db"]["sha256"]:
                raise SupervisedDenied("postbackup lineage mismatch")
        expected = remote["custody"]
        custody = evidence["backup_custody"]
        for key in ("manifest", "restore_receipt"):
            raw = Path(custody[key + "_path"]).read_bytes()
            if (
                hashlib.sha256(raw).hexdigest() != expected[key + "_sha256"]
                or json.loads(raw) != expected[key]
            ):
                raise SupervisedDenied("custody does not match retained phase lineage")

    def compile(self, plan, stage, observed, previous):
        if stage == "activate":
            self.check_handoff(plan, observed["evidence"])
        if stage in {"migrate", "validate", "activate"}:
            self.check_custody(plan, stage, observed["evidence"])
        dep = observed["deployment"]
        old, new = plan["old_image"], plan["new_image"]
        for image in (old, new):
            if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
                raise SupervisedDenied("digest-pinned images required")
        if dep["spec"]["template"]["spec"]["containers"][0]["image"] != old:
            raise SupervisedDenied("source Deployment image changed")
        name = plan["operation"]
        code = ""
        if stage in {"scale_zero", "activate"}:
            patch = [
                {"op": "test", "path": p, "value": v}
                for p, v in (
                    ("/metadata/uid", dep["metadata"]["uid"]),
                    ("/metadata/resourceVersion", dep["metadata"]["resourceVersion"]),
                    ("/spec", dep["spec"]),
                )
            ]
            patch += [
                {"op": "replace", "path": "/spec/replicas", "value": int(stage == "activate")},
                {
                    "op": "replace",
                    "path": "/spec/template/spec/containers/0/image",
                    "value": new if stage == "activate" else old,
                },
            ]
            command = {
                "argv": [
                    *target.KUBECTL,
                    "-n",
                    target.NAMESPACE,
                    "patch",
                    "deployment",
                    "tessera-control",
                    "--type=json",
                    "--patch",
                    json.dumps(patch, sort_keys=True),
                    "-o",
                    "json",
                ]
            }
        elif stage in {"network_policy", "maintenance_pod"}:
            body = self.policy(name) if stage == "network_policy" else self.pod(plan)
            command = {
                "argv": [
                    *target.KUBECTL,
                    "-n",
                    target.NAMESPACE,
                    "create",
                    "-f",
                    "-",
                    "-o",
                    "json",
                ],
                "stdin": json.dumps(body, sort_keys=True),
            }
        else:
            code = phase_script(stage, name, observed["db"], plan["deadline"], plan["target"])
            command = exec_command(name, "old" if stage == "backup" else "new", code)
        return dict(
            target=plan["target"],
            db=observed["db"],
            image=new if stage in {"migrate", "validate", "activate"} else old,
            script_sha256=hashlib.sha256(code.encode()).hexdigest(),
            command=command,
            evidence_sha256=digest(observed),
        )

    @staticmethod
    def policy(name):
        return dict(
            apiVersion="networking.k8s.io/v1",
            kind="NetworkPolicy",
            metadata={"name": name},
            spec={
                "podSelector": {"matchLabels": {"tessera-maintenance-operation": name}},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        )

    @staticmethod
    def pod(plan):
        security = dict(
            runAsUser=65532,
            runAsGroup=65532,
            runAsNonRoot=True,
            allowPrivilegeEscalation=False,
            readOnlyRootFilesystem=True,
            capabilities={"drop": ["ALL"]},
            seccompProfile={"type": "RuntimeDefault"},
        )
        return dict(
            apiVersion="v1",
            kind="Pod",
            metadata={
                "name": plan["operation"],
                "labels": {"tessera-maintenance-operation": plan["operation"]},
            },
            spec=dict(
                restartPolicy="Never",
                automountServiceAccountToken=False,
                activeDeadlineSeconds=600,
                volumes=[
                    {
                        "name": "state",
                        "persistentVolumeClaim": {"claimName": "tessera-control-state"},
                    },
                    {"name": "scratch", "emptyDir": {"sizeLimit": "1Gi"}},
                ],
                containers=[
                    dict(
                        name=n,
                        image=i,
                        securityContext=security,
                        command=[
                            "/app/.venv/bin/python",
                            "-I",
                            "-B",
                            "-c",
                            "import time; time.sleep(600)",
                        ],
                        volumeMounts=[
                            {"name": "state", "mountPath": "/state"},
                            {"name": "scratch", "mountPath": "/scratch"},
                        ],
                    )
                    for n, i in (("old", plan["old_image"]), ("new", plan["new_image"]))
                ],
            ),
        )

    def check_bound(self, binding):
        if self.clock() >= binding["deadline"] or binding["profile_sha256"] != self.source_digest():
            raise SupervisedDenied("deadline/profile changed")

    def dispatch(self, binding):
        self.check_bound(binding)
        if binding["phase"] in {"backup", "migrate", "validate"}:
            hold, retained = self.maintenance_hold(binding)
            with hold.db_window(retained):
                returned = self.transport.call(binding["command"])
        else:
            returned = self.transport.call(binding["command"])
        self.validate_remote(binding, returned)
        if binding["phase"] not in {"backup", "migrate", "validate"}:
            returned = {
                "metadata": {
                    k: v
                    for k, v in returned["metadata"].items()
                    if k in {"name", "uid", "resourceVersion"}
                },
                "validated_response_sha256": digest(returned),
            }
        return dict(
            action_sha256=digest(binding),
            phase=binding["phase"],
            post_db_sha256=returned.get("db", binding["db"])["sha256"],
            remote=returned,
        )

    def validate_result(self, binding, result):
        if result["action_sha256"] != digest(binding):
            raise SupervisedDenied("effect receipt mismatch")
        remote, stage = result["remote"], binding["phase"]
        if stage in {"backup", "migrate", "validate"}:
            self.validate_remote(binding, remote)
        elif not remote.get("validated_response_sha256"):
            raise SupervisedDenied("validated object readback missing")

    @staticmethod
    def validate_remote(binding, remote):
        stage = binding["phase"]
        if stage in {"backup", "migrate", "validate"}:
            if (
                remote["operation"] != binding["operation"]
                or remote["phase"] != stage
                or remote["db"]["incarnation"] != binding["db"]["incarnation"]
                or remote["db"]["schema"] != (56 if stage == "backup" else 58)
                or remote["target"] != binding["target"]
                or remote["input_db_sha256"] != binding["db"]["sha256"]
            ):
                raise SupervisedDenied("remote phase identity mismatch")
            if stage in {"backup", "validate"}:
                custody = remote["custody"]
                manifest, proof = custody["manifest"], custody["restore_receipt"]
                expected = dict(
                    operation=remote["operation"],
                    phase=stage,
                    target=binding["target"],
                    source_incarnation=binding["db"]["incarnation"],
                    input_db_sha256=binding["db"]["sha256"],
                    post_db_sha256=remote["db"]["sha256"],
                    manifest_sha256=custody["manifest_sha256"],
                    snapshot_sha256=manifest["sha256"],
                    state="held_restore",
                )
                if (
                    any(proof.get(k) != v for k, v in expected.items())
                    or proof["held_db"]["schema"] != remote["db"]["schema"]
                    or proof["held_db"]["incarnation"] == remote["db"]["incarnation"]
                    or hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()
                    != custody["restore_receipt_sha256"]
                ):
                    raise SupervisedDenied("generated restore proof lineage mismatch")
        else:
            if remote["metadata"]["name"] != (
                "tessera-control" if stage in {"scale_zero", "activate"} else binding["operation"]
            ):
                raise SupervisedDenied("remote object identity mismatch")
            if stage in {"scale_zero", "activate"}:
                if remote["metadata"]["uid"] != binding["target"]["deployment_uid"]:
                    raise SupervisedDenied("Deployment UID changed after CAS")
                argv = binding["command"]["argv"]
                patch = json.loads(argv[argv.index("--patch") + 1])
                spec = json.loads(json.dumps(patch[2]["value"]))
                spec["replicas"] = patch[3]["value"]
                spec["template"]["spec"]["containers"][0]["image"] = patch[4]["value"]
                if remote["spec"] != spec:
                    raise SupervisedDenied("CAS post-state mismatch")
            else:
                expected = json.loads(binding["command"]["stdin"])

                def contains(want, got):
                    if isinstance(want, dict):
                        return isinstance(got, dict) and all(
                            k in got and contains(v, got[k]) for k, v in want.items()
                        )
                    if isinstance(want, list):
                        return (
                            isinstance(got, list)
                            and len(want) == len(got)
                            and all(contains(a, b) for a, b in zip(want, got, strict=True))
                        )
                    return want == got

                if not contains(expected, remote):
                    raise SupervisedDenied("created object profile mismatch")

    def readback(self, binding):
        stage = binding["phase"]
        if stage in {"backup", "migrate", "validate"}:
            hold, retained = self.maintenance_hold(binding)
            code = (
                "from pathlib import Path;print(Path("
                + repr("/scratch/" + stage + ".result.json")
                + ").read_text())"
            )
            with hold.db_window(retained):
                remote = self.transport.call(
                    exec_command(binding["operation"], "old" if stage == "backup" else "new", code)
                )
            self.validate_remote(binding, remote)
            return {
                "observation": remote,
                "resubmit": False,
                "acknowledgement": "retained phase result",
            }
        kind = (
            "deployment"
            if stage in {"scale_zero", "activate"}
            else "networkpolicy"
            if stage == "network_policy"
            else "pod"
        )
        remote = self.transport.get(
            kind, "tessera-control" if kind == "deployment" else binding["operation"]
        )
        self.validate_remote(binding, remote)
        return {
            "metadata": {
                k: v
                for k, v in remote["metadata"].items()
                if k in {"name", "uid", "resourceVersion"}
            },
            "response_sha256": digest(remote),
            "resubmit": False,
            "acknowledgement": "observed post-state, not proof of original response",
        }
