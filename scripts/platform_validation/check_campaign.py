"""The platform campaign runner on REAL local Docker (ADR 0038), synthetic only.

Starts its own `tessera serve` (campaign tick 0.5 s, fixed port so it can be killed and
restarted) and `tessera-worker development-poll` subprocesses, writes a tiny stdlib-only
payload (`code/s1.py` ... `code/s3.py`) and drives three campaigns through the public HTTP
surface. Nothing here is experiment specific, calls a provider, or claims a scientific result.

Campaign A (budget cap 10 cents): s1 -> s2 (retry, reads s1 bytes) -> s3 and s4 (hold 6 each).
  staged-input        s2 read s1's exact bytes through /inputs/up/data.json
  transient-retried   the poller runs one grant (s1) only, so s2's first grant expires unleased
                      (t1_grant_expired_unleased); an unbounded poller then runs attempt 2
  over-budget-refused s3 reserved, s4 refused_budget, no grant for s4, stage_refused event
  restart-resumes     SIGKILL serve while s3 is admitted|running; restart; one grant for s3
  receipt             receipt digest recomputes; run receipt digests, lineage edge, measurement
Campaign B: a long-sleeping stage is cancelled while running; grant reconciled, no residue.
Campaign C: private-derived input plus egress is refused at submit (data-class-refused),
and a spec declaring the owner-adopted snapshot public is refused 409 (ADR 0039).

No local `python@sha256:` image or Docker is a GAP with the reason, never a pull. State reads
(`campaigns`, `campaign_stage_attempts`, events) are read-only SQLite queries on the check's
own throwaway state directory. The check's cost is local CPU only; budgets are accounting.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import (
    _BOOT,
    AUTOMATION_HEADER,
    AUTOMATION_LABEL,
    REPO,
    Context,
    Result,
    _later,
    scrub,
)
from .real_run import RealRun, _docker, local_python_image

CAPABILITY = "campaign-runner"
# Needs local Docker and a local pinned python image; run only with --only campaign.
OPT_IN = True
S1 = "import json; open('/outputs/data.json','w').write(json.dumps({'n': 21}))\n"
S2 = (
    "import hashlib, json\n"
    "raw = open('/inputs/up/data.json','rb').read()\n"
    "n = json.loads(raw)['n']\n"
    "open('/outputs/result.json','w').write(json.dumps("
    "{'score': n * 2, 'up_sha256': hashlib.sha256(raw).hexdigest()}))\n"
)
S3 = "import time\ntime.sleep(20)\nopen('/outputs/out.json','w').write('{}')\n"
TERMINAL = ("succeeded", "stopped", "failed", "cancelled")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Check:
    def __init__(self, ctx: Context, image: str) -> None:
        self.ctx, self.image = ctx, image
        self.rr = RealRun(ctx, image)
        self.port = 0
        self.poller: subprocess.Popen | None = None
        self.out: list[Result] = []

    def add(self, case: str, ok: bool | None, evidence: str) -> None:
        status = "GAP" if ok is None else "PASS" if ok else "FAIL"
        self.out.append(Result(CAPABILITY, case, status, evidence))

    # -- processes -----------------------------------------------------------
    def write_trust(self, providers: dict) -> Path:
        """The provider trust file (ADR 0040 section 4); owner-only, as serve requires."""
        path = self.ctx.state_dir.parent / "provider-trust.json"
        body = json.dumps({"schema": "tessera.provider-trust.v1", "providers": providers})
        # Created owner-only before anything is written, never widened then narrowed.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(body)
        path.chmod(0o600)  # an existing file keeps its old mode through O_CREAT
        return path

    def bind_worker_trust(self) -> None:
        """Bind the check's worker principal to local-docker for private_derived inputs.

        The default setting binds no principal, so a local stage fed by upstream outputs is
        refused. The principal only exists after setup, so the file is rewritten and the
        running service reloads it (a human decision).
        """
        self.write_trust(
            {
                "local-docker": {
                    "data_classes": ["public", "private_derived"],
                    "worker_principal_ids": [self.rr.worker_principal],
                }
            }
        )
        self.ctx.call(
            "POST",
            "/admin/provider-trust/reload",
            {},
            headers={AUTOMATION_HEADER: AUTOMATION_LABEL},
        )

    def start_serve(self) -> None:
        ctx = self.ctx
        ctx.init_state()
        trust_file = self.ctx.state_dir.parent / "provider-trust.json"
        if not trust_file.exists():
            self.write_trust({})
        if not self.port:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                self.port = probe.getsockname()[1]
        ctx.base_url = f"http://127.0.0.1:{self.port}"
        log = (ctx.state_dir.parent / "serve.log").open("ab")
        ctx.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _BOOT,
                "serve",
                "--state-dir",
                str(ctx.state_dir),
                "--port",
                str(self.port),
                "--campaign-tick-seconds",
                "0.5",
                "--provider-trust",
                str(trust_file),
            ],
            stdout=log,
            stderr=log,
            cwd=REPO,
        )
        log.close()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if ctx.process.poll() is not None:
                raise RuntimeError(f"serve exited {ctx.process.returncode}")
            try:
                with urllib.request.urlopen(ctx.base_url + "/health/ready", timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.2)
        raise RuntimeError("serve did not become ready in 60s")

    def start_poller(self, max_grants: int | None = None) -> None:
        self.stop_poller()
        rr = self.rr
        base = rr.root / "poller"
        workspace, outputs = base / "workspace", base / "outputs"
        for path in (base, workspace, outputs):
            path.mkdir(mode=0o700, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "tessera_worker.cli",
            "development-poll",
            "--control-url",
            self.ctx.base_url,
            "--credential-file",
            str(rr.credential_file),
            "--workspace-root",
            str(workspace),
            "--development-output-root",
            str(outputs),
            "--allowed-image",
            self.image,
            "--poll-seconds",
            "0.5",
        ]
        if max_grants is not None:
            command += ["--max-grants", str(max_grants)]
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(REPO / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        log = (rr.root / "poller.log").open("ab")
        self.poller = subprocess.Popen(command, stdout=log, stderr=log, cwd=REPO, env=env)
        log.close()

    def stop_poller(self) -> None:
        proc, self.poller = self.poller, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    # -- reads ---------------------------------------------------------------
    def q(self, sql: str, *params) -> list[tuple]:
        uri = f"file:{self.ctx.state_dir / 'tessera.sqlite3'}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=10) as db:
            return [tuple(r) for r in db.execute(sql, params).fetchall()]

    def wait(self, what: str, probe, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = probe()
            if value:
                return value
            time.sleep(0.5)
        raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")

    def dump(self, campaign_id: str) -> str:
        """Bounded diagnostic snapshot of a campaign, appended to timeout errors."""
        parts = []
        for label, sql in (
            ("campaign", "SELECT status, stop_reason FROM campaigns WHERE id=?"),
            (
                "attempts",
                "SELECT stage_key, attempt, status, failure_class, grant_id FROM "
                "campaign_stage_attempts WHERE campaign_id=? ORDER BY stage_key, attempt",
            ),
            (
                "intents_not_done",
                "SELECT stage_key, attempt, step, status, error_class, response_status FROM "
                "campaign_step_intents WHERE campaign_id=? AND status!='done'",
            ),
            (
                "intent_steps_done",
                "SELECT step, count(*) FROM campaign_step_intents WHERE campaign_id=? "
                "AND status='done' GROUP BY step",
            ),
            (
                "leases",
                "SELECT grant_id, epoch, issued_at, expires_at, hard_expires_at FROM "
                "execution_leases WHERE grant_id IN (SELECT grant_id FROM "
                "campaign_stage_attempts WHERE campaign_id=? AND grant_id IS NOT NULL)",
            ),
            (
                "grants",
                "SELECT g.id, g.status, g.epoch, g.expires_at, g.snapshot_release_id FROM "
                "execution_grants g WHERE g.id IN (SELECT grant_id FROM "
                "campaign_stage_attempts WHERE campaign_id=? AND grant_id IS NOT NULL)",
            ),
        ):
            try:
                parts.append(f"{label}={self.q(sql, campaign_id)[:20]}")
            except Exception as error:  # noqa: BLE001
                parts.append(f"{label}=<{type(error).__name__}: {error}>")
        try:
            events = self.q(
                "SELECT event_type, substr(payload_json,1,160) FROM events "
                "ORDER BY cursor DESC LIMIT 12"
            )
            parts.append(f"events={events}")
        except Exception as error:  # noqa: BLE001
            parts.append(f"events=<{error}>")
        try:
            base = self.rr.root / "poller" / "workspace" / "grants"
            tree = sorted(
                str(path.relative_to(base))
                for path in base.rglob("*")
                if len(path.relative_to(base).parts) <= 4
            )
            parts.append(f"poller_tree={tree[:60]}")
        except OSError as error:
            parts.append(f"poller_tree=<{error}>")
        parts.append(f"now={time.strftime('%H:%M:%S', time.gmtime())}Z")
        proc = self.poller
        parts.append(f"poller_returncode={None if proc is None else proc.poll()}")
        try:
            tail = (self.rr.root / "poller.log").read_bytes()[-1500:].decode("utf-8", "replace")
            parts.append(f"poller_log_tail={tail!r}")
        except OSError as error:
            parts.append(f"poller_log_tail=<{error}>")
        return "; ".join(parts)[:6000]

    def status(self, campaign_id: str) -> str:
        rows = self.q("SELECT status FROM campaigns WHERE id=?", campaign_id)
        return rows[0][0] if rows else ""

    def attempts(self, campaign_id: str, stage: str) -> list[dict]:
        rows = self.q(
            "SELECT attempt, status, failure_class, grant_id, operation_id, exit_code, "
            "run_receipt_digest, admitted_attempt_id FROM campaign_stage_attempts "
            "WHERE campaign_id=? AND stage_key=? ORDER BY attempt",
            campaign_id,
            stage,
        )
        keys = (
            "attempt status failure_class grant_id operation_id exit_code "
            "run_receipt_digest admitted_attempt_id"
        ).split()
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def note_operations(self, campaign_id: str) -> None:
        for (op,) in self.q(
            "SELECT operation_id FROM campaign_stage_attempts "
            "WHERE campaign_id=? AND operation_id IS NOT NULL",
            campaign_id,
        ):
            if op not in self.rr.operations:
                self.rr.operations.append(op)

    # -- control calls ---------------------------------------------------------
    def setup(self) -> dict:
        rr, call = self.rr, self.ctx.call
        rr.project = call("POST", "/projects", {"name": "Campaign runner check"})
        pid = rr.project["id"]
        credential = call(
            "POST",
            "/credentials",
            {
                "kind": "worker",
                "audience": "worker",
                "token": rr._token,
                "project_id": pid,
                "expires_at": _later(90),
            },
        )
        rr.worker_principal = credential["principal_id"]
        rr.credential_file.write_text(rr._token)
        rr.credential_file.chmod(0o600)
        call(
            "POST",
            f"/projects/{pid}/workers",
            {
                "worker_principal_id": rr.worker_principal,
                "approved_build_identifier": "synthetic-pv-campaign",
                "approved_build_digest": "d" * 64,
                "trust_class": "test",
                "region": "local",
            },
        )
        self.bind_worker_trust()
        code = rr.root / "code"
        code.mkdir()
        for name, text in (("s1.py", S1), ("s2.py", S2), ("s3.py", S3)):
            (code / name).write_text(text)
        inventory = json.loads(self.ctx.cli("workspace", "inventory", "--root", str(code)))
        snapshot = json.loads(
            self.ctx.cli(
                "workspace",
                "adopt",
                "--root",
                str(code),
                "--state-dir",
                str(self.ctx.state_dir),
                "--project-id",
                pid,
                "--expected-digest",
                inventory["digest"],
                "--idempotency-key",
                "pv-campaign-code",
            )
        )
        call(
            "PUT",
            f"/projects/{pid}/execution-limits",
            {
                "expected_revision": 0,
                "currency": "USD",
                "monetary_limit_cents": 20,
                "max_concurrency": 2,
            },
        )
        return snapshot

    def spec(self, snapshot: dict, name: str, cap: int, stages: list[dict]) -> dict:
        return {
            "version": 1,
            "name": name,
            "budget_cap_cents": cap,
            "deadline_seconds": 1800,
            "worker_principal_id": self.rr.worker_principal,
            "stages": stages,
        }

    def stage(self, key: str, argv: list[str], inputs: list[dict], outputs: list[str], **extra):
        return {
            "key": key,
            "backend": "docker-local-cpu-v1",
            "image": self.image,
            "argv": argv,
            "output_paths": outputs,
            "max_output_bytes": 65536,
            "max_duration_seconds": 120,
            "inputs": inputs,
            **extra,
        }

    @staticmethod
    def code_input(snapshot: dict, data_class: str = "private_derived") -> dict:
        # ADR 0039: the CLI adopts with the bare owner credential, which can only record
        # private_derived; a spec declaring it public is refused (409). Local CPU stages
        # accept private_derived inputs (released as private_derived).
        return {
            "kind": "snapshot",
            "name": "code",
            "snapshot_id": snapshot["id"],
            "digest": snapshot["digest"],
            "data_class": data_class,
        }

    def submit(self, spec: dict, expected=(200, 201)) -> tuple[int, dict]:
        pid = self.rr.project["id"]
        status, body = self.ctx.request(
            "POST",
            f"/projects/{pid}/campaigns",
            {"spec": spec},
            idempotency_key=self.ctx.new_key(),
        )
        if status not in expected:
            raise RuntimeError(scrub(f"submit -> {status}: {body}"))
        return status, body

    def approve(self, campaign: dict) -> dict:
        digest = campaign.get("spec_digest")
        return self.ctx.call(
            "POST",
            f"/campaigns/{campaign['id']}/approve",
            {"expected_revision": campaign["revision"], "expected_spec_digest": digest},
            headers={AUTOMATION_HEADER: AUTOMATION_LABEL},
        )

    # -- the cases ---------------------------------------------------------------
    def campaign_a(self, snapshot: dict) -> str:
        code = self.code_input(snapshot)
        up = {"kind": "stage", "name": "up", "stage": "s2", "paths": ["result.json"]}
        spec = self.spec(
            snapshot,
            "pv campaign A",
            10,
            [
                self.stage(
                    "s1",
                    ["python", "/inputs/s1.py"],
                    [code],
                    ["data.json"],
                    max_exposure_cents=2,
                ),
                self.stage(
                    "s2",
                    ["python", "/inputs/code/s2.py"],
                    [
                        code,
                        {"kind": "stage", "name": "up", "stage": "s1", "paths": ["data.json"]},
                    ],
                    ["result.json"],
                    max_exposure_cents=2,
                    grant_ttl_seconds=60,
                    retry={"max_attempts": 2},
                    metrics=[{"name": "score", "path": "result.json", "json_pointer": "/score"}],
                ),
                self.stage(
                    "s3",
                    ["python", "/inputs/code/s3.py"],
                    [code, up],
                    ["out.json"],
                    max_exposure_cents=6,
                ),
                self.stage(
                    "s4",
                    ["python", "/inputs/code/s3.py"],
                    [code, up],
                    ["out.json"],
                    max_exposure_cents=6,
                ),
            ],
        )
        _, campaign = self.submit(spec)
        self.approve(campaign)
        cid = campaign["id"]
        self.start_poller(max_grants=1)  # s1 only; s2's first grant then expires unleased
        self.wait(
            "s1 succeeded",
            lambda: (
                self.attempts(cid, "s1")[:1]
                and self.attempts(cid, "s1")[0]["status"] == "succeeded"
            ),
            240,
        )
        first = self.wait(
            "s2 attempt 1 failed_transient",
            lambda: next(
                (a for a in self.attempts(cid, "s2") if a["status"] == "failed_transient"), None
            ),
            300,
        )
        transient_ok = first["failure_class"] == "t1_grant_expired_unleased"
        self.start_poller()  # unbounded
        self.wait(
            "s2 succeeded",
            lambda: any(a["status"] == "succeeded" for a in self.attempts(cid, "s2")),
            240,
        )
        s2 = self.attempts(cid, "s2")
        grants = [a["grant_id"] for a in s2 if a["grant_id"]]
        self.add(
            "transient-retried",
            transient_ok and len(s2) == 2 and len(set(grants)) == 2,
            f"attempt1={first['status']}/{first['failure_class']} attempts={len(s2)} "
            f"grants_for_s2={len(set(grants))}",
        )
        self.note_operations(cid)
        self.staged_input_case(cid)
        self.budget_case(cid)
        self.restart_case(cid)
        return cid

    def manifest(self, operation_id: str) -> dict | None:
        return self.rr.manifest_for(operation_id)

    def staged_input_case(self, cid: str) -> None:
        s1 = next(a for a in self.attempts(cid, "s1") if a["status"] == "succeeded")
        s2 = next(a for a in self.attempts(cid, "s2") if a["status"] == "succeeded")
        m1, m2 = self.manifest(s1["operation_id"]), self.manifest(s2["operation_id"])
        if not (m1 and m2):
            self.add("staged-input", False, "no artifact manifest for s1/s2 operation")
            return
        data = self.rr.fetch_artifact(m1, "data.json")
        result = json.loads(self.rr.fetch_artifact(m2, "result.json"))
        entry = next(a for a in m1["artifacts"] if a["relative_path"] == "data.json")
        self.add(
            "staged-input",
            result.get("up_sha256") == _sha(data) == entry.get("sha256", _sha(data)),
            f"s2 saw sha256 {str(result.get('up_sha256'))[:16]}; s1 manifest "
            f"{_sha(data)[:16]}; score={result.get('score')}",
        )
        self._s1_bytes, self._s2_result = data, result
        self._manifests = {"s1": m1, "s2": m2}

    def budget_case(self, cid: str) -> None:
        s3 = self.wait("s3 attempt", lambda: self.attempts(cid, "s3"), 120)
        s4 = self.wait("s4 attempt", lambda: self.attempts(cid, "s4"), 120)
        refused = [a for a in s4 if a["status"] == "refused_budget"]
        events = self.q("SELECT count(*) FROM events WHERE event_type='campaign.stage_refused'")
        self.add(
            "over-budget-refused",
            bool(s3)
            and len(refused) == 1
            and refused[0]["grant_id"] is None
            and refused[0]["admitted_attempt_id"] is None
            and events[0][0] >= 1,
            f"s3={[a['status'] for a in s3]} s4={[a['status'] for a in s4]} "
            f"stage_refused_events={events[0][0]}",
        )

    def restart_case(self, cid: str) -> None:
        self.wait(
            "s3 admitted|running",
            lambda: any(a["status"] in ("admitted", "running") for a in self.attempts(cid, "s3")),
            120,
        )
        self.ctx.stop_service(kill=True)
        self.start_serve()
        if self.poller is None or self.poller.poll() is not None:
            self.start_poller()
        try:
            terminal = self.wait("campaign A terminal", lambda: self.status(cid) in TERMINAL, 300)
        except TimeoutError as error:
            detail = self.dump(cid)
            print(f"campaign A timeout diagnostics: {detail}", file=sys.stderr)
            raise TimeoutError(f"{error} :: {detail}") from error
        state = self.status(cid)
        reason = self.q("SELECT stop_reason FROM campaigns WHERE id=?", cid)[0][0]
        s3 = self.attempts(cid, "s3")
        pending = self.q(
            "SELECT count(*) FROM campaign_step_intents WHERE campaign_id=? AND status='pending'",
            cid,
        )[0][0]
        keys = self.q(
            "SELECT count(*), count(DISTINCT idempotency_key) FROM campaign_step_intents "
            "WHERE campaign_id=?",
            cid,
        )[0]
        self.add(
            "restart-resumes",
            bool(terminal)
            and state == "stopped"
            and len([a for a in s3 if a["grant_id"]]) == 1
            and pending == 0
            and keys[0] == keys[1],
            f"status={state} reason={reason} s3_grants={len([a for a in s3 if a['grant_id']])} "
            f"pending_intents={pending} intents={keys[0]} distinct_keys={keys[1]}",
        )
        self.note_operations(cid)

    def receipt_case(self, cid: str) -> None:
        sys.path.insert(0, str(REPO / "src"))
        from tessera_control.store import canonical_digest

        body = self.ctx.call("GET", f"/campaigns/{cid}/receipt")
        receipt = body.get("receipt", body)
        claimed = body.get("receipt_digest") or receipt.get("receipt_digest")
        bare = {k: v for k, v in receipt.items() if k != "receipt_digest"}
        problems = []
        if claimed not in (canonical_digest(bare), canonical_digest(receipt)):
            problems.append("receipt digest does not recompute")
        succeeded = [
            attempt
            for stage in receipt.get("stages", [])
            for attempt in stage.get("attempts", [])
            if attempt.get("status") == "succeeded"
        ]
        if not succeeded:
            problems.append("no succeeded attempt in receipt")
        for attempt in succeeded:
            run = self.ctx.call(
                "GET", f"/external-operations/{attempt['operation_id']}/run-receipt"
            )
            digests = {run.get("digest"), run.get("receipt_digest")}
            if attempt.get("run_receipt_digest") not in digests:
                problems.append(f"run receipt digest mismatch for {attempt['operation_id'][:12]}")
        edges = (receipt.get("lineage") or {}).get("edge_ids") or []
        export = self.ctx.call("GET", f"/projects/{self.rr.project['id']}/artifact-lineage/export")
        known = {e.get("id") for e in export.get("edges", [])}
        if not edges or not set(edges) <= known:
            problems.append("lineage edge s2-output derived_from s1-output not found")
        measured = [
            m
            for stage in receipt.get("stages", [])
            if stage.get("key", stage.get("stage_key")) == "s2"
            for m in (stage.get("measurements") or [])
            + [m for a in stage.get("attempts", []) for m in (a.get("measurements") or [])]
        ]
        score = getattr(self, "_s2_result", {}).get("score")
        if not measured or any(m.get("value") != score for m in measured):
            problems.append(f"measurement {measured!r:.80} != bytes score {score}")
        verdict = (receipt.get("verdict") or {}).get("scientific_verdict")
        if verdict in ("pass", "passed", "verified", "confirmed"):
            problems.append(f"scientific_verdict is {verdict}")
        self.add(
            "receipt",
            not problems,
            "; ".join(problems)
            or f"digest recomputes; {len(succeeded)} run receipts match; "
            f"scientific_verdict={verdict}",
        )

    def campaign_b(self, snapshot: dict) -> None:
        sleeper = self.stage(
            "long",
            ["python", "-c", "import time; time.sleep(120)"],
            [self.code_input(snapshot)],
            ["never.json"],
            max_exposure_cents=2,
        )
        _, campaign = self.submit(self.spec(snapshot, "pv campaign B", 4, [sleeper]))
        self.approve(campaign)
        cid = campaign["id"]
        self.wait(
            "long stage running",
            lambda: any(a["status"] == "running" for a in self.attempts(cid, "long")),
            180,
        )
        current = self.ctx.call("GET", f"/campaigns/{cid}")
        current = current.get("campaign", current)
        self.ctx.call(
            "POST",
            f"/campaigns/{cid}/cancel",
            {"expected_revision": current["revision"], "reason": "pv: cancel while running"},
        )
        self.wait("campaign B terminal", lambda: self.status(cid) in TERMINAL, 240)
        self.note_operations(cid)
        attempt = self.attempts(cid, "long")[0]
        grant = self.ctx.call("GET", f"/execution-grants/{attempt['grant_id']}")
        left = self.rr.residue()
        running = [
            item
            for item in left
            if item.startswith("containers:")
            and _docker(
                "inspect", "--format", "{{.State.Running}}", item.split(":", 1)[1]
            ).stdout.strip()
            == "true"
        ]
        others = [x for x in left if not x.startswith("containers:")]
        self.add(
            "cancel-running",
            self.status(cid) == "cancelled"
            and str(grant.get("status")) != "active"
            and not running
            and not others,
            f"campaign={self.status(cid)} grant={grant.get('status')} "
            f"running_containers={len(running)} other_residue={others}",
        )

    def campaign_c(self, snapshot: dict) -> None:
        stage = self.stage(
            "leak",
            ["python", "/inputs/s1.py"],
            [self.code_input(snapshot, "private_derived")],
            ["data.json"],
            egress={
                "profile": "allowlist-proxy-v1",
                "destinations": [{"host": "example.com", "port": 80}],
                "max_egress_bytes": 1024,
            },
        )
        spec = self.spec(snapshot, "pv campaign C", 2, [stage])
        status, body = self.ctx.request(
            "POST",
            f"/projects/{self.rr.project['id']}/campaigns",
            {"spec": spec},
            idempotency_key=self.ctx.new_key(),
        )
        self.add("data-class-refused", status in (409, 422), f"submit -> {status}")
        public_spec = self.spec(
            snapshot,
            "pv campaign C public claim",
            2,
            [
                self.stage(
                    "claim",
                    ["python", "/inputs/s1.py"],
                    [self.code_input(snapshot, "public")],
                    ["data.json"],
                )
            ],
        )
        status, _body = self.ctx.request(
            "POST",
            f"/projects/{self.rr.project['id']}/campaigns",
            {"spec": public_spec},
            idempotency_key=self.ctx.new_key(),
        )
        self.add("public-claim-refused", status == 409, f"submit -> {status}")


def _v2_stage(check: Check, snapshot: dict, key: str, inputs: list[dict], **extra) -> dict:
    stage = {
        "key": key,
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": check.image,
        "argv": ["python", "/inputs/s1.py"],
        "max_duration_seconds": 120,
        "max_exposure_cents": 2,
        "inputs": inputs,
        "outputs": [{"name": "data", "path": "data.json", "max_bytes": 65536}],
        **extra,
    }
    return stage


def campaign_d(check: Check, snapshot: dict) -> str:
    """A version-2 spec: typed outputs, a stop rule and a skipped later stage."""
    first = _v2_stage(
        check,
        snapshot,
        "d1",
        [check.code_input(snapshot)],
        metrics=[{"name": "n", "path": "data.json", "json_pointer": "/n"}],
        stop_when=[{"metric": "n", "op": "gt", "value": 20}],
    )
    second = _v2_stage(
        check,
        snapshot,
        "d2",
        [
            check.code_input(snapshot),
            {"kind": "stage", "name": "up", "stage": "d1", "outputs": ["data"]},
        ],
    )
    spec = {
        "version": 2,
        "name": "pv campaign D",
        "budget_cap_cents": 6,
        "deadline_seconds": 1800,
        "stages": [first, second],
    }
    _, campaign = check.submit(spec)
    check.approve(campaign)
    cid = campaign["id"]
    check.start_poller()
    check.wait("campaign D terminal", lambda: check.status(cid) in TERMINAL, 300)
    stages = {
        row[0]: row[1]
        for row in check.q("SELECT stage_key, status FROM campaign_stages WHERE campaign_id=?", cid)
    }
    check.add(
        "v2-stop-rule",
        check.status(cid) == "stopped" and stages == {"d1": "succeeded", "d2": "skipped"},
        f"campaign={check.status(cid)} stages={stages}",
    )
    return cid


def campaign_e(check: Check, snapshot: dict, source_campaign: str) -> None:
    """A later campaign pins campaign D's recorded output by sha256 (kind: artifact)."""
    rows = check.q(
        "SELECT sha256 FROM campaign_stage_io WHERE campaign_id=? AND stage_key='d1' "
        "AND direction='output' AND name='data'",
        source_campaign,
    )
    if not rows:
        check.add("artifact-chain", False, "campaign D recorded no d1 output to pin")
        return
    digest = rows[0][0]
    stage = _v2_stage(
        check,
        snapshot,
        "e1",
        [
            check.code_input(snapshot),
            {
                "kind": "artifact",
                "name": "prior",
                "campaign_id": source_campaign,
                "stage": "d1",
                "output": "data",
                "sha256": digest,
            },
        ],
    )
    spec = {
        "version": 2,
        "name": "pv campaign E",
        "budget_cap_cents": 3,
        "deadline_seconds": 1800,
        "stages": [stage],
    }
    _, campaign = check.submit(spec)
    check.approve(campaign)
    cid = campaign["id"]
    check.wait("campaign E terminal", lambda: check.status(cid) in TERMINAL, 300)
    pinned = [
        row[0]
        for row in check.q(
            "SELECT sha256 FROM campaign_stage_io WHERE campaign_id=? AND name='prior' "
            "AND direction='input'",
            cid,
        )
    ]
    check.add(
        "artifact-chain",
        check.status(cid) == "succeeded" and pinned == [digest],
        f"campaign={check.status(cid)} pinned={pinned} expected={digest}",
    )


def run(ctx: Context) -> list[Result]:
    if shutil.which("docker") is None:
        return [Result(CAPABILITY, "prerequisites", "GAP", "docker CLI not found; not run")]
    image = local_python_image()
    if image is None:
        return [
            Result(CAPABILITY, "prerequisites", "GAP", "no local python@sha256 image; never pulls")
        ]
    check = Check(ctx, image)
    try:
        check.start_serve()
        snapshot = check.setup()
        cid = check.campaign_a(snapshot)
        check.receipt_case(cid)
        check.campaign_b(snapshot)
        check.campaign_c(snapshot)
        source = campaign_d(check, snapshot)
        campaign_e(check, snapshot, source)
    except Exception as error:  # noqa: BLE001 - keep earlier results, report the crash
        check.add("driver-error", False, f"{type(error).__name__}: {error}")
    finally:
        check.stop_poller()
        left = check.rr.cleanup()
        retained = [x for x in left if x.startswith("containers:tessera-op-")]
        other = [x for x in left if x not in retained]
        check.add(
            "docker-residue",
            not other,
            f"{len(retained)} exited workload container(s) retained by the worker were removed "
            f"by exact name; other residue: {other}",
        )
    return check.out
