"""Out-of-process runner for code review jobs (ADR 0092).

The runner claims queued ``code_review`` jobs from the control store, re-checks
policy, the pinned prompt, egress and destination at launch, loads the verified
custody content of the job's inputs, and runs the built-in model child
(``tessera_agent.code_review_child``) under a bounded concurrency and memory gate.
It reports the child's outcome back through the store protocol together with any
model-call receipt the child produced, on success and on every failure. It never
invents findings, verdicts or receipts: whatever the child returns is validated by
the store, and anything else is a truthful failure. Without an operator policy,
model profile and credential the runner is not started and jobs stay queued.

The Anthropic destination is the Messages API, reached by the child with the
``anthropic-api-key`` credential held by reference under ``$CREDENTIALS_DIRECTORY``,
which must contain only that entry. The runner only checks that the file exists, is
a regular non-symlink file and is private to its owner; it never opens or reads it,
and the child reads it itself. The key is never an environment variable.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .review_jobs import ReviewRefused
from .store import StoreError

SYSTEM_ACTOR = "system:code-review-runner"
FORBIDDEN_ENVIRONMENT = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY")
# No HOME and no PATH: the built-in child is started by absolute interpreter path and
# reaches its model credential only by reference under CREDENTIALS_DIRECTORY (ADR 0082),
# never from a login found in a home directory.
CHILD_ENVIRONMENT_ALLOWLIST = ("LANG", "CREDENTIALS_DIRECTORY", "TMPDIR")
CREDENTIAL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_CONCURRENT_LIMIT = 8
MAX_CHILD_STDOUT_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
MAX_PROFILE_BYTES = 64 * 1024
# The only credential the built-in child reads: the Anthropic Messages client's key file.
ANTHROPIC_CREDENTIAL_NAME = "anthropic-api-key"
CODE_REVIEW_DESTINATION = "anthropic"
CODE_REVIEW_EGRESS = "model_runtime"
# The only child the CLI starts. ``-I`` isolates it from the environment and the CWD.
BUILTIN_CHILD_ARGV = (sys.executable, "-I", "-m", "tessera_agent.code_review_child")
# Child failure codes recorded as themselves; anything else is ``model_failed``.
CHILD_FAILURE_REASONS = {
    "budget_exceeded": "budget_exceeded",
    "input_too_large": "input_too_large",
    "failed_output_invalid": "failed_output_invalid",
    "prompt_not_pinned": "prompt_not_pinned",
}


# Room left between the child timeout and the lease for the kill and the report.
CHILD_TIMEOUT_MARGIN_SECONDS = 30


class RunnerRefused(RuntimeError):
    """Sanitized startup refusal; never echoes environment values."""


def refuse_api_key_environment(environment) -> None:
    # tessera_agent imports tessera_control, so an import here would be a cycle.
    # This mirrors tessera_agent.model_destinations.refuse_api_key_environment.
    if any(name in environment for name in FORBIDDEN_ENVIRONMENT):
        raise RunnerRefused("api_key_environment_present")


def validate_credential_reference(name: str | None, environment) -> None:
    """Check a credential reference at startup without ever opening the file.

    Refusal codes are fixed strings; neither the directory nor file content is echoed.
    """
    if name is None:
        return
    if not isinstance(name, str) or CREDENTIAL_NAME_PATTERN.fullmatch(name) is None:
        raise RunnerRefused("credential_name_invalid")
    directory = environment.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise RunnerRefused("credential_directory_missing")
    try:
        info = os.lstat(os.path.join(directory, name))
    except OSError as error:
        raise RunnerRefused("credential_missing") from error
    if not stat.S_ISREG(info.st_mode):  # lstat: a symlink is not a regular file
        raise RunnerRefused("credential_missing")
    if info.st_mode & 0o077:
        raise RunnerRefused("credential_not_private")


def validate_exclusive_credential_directory(name: str, environment) -> None:
    """Refuse unless ``$CREDENTIALS_DIRECTORY`` holds exactly the named credential.

    The child inherits the directory, so anything else in it would be reachable too.
    """
    directory = environment.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise RunnerRefused("credential_directory_missing")
    try:
        entries = os.listdir(directory)
    except OSError as error:
        raise RunnerRefused("credential_directory_missing") from error
    if entries != [name]:
        raise RunnerRefused("credential_directory_not_exclusive")


def load_model_profile(path: str) -> dict:
    """Read and statically validate the ADR 0082 Anthropic profile; codes only on refusal."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_PROFILE_BYTES + 1)
        if len(raw) > MAX_PROFILE_BYTES:
            raise ValueError("too large")
        profile = json.loads(raw.decode("utf-8"))
        if not isinstance(profile, dict):
            raise ValueError("not an object")
        # tessera_agent imports tessera_control; import lazily to avoid a cycle.
        from tessera_agent.anthropic_messages import validate_anthropic_profile

        validate_anthropic_profile(profile)
    except Exception as error:  # noqa: BLE001 - every problem is the same sanitized refusal
        raise RunnerRefused("model_profile_invalid") from error
    return profile


def pinned_prompt_digests() -> frozenset[str]:
    """SHA-256 digests of the built-in child's pinned prompts (review and re-review).

    Empty (nothing matches) if the child's constants are unavailable or malformed.
    """
    try:
        from tessera_agent.code_review_child import PINNED_PROMPT_DIGESTS
    except Exception:  # noqa: BLE001 - fail closed: no job is pinned to a missing prompt
        return frozenset()
    if not isinstance(PINNED_PROMPT_DIGESTS, (set, frozenset)) or not all(
        isinstance(item, str) and len(item) == 64 for item in PINNED_PROMPT_DIGESTS
    ):
        return frozenset()
    return frozenset(PINNED_PROMPT_DIGESTS)


@dataclass(frozen=True)
class ChildOutcome:
    kind: str  # "completed" | "vanished" | "failed"
    output: dict | None = None
    detail: str = ""
    receipt: dict | None = None  # model-call receipt reported by the child, if any

    @staticmethod
    def completed(output: dict) -> ChildOutcome:
        receipt = output.get("receipt") if isinstance(output, dict) else None
        return ChildOutcome(
            "completed", output=output, receipt=receipt if isinstance(receipt, dict) else None
        )

    @staticmethod
    def vanished(detail: str = "") -> ChildOutcome:
        return ChildOutcome("vanished", detail=detail)

    @staticmethod
    def failed(reason: str, receipt: dict | None = None) -> ChildOutcome:
        return ChildOutcome(
            "failed", detail=reason, receipt=receipt if isinstance(receipt, dict) else None
        )


class ReviewModel(Protocol):
    def run(self, job: dict, attempt: int, extra: dict) -> ChildOutcome:
        """Run one attempt. ``extra`` carries the runner-built spec fields:
        ``inputs`` (custody content, base64), ``profile`` and ``receipt_path``."""
        ...


def child_environment(parent: dict | None = None) -> dict[str, str]:
    source = os.environ if parent is None else parent
    return {
        name: source[name]
        for name in CHILD_ENVIRONMENT_ALLOWLIST
        if name in source and name not in FORBIDDEN_ENVIRONMENT
    }


def job_spec(
    job: dict, attempt: int, credential_name: str | None = None, extra: dict | None = None
) -> dict:
    subject = job.get("code_review") or {}
    spec = {
        "job_id": job.get("id"),
        "kind": job.get("kind"),
        "inputs": [
            {
                "snapshot_id": item.get("snapshot_id"),
                "digest": item.get("digest"),
                "data_class": item.get("data_class"),
            }
            for item in job.get("inputs", [])
        ],
        "code_review": {
            "mode": subject.get("mode"),
            "head_digest": subject.get("head_digest"),
            "base_digest": subject.get("base_digest"),
            "rubric_digest": subject.get("rubric_digest"),
        },
        "prompt_digest": job.get("prompt_digest"),
        "destination_id": job.get("destination_id"),
        "tier": job.get("tier"),
        "budget_cents": job.get("budget_cents"),
        "egress": job.get("egress"),
        "attempt": attempt,
        "credential_name": credential_name,
    }
    if extra:
        spec.update(extra)
    return spec


def encode_inputs(job: dict, loaded) -> list[dict]:
    """Base64 the verified custody content for the child spec.

    Raises ValueError unless the loaded inputs are exactly the job's inputs (by digest).
    """
    if not isinstance(loaded, list):
        raise ValueError("inputs")
    wanted = sorted(str(item.get("digest")) for item in job.get("inputs", []))
    encoded = []
    for item in loaded:
        files = []
        for entry in item["files"]:
            if not isinstance(entry["path"], str) or not isinstance(entry["content"], bytes):
                raise ValueError("file")
            files.append(
                {"path": entry["path"], "content_b64": base64.b64encode(entry["content"]).decode()}
            )
        encoded.append(
            {
                "snapshot_id": item["snapshot_id"],
                "digest": item["digest"],
                "data_class": item["data_class"],
                "files": files,
            }
        )
    if sorted(str(item["digest"]) for item in encoded) != wanted:
        raise ValueError("inputs")
    return encoded


def _cents(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def remaining_budget_cents(job: dict) -> int:
    """Job budget minus what earlier attempts' receipts cost, clamped at 0.

    A malformed budget or spent total is treated as no budget left (fail closed).
    """
    budget = _cents(job.get("budget_cents"))
    spent = (job.get("code_review") or {}).get("cost_cents_total", 0)
    spent = 0 if spent is None else _cents(spent)
    if budget is None or spent is None:
        return 0
    return max(budget - spent, 0)


def read_receipt_file(path: str) -> dict | None:
    """A child-written receipt: regular non-symlink file, at most 64 KiB, a JSON object."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(MAX_RECEIPT_BYTES + 1)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > MAX_RECEIPT_BYTES:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


class SubprocessReviewModel:
    """Runs a child argv (the CLI uses ``BUILTIN_CHILD_ARGV`` only); spec on stdin, one
    JSON object on stdout."""

    def __init__(self, argv: list[str], timeout_seconds: float, credential_name: str | None = None):
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise ValueError("argv must be a non-empty list of strings")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.argv = list(argv)
        self.timeout_seconds = timeout_seconds
        if credential_name is not None and (
            not isinstance(credential_name, str)
            or CREDENTIAL_NAME_PATTERN.fullmatch(credential_name) is None
        ):
            raise ValueError("credential_name is not a plain file name")
        self.credential_name = credential_name

    def run(self, job: dict, attempt: int, extra: dict | None = None) -> ChildOutcome:
        payload = json.dumps(job_spec(job, attempt, self.credential_name, extra)).encode()
        try:
            process = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=child_environment(),
                start_new_session=True,
            )
        except OSError:
            return ChildOutcome.vanished("child_not_launched")
        try:
            stdout, _ = process.communicate(payload, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            self._kill(process)
            try:
                process.communicate(timeout=5)
            except Exception:  # noqa: BLE001 - child is already killed
                pass
            return ChildOutcome.vanished("child_timeout")
        except OSError:
            self._kill(process)
            return ChildOutcome.vanished("child_io_error")
        if process.returncode is None or process.returncode < 0:
            return ChildOutcome.vanished("child_signalled")
        return self._parse(stdout)

    @staticmethod
    def _kill(process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass

    @staticmethod
    def _parse(stdout: bytes) -> ChildOutcome:
        if not stdout or len(stdout) > MAX_CHILD_STDOUT_BYTES:
            return ChildOutcome.vanished("child_no_output")
        try:
            body = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return ChildOutcome.vanished("child_unparseable_output")
        if not isinstance(body, dict):
            return ChildOutcome.vanished("child_unparseable_output")
        if body.get("outcome") == "completed" and isinstance(body.get("output"), dict):
            return ChildOutcome.completed(body["output"])
        if body.get("outcome") == "failed" and isinstance(body.get("reason"), str):
            return ChildOutcome.failed(body["reason"][:64], body.get("receipt"))
        return ChildOutcome.vanished("child_unparseable_output")


def read_available_memory_mib() -> float:
    """MemAvailable in MiB from /proc/meminfo; unknown or unreadable is 0 (fail closed)."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


MemoryProbe = Callable[[], float]


def _safe_probe(probe: MemoryProbe) -> float:
    try:
        value = float(probe())
    except Exception:  # noqa: BLE001 - fail closed
        return 0.0
    return value if value == value else 0.0  # NaN -> 0


def _over_budget(output: object, budget_cents: object) -> bool:
    receipt = output.get("receipt") if isinstance(output, dict) else None
    billed = ("reported_cost", "computed_from_pinned_prices")
    if not isinstance(receipt, dict) or receipt.get("cost_basis") not in billed:
        return False
    cents = receipt.get("cost_cents")
    if not isinstance(cents, int) or isinstance(cents, bool):
        return True
    return isinstance(budget_cents, int) and cents > budget_cents


class CodeReviewRunner:
    def __init__(
        self,
        store,
        model: ReviewModel,
        policy,
        *,
        runner_id: str,
        max_concurrent: int = 2,
        min_free_memory_mib: float = 1024,
        lease_seconds: float = 900,
        memory_probe: MemoryProbe = read_available_memory_mib,
        clock: Callable[[], float] = time.time,
        renew_interval_seconds: float | None = None,
        credential_name: str | None = None,
        profile: dict | None = None,
    ):
        if model is None:
            raise ValueError("a review model is required")
        if not isinstance(max_concurrent, int) or not 1 <= max_concurrent <= MAX_CONCURRENT_LIMIT:
            raise ValueError("max_concurrent must be between 1 and 8")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not runner_id:
            raise ValueError("runner_id is required")
        if credential_name is not None and (
            not isinstance(credential_name, str)
            or CREDENTIAL_NAME_PATTERN.fullmatch(credential_name) is None
        ):
            raise ValueError("credential_name is not a plain file name")
        self.credential_name = credential_name
        self.profile = profile
        self.store = store
        self.model = model
        self.policy = policy
        self.runner_id = runner_id
        self.max_concurrent = max_concurrent
        self.min_free_memory_mib = min_free_memory_mib
        self.lease_seconds = lease_seconds
        self.memory_probe = memory_probe
        self.clock = clock
        self.renew_interval = renew_interval_seconds or lease_seconds / 3.0
        child_timeout = getattr(model, "timeout_seconds", None)
        if isinstance(child_timeout, (int, float)) and (
            child_timeout >= lease_seconds - min(self.renew_interval, CHILD_TIMEOUT_MARGIN_SECONDS)
        ):
            # A child that may outlive the lease would be reaped and requeued while still running.
            raise ValueError("the child timeout must be shorter than the lease minus a margin")
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def run_once(self) -> int:
        """Reap, then claim and start as many jobs as the local and memory gates allow."""
        self.store.reap_expired_code_review_leases(self.clock())
        started = 0
        while True:
            with self._lock:
                if self._in_flight >= self.max_concurrent:
                    break
            if _safe_probe(self.memory_probe) < self.min_free_memory_mib:
                break
            job = self.store.claim_code_review(
                self.runner_id, self.max_concurrent, self.lease_seconds, self.clock()
            )
            if job is None:
                break
            if self._recheck_launch(job):
                self._start(job)
                started += 1
        return started

    def _recheck_launch(self, job: dict) -> bool:
        classes = sorted({item["data_class"] for item in job.get("inputs", [])})
        try:
            reason = self.policy.check(
                job["destination_id"], job["tier"], classes, job["budget_cents"]
            )
        except Exception:  # noqa: BLE001 - fail closed
            reason = "policy_check_failed"
        if not reason:
            if job.get("prompt_digest") not in pinned_prompt_digests():
                reason = "prompt_not_pinned"
            elif job.get("egress") != CODE_REVIEW_EGRESS:
                reason = "egress_not_permitted"
            elif job.get("destination_id") != CODE_REVIEW_DESTINATION:
                reason = "destination_not_supported"
        if reason:
            self.store.not_start_code_review(job["id"], self.runner_id, reason, self.clock())
            return False
        return True

    def _start(self, job: dict) -> None:
        thread = threading.Thread(
            target=self._work, args=(job,), name="code-review-job", daemon=True
        )
        with self._lock:
            self._in_flight += 1
            self._threads.add(thread)
        thread.start()

    def _work(self, job: dict) -> None:
        stop = threading.Event()
        renewer = threading.Thread(
            target=self._renew_loop, args=(job["id"], stop), name="code-review-lease", daemon=True
        )
        renewer.start()
        try:
            self._execute(job)
        except Exception:  # noqa: BLE001 - the lease reaper handles anything unreported
            pass
        finally:
            stop.set()
            renewer.join(timeout=5)
            with self._lock:
                self._in_flight -= 1
                self._threads.discard(threading.current_thread())

    def _renew_loop(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.renew_interval):
            try:
                if not self.store.renew_code_review_lease(
                    job_id, self.runner_id, self.lease_seconds, self.clock()
                ):
                    return
            except Exception:  # noqa: BLE001 - a transient store error must not end renewal
                continue  # back off to the next interval; only a definitive False stops it

    def _execute(self, job: dict) -> None:
        job_id = job["id"]
        attempt = (job.get("code_review") or {}).get("attempts", 1)
        remaining = remaining_budget_cents(job)
        if remaining <= 0:
            # Earlier attempts already spent the budget: a retry would spend it twice.
            self.store.fail_code_review(
                job_id,
                self.runner_id,
                "budget_exceeded",
                self.clock(),
                receipt=None,
                attempt=attempt,
            )
            return
        try:
            inputs = encode_inputs(
                job, self.store.code_review_inputs(job_id, self.runner_id, self.clock())
            )
        except (StoreError, ValueError, TypeError, KeyError, AttributeError):
            self.store.fail_code_review(
                job_id, self.runner_id, "input_unavailable", self.clock(), attempt=attempt
            )
            return
        directory = tempfile.mkdtemp(prefix="tessera-code-review-")
        try:
            os.chmod(directory, 0o700)
            receipt_path = os.path.join(directory, "receipt.json")
            extra = {"inputs": inputs, "receipt_path": receipt_path, "budget_cents": remaining}
            if self.profile is not None:
                extra["profile"] = self.profile
            try:
                outcome = self.model.run(job, attempt, extra)
            except Exception:  # noqa: BLE001 - a crashing adapter is a model failure
                outcome = ChildOutcome.failed("model_failed")
            receipt = outcome.receipt
            if receipt is None:
                receipt = read_receipt_file(receipt_path)
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        self._report(job, outcome, receipt, attempt)

    def _report(self, job: dict, outcome: ChildOutcome, receipt: dict | None, attempt: int) -> None:
        job_id = job["id"]
        if outcome.kind == "completed":
            try:
                self.store.complete_code_review(
                    job_id, self.runner_id, outcome.output, self.clock(), attempt=attempt
                )
            except ReviewRefused as refused:
                if refused.status != 422:
                    raise
                # The store names an over-budget refusal; the local check is a fallback only.
                over = getattr(refused, "detail", None) == "budget_exceeded" or _over_budget(
                    outcome.output, job.get("budget_cents")
                )
                reason = "budget_exceeded" if over else "failed_output_invalid"
                self.store.fail_code_review(
                    job_id, self.runner_id, reason, self.clock(), receipt=receipt, attempt=attempt
                )
        elif outcome.kind == "vanished":
            self.store.requeue_vanished_code_review(
                job_id, self.runner_id, self.clock(), receipt=receipt, attempt=attempt
            )
        else:
            reason = CHILD_FAILURE_REASONS.get(outcome.detail, "model_failed")
            self.store.fail_code_review(
                job_id, self.runner_id, reason, self.clock(), receipt=receipt, attempt=attempt
            )

    def drain(self, timeout: float | None = None) -> None:
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)

    close = drain


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tessera_control.code_review_runner")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument(
        "--model-profile",
        required=True,
        help="ADR 0082 Anthropic Messages profile (JSON file), validated at startup",
    )
    parser.add_argument("--max-concurrent", type=int, default=2)
    parser.add_argument("--min-free-memory-mib", type=float, default=1024)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--lease-seconds", type=float, default=900)
    parser.add_argument("--child-timeout-seconds", type=float, default=840)
    parser.add_argument(
        "--policy",
        help=(
            "operator review policy as module:attribute (an object, or a class taking no "
            "arguments, "
            "with check(destination_id, tier, data_classes, budget_cents)). Required: without "
            "it the runner does not start and jobs stay queued"
        ),
    )
    parser.add_argument(
        "--credential-name",
        required=True,
        help=(
            f"must be {ANTHROPIC_CREDENTIAL_NAME}: the Messages API key file under "
            "$CREDENTIALS_DIRECTORY, which must contain only that entry. Held by reference "
            "and never read by the runner"
        ),
    )
    return parser


def load_policy(spec: str):
    """Import an operator policy named ``module:attribute``; refuse anything without check()."""
    import importlib

    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise RunnerRefused("policy_spec_invalid")
    try:
        found = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as error:
        raise RunnerRefused("policy_not_found") from error
    policy = found() if isinstance(found, type) else found
    if not callable(getattr(policy, "check", None)):
        raise RunnerRefused("policy_invalid")
    return policy


def main(argv: list[str] | None = None, environment=None, policy=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.max_concurrent <= MAX_CONCURRENT_LIMIT:
        parser.error("--max-concurrent must be between 1 and 8")
    if args.poll_seconds <= 0 or args.lease_seconds <= 0 or args.child_timeout_seconds <= 0:
        parser.error("durations must be positive")
    if args.child_timeout_seconds >= args.lease_seconds:
        parser.error("--child-timeout-seconds must be less than --lease-seconds")
    environment = os.environ if environment is None else environment
    try:
        refuse_api_key_environment(environment)
        if policy is None:
            # A runner without an operator policy would claim every queued job only to mark it
            # not_started; refusing to start keeps those jobs queued (the truthful default).
            if not args.policy:
                raise RunnerRefused("policy_required")
            policy = load_policy(args.policy)
        profile = load_model_profile(args.model_profile)
        if args.credential_name != ANTHROPIC_CREDENTIAL_NAME:
            raise RunnerRefused("credential_name_invalid")
        validate_credential_reference(args.credential_name, environment)
        validate_exclusive_credential_directory(args.credential_name, environment)
    except RunnerRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    from pathlib import Path

    from .review_jobs import ReviewJobStore

    store = ReviewJobStore(Path(args.state_dir))
    runner = CodeReviewRunner(
        store,
        SubprocessReviewModel(
            list(BUILTIN_CHILD_ARGV), args.child_timeout_seconds, args.credential_name
        ),
        policy,
        runner_id=f"runner-{uuid.uuid4().hex}",
        max_concurrent=args.max_concurrent,
        min_free_memory_mib=args.min_free_memory_mib,
        lease_seconds=args.lease_seconds,
        credential_name=args.credential_name,
        profile=profile,
    )
    try:
        while True:
            runner.run_once()
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        runner.drain()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
