"""Items 1b and 2: live allowlisted egress and dataset staging, on local Docker only.

Two REAL runs through the public surface (see `real_run.py`):

* run 1 (profile local-cpu, egress allowlist example.com:80): the workload connects
  through the egress proxy (HTTP CONNECT) to example.com:80 (allowed) and example.org:80
  (not allowlisted), and writes what it observed to /outputs/egress.json plus the allowed
  response body to /outputs/page.html. PASS only if the workload's own observation AND the
  control-plane recorded `execution.egress_journal_recorded` event agree.
* run 2 (network none, no egress): a human retains and custody-releases run 1's manifest,
  stages page.html as a snapshot, releases it, and a second task reads it from /inputs and
  writes its length and sha256. PASS only if run 2's output bytes carry the same sha256 as
  run 1's page.html.

The same workload also tries to BYPASS the proxy: one direct TCP connect (no proxy) to the IP
literal 1.1.1.1:80 and one direct `socket.getaddrinfo("example.com")`. Case 1b passes only if
both of those FAIL; if either succeeds that is a real isolation FAIL with the exact
observation, and if the workload could not tell (unexpected error) it is a GAP with the error.

ALL live traffic is gated behind TESSERA_PV_LIVE_EGRESS=1 (like TESSERA_PV_WEAK_ATTESTATION):
without it the egress cases are GAP and no worker run or network request is made. With it,
the traffic is two proxied HTTP requests (example.com, example.org) plus the one direct
connect attempt and one direct DNS lookup.

No internet, or a missing local image/docker, is a GAP with the exact reason, never a fake
pass. Live traffic is two small requests per run. No pull, build or prune; only resources
named after this driver's operations are checked and removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil

from .common import Context, Result
from .real_run import RealRun, local_python_image

CAPABILITY = "real-run"
ALLOWED, DENIED = ("example.com", 80), ("example.org", 80)
MAX_EGRESS_BYTES = 65536
LIVE_ENV = "TESSERA_PV_LIVE_EGRESS"
NOT_RUN = f"not run: set {LIVE_ENV}=1 (sends two HTTP requests to example.com / example.org)"
DIRECT_IP = "1.1.1.1"

EGRESS_WORKLOAD = r"""
import errno, json, os, signal, socket, urllib.parse, hashlib
proxy = urllib.parse.urlparse(os.environ["HTTP_PROXY"])

def head(sock):
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 65536:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    first, _, rest = buf.partition(b"\r\n\r\n")
    return first, rest

def connect(host, port, fetch):
    seen = {"host": host, "port": port}
    body = b""
    try:
        sock = socket.create_connection((proxy.hostname, proxy.port), timeout=15)
        sock.settimeout(15)
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        first, _ = head(sock)
        seen["connect_status"] = int(first.split()[1])
        if seen["connect_status"] == 200 and fetch:
            sock.sendall(
                f"GET / HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
            )
            data = b""
            while len(data) < 65536:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                data += chunk
            first, _, body = data.partition(b"\r\n\r\n")
            seen["http_status"] = int(first.split()[1])
            seen["body_bytes"] = len(body)
            seen["body_sha256"] = hashlib.sha256(body).hexdigest()
        sock.close()
    except Exception as error:
        seen["error"] = type(error).__name__ + ": " + str(error)[:120]
    return seen, body

def direct_connect():
    seen = {"target": "1.1.1.1:80"}
    try:
        sock = socket.create_connection(("1.1.1.1", 80), timeout=5)
        seen["connected"] = True
        sock.close()
    except OSError as error:
        seen["connected"] = False
        seen["error_class"] = type(error).__name__
        seen["errno"] = error.errno
        # Symbolic name from the container's own (Linux) errno table; a timeout has no errno.
        seen["errno_name"] = (
            "TIMEOUT" if isinstance(error, TimeoutError) else errno.errorcode.get(error.errno)
        )
        seen["error"] = str(error)[:120]
    except Exception as error:
        seen["connected"] = None
        seen["error_class"] = type(error).__name__
        seen["error"] = str(error)[:120]
    return seen

class _Alarm(Exception):
    pass

def _raise_alarm(*_):
    raise _Alarm()

def direct_dns():
    seen = {"name": "example.com"}
    # socket.setdefaulttimeout would change the timeout of every later socket, including the
    # proxied connections, so it is not used. The alarm is a best-effort bound: its handler
    # runs when the interpreter regains control, so a resolver call that blocks inside C may
    # not be interrupted; the lookup is then bounded by the resolver's own timeout and by the
    # harness's worker timeout.
    signal.signal(signal.SIGALRM, _raise_alarm)
    signal.alarm(8)
    try:
        info = socket.getaddrinfo("example.com", 80, proto=socket.IPPROTO_TCP)
        seen["resolved"] = True
        seen["addresses"] = sorted({i[4][0] for i in info})[:4]
    except socket.gaierror as error:
        seen["resolved"] = False
        seen["error_class"] = "gaierror"
        seen["errno"] = error.errno
        seen["error"] = str(error)[:120]
    except _Alarm:
        seen["resolved"] = None
        seen["error_class"] = "lookup-timeout"
        seen["error"] = "no answer within 8 seconds"
    except Exception as error:
        seen["resolved"] = None
        seen["error_class"] = type(error).__name__
        seen["error"] = str(error)[:120]
    finally:
        signal.alarm(0)
    return seen

direct = {"connect": direct_connect(), "dns": direct_dns()}
allowed, body = connect("example.com", 80, True)
denied, _ = connect("example.org", 80, False)
open("/outputs/page.html", "wb").write(body)
report = {"allowed": allowed, "denied": denied, "direct": direct}
open("/outputs/egress.json", "w").write(json.dumps(report))
"""

STAGED_WORKLOAD = r"""
import hashlib, json
data = open("/inputs/page.html", "rb").read()
open("/outputs/result.json", "w").write(
    json.dumps({"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
)
"""


# A direct connect is "blocked" only when the kernel reports no route (the network is
# unreachable) or nothing answers (timeout). A refused or reset connection means packets
# reached a remote host, which is the very bypass this check exists to catch.
_BLOCKED = frozenset({"ENETUNREACH", "EHOSTUNREACH", "TIMEOUT"})
_REACHED = frozenset({"ECONNREFUSED", "ECONNRESET"})


def classify_direct_connect(obs: dict) -> str:
    """'blocked', 'reached' or 'undecided' for the workload's direct-connect observation."""
    if obs.get("connected") is True:
        return "reached"
    if obs.get("connected") is not False:
        return "undecided"
    name = obs.get("errno_name")
    if name in _BLOCKED:
        return "blocked"
    if name in _REACHED:
        return "reached"
    return "undecided"


def _short(value: object, limit: int = 160) -> str:
    return json.dumps(value, sort_keys=True)[:limit]


def _journal_problems(event: dict) -> list[str]:
    """Self-consistency of the recorded summary and the expected allow/deny rows."""
    summary, problems = event["summary"], []
    rows = summary["destinations"]
    allow = [r for r in rows if r["decision"] == "allow"]
    deny = [r for r in rows if r["decision"] == "deny"]
    if not any((r["host"], r["port"]) == ALLOWED for r in allow):
        problems.append("no allow row for example.com:80")
    if not any((r["host"], r["port"]) == DENIED for r in deny):
        problems.append("no deny row for example.org:80")
    if any((r["host"], r["port"]) not in (ALLOWED, DENIED) for r in rows):
        problems.append("journal has a destination beyond the two requested")
    if summary["connections_allowed"] != sum(r["connections"] for r in allow):
        problems.append("connections_allowed != sum of allow rows")
    if summary["connections_denied"] != sum(r["connections"] for r in deny):
        problems.append("connections_denied != sum of deny rows")
    if summary["bytes_up"] != sum(r["bytes_up"] for r in rows) or summary["bytes_down"] != sum(
        r["bytes_down"] for r in rows
    ):
        problems.append("byte totals != sum over destinations")
    if event.get("cap_exceeded") is not False:
        problems.append(f"cap_exceeded={event.get('cap_exceeded')}")
    if event.get("network_profile") != "allowlist-proxy-v1":
        problems.append("network_profile is not allowlist-proxy-v1")
    return problems


def run(ctx: Context) -> list[Result]:
    out: list[Result] = []

    def add(case: str, status: str, evidence: str) -> None:
        out.append(Result(CAPABILITY, case, status, evidence))

    if shutil.which("docker") is None:
        return [Result(CAPABILITY, "prerequisites", "GAP", "docker CLI not found; not run")]
    image = local_python_image()
    if image is None:
        return [
            Result(CAPABILITY, "prerequisites", "GAP", "no local python@sha256 image; never pulls")
        ]
    if os.environ.get(LIVE_ENV) != "1":
        for case in ("egress-live-run", "dataset-staging", "dataset-staged-input-run"):
            add(case, "GAP", NOT_RUN)
        add("docker-residue", "GAP", NOT_RUN)
        return out
    ctx.start_service()
    driver = RealRun(ctx, image)
    try:
        driver.setup()
        egress = {
            "profile": "allowlist-proxy-v1",
            "destinations": [{"host": ALLOWED[0], "port": ALLOWED[1]}],
            "max_egress_bytes": MAX_EGRESS_BYTES,
        }
        run1 = driver.run_task(
            "egress allowlist run",
            EGRESS_WORKLOAD,
            output_paths=["egress.json", "page.html"],
            egress=egress,
            proxy_image=image,
        )
        page = _egress_case(driver, run1, add)
        if page is not None:
            _staging_cases(driver, run1, page, add)
    except Exception as error:  # noqa: BLE001 - keep earlier results, report the crash
        add("driver-error", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        left = driver.cleanup()
        status, evidence = driver.residue_report(left)
        add("docker-residue", status, evidence)
    return out


def _egress_case(driver: RealRun, run1, add) -> bytes | None:
    """Case 1b. Returns the allowed page bytes the workload collected, or None."""
    if not run1.quarantined:
        add(
            "egress-live-run",
            "FAIL",
            f"worker rc={run1.worker_returncode} state={run1.worker.get('state')} "
            f"reason={run1.worker.get('reason')} detail={run1.worker.get('detail')} "
            f"stderr={run1.worker_stderr}",
        )
        return None
    manifest = driver.manifest_for(run1.operation_id)
    if manifest is None:
        add("egress-live-run", "FAIL", "worker quarantined outputs but no artifact manifest listed")
        return None
    driver.retain_and_release(manifest)
    observed = json.loads(driver.fetch_artifact(manifest, "egress.json"))
    page = driver.fetch_artifact(manifest, "page.html")
    allowed, denied = observed["allowed"], observed["denied"]
    event = driver.egress_event(run1.operation_id)
    if "error" in allowed and "connect_status" not in allowed:
        add(
            "egress-live-run",
            "GAP",
            f"workload could not reach the proxy/example.com: {allowed['error']}; "
            f"journal_recorded={event is not None}",
        )
        return None
    problems = []
    if allowed.get("connect_status") != 200 or allowed.get("http_status") != 200:
        problems.append(f"allowed observation {_short(allowed)}")
    elif allowed["body_sha256"] != hashlib.sha256(page).hexdigest() or not page:
        problems.append("page.html does not match the workload's own digest")
    if denied.get("connect_status") != 403:
        problems.append(f"denied observation {_short(denied)}")
    if event is None:
        problems.append("no execution.egress_journal_recorded event for the operation")
    else:
        problems += _journal_problems(event)
    proxied_ok = not problems
    direct = observed.get("direct") or {}
    connect_obs, dns_obs = direct.get("connect") or {}, direct.get("dns") or {}
    unknown = []
    verdict = classify_direct_connect(connect_obs)
    if verdict == "reached":
        problems.append(
            f"ISOLATION FINDING: direct TCP connect to {DIRECT_IP}:80 without the proxy "
            f"reached the network ({_short(connect_obs)})"
        )
    elif verdict != "blocked":
        unknown.append(
            f"direct connect undecided: {_short(connect_obs) if connect_obs else 'absent'}"
        )
    if dns_obs.get("resolved") is True:
        problems.append(
            "ISOLATION FINDING: direct getaddrinfo('example.com') without the proxy "
            f"SUCCEEDED ({_short(dns_obs)})"
        )
    elif dns_obs.get("resolved") is not False:
        unknown.append(f"direct DNS undecided: {_short(dns_obs) if dns_obs else 'absent'}")
    if problems:
        add("egress-live-run", "FAIL", "; ".join(problems))
        return page if proxied_ok else None
    if unknown:
        add("egress-live-run", "GAP", "; ".join(unknown))
        return page
    direct_note = (
        f"direct bypass attempts both failed: connect {DIRECT_IP}:80 -> "
        f"{connect_obs.get('error_class')} {connect_obs.get('errno_name')} "
        f"({connect_obs.get('error')}); getaddrinfo('example.com') -> "
        f"{dns_obs.get('error_class')} errno={dns_obs.get('errno')} ({dns_obs.get('error')}); "
    )
    rows = [
        f"{r['decision']} {r['host']}:{r['port']} x{r['connections']} "
        f"up={r['bytes_up']} down={r['bytes_down']}"
        for r in event["summary"]["destinations"]
    ]
    add(
        "egress-live-run",
        "PASS",
        f"workload: example.com:80 CONNECT 200 then HTTP 200 ({allowed['body_bytes']} body bytes, "
        f"sha256 {allowed['body_sha256'][:12]}); example.org:80 CONNECT 403; {direct_note}"
        "Recorded event "
        f"journal_sha256={event['journal_sha256'][:16]} cap_exceeded=false rows=[{'; '.join(rows)}]"
        " (worker-reported journal, not independent proof)",
    )
    return page


def _staging_cases(driver: RealRun, run1, page: bytes, add) -> None:
    """Case 2: stage run 1's page.html and prove the same bytes reach a second run."""
    manifest = driver.manifest_for(run1.operation_id)
    try:
        snapshot = driver.stage(manifest, ["page.html"])
    except RuntimeError as error:
        add("dataset-staging", "FAIL", str(error))
        return
    record = driver.staging_record(snapshot["id"])
    files = record.get("files", [])
    page_sha = hashlib.sha256(page).hexdigest()
    ok = record.get("manifest_id") == manifest["id"] and [f.get("sha256") for f in files] == [
        page_sha
    ]
    add(
        "dataset-staging",
        "PASS" if ok else "FAIL",
        f"staged snapshot {snapshot['id'][-8:]} digest {snapshot['digest'][:12]} (expected digest "
        "derived in-process via control's staged_inventory/inventory_digest); "
        f"staging record manifest_id={'matches' if ok else 'MISMATCH'} files={_short(files, 120)}",
    )
    if not ok:
        return
    run2 = driver.run_task(
        "staged input run",
        STAGED_WORKLOAD,
        output_paths=["result.json"],
        snapshot=snapshot,
        max_output_bytes=4096,
    )
    if not run2.quarantined:
        add(
            "dataset-staged-input-run",
            "FAIL",
            f"worker rc={run2.worker_returncode} state={run2.worker.get('state')} "
            f"reason={run2.worker.get('reason')} stderr={run2.worker_stderr}",
        )
        return
    manifest2 = driver.manifest_for(run2.operation_id)
    if manifest2 is None:
        add("dataset-staged-input-run", "FAIL", "run 2 left no artifact manifest")
        return
    driver.retain_and_release(manifest2)
    result = json.loads(driver.fetch_artifact(manifest2, "result.json"))
    same = result.get("sha256") == page_sha and result.get("bytes") == len(page)
    add(
        "dataset-staged-input-run",
        "PASS" if same else "FAIL",
        f"run 2 read /inputs/page.html: {result.get('bytes')} bytes sha256 "
        f"{str(result.get('sha256'))[:16]}; run 1 page.html {len(page)} bytes sha256 "
        f"{page_sha[:16]}; {'identical' if same else 'DIFFERENT'} bytes end to end",
    )
