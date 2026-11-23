"""Submit a code review of a branch on the platform's own git remote (ADR 0108).

One call, under one idempotency key: authorize the caller (a human, or an agent with
``review:submit``, the authority of ADR 0084 submit), apply the round and chain rules, read the
change from the operator-mapped local bare repository (``code_review_git``), adopt three
snapshots in the caller's project (head: changed files at the head plus the focused tests; base:
changed files at the merge base; rubric: the pinned rubric, the focused test list and, for a
re-review, ``recheck.json``), and submit a ``code_review`` job in ``diff`` mode whose
``code_review_sources`` row is written with it.

Git is read before the write transaction when the read-only precheck passes, so the database
lock is not held across git calls; the authoritative checks run again inside the transaction.
The snapshots, the job row, its subject and source rows, the events and the idempotency record
commit in that one transaction: a refusal at any step (policy, sealed input, round rules) leaves
nothing behind, and a replay of the key returns the same job without reading git again.

The verdict stays advisory: the job authorizes nothing and merges nothing.
"""

import hashlib
import json
import re
import uuid

from .code_review_jobs import MAX_CODE_REVIEW_INPUT_BYTES, source_record
from .review_jobs import ReviewRefused
from .review_jobs_models import SubmitCodeReview, SubmitReviewJob
from .store import StoreError, _connect, canonical_digest, utc_now
from .workspace import CapturedWorkspace, InventoryEntry, WorkspaceInventory, inventory_digest

MAX_FOCUSED_TESTS = 20
MAX_ROUNDS = 3
RUBRIC_FILE = "rubric.md"
FOCUSED_TESTS_FILE = "focused-tests.txt"
RECHECK_FILE = "recheck.json"
# Pinned rubric text: generic, so the rubric snapshot of every round-1 review is reproducible.
CODE_REVIEW_RUBRIC = """\
# Code review rubric

Review the change (base to head) for:

1. Correctness: logic errors, unhandled edge cases, broken invariants, wrong error handling.
2. Security and isolation: authorization and tenant checks at data access, unsafe input
   handling, secrets in code, logs or messages, fail-open behavior.
3. Data integrity: state, event and idempotency writes that should share one transaction;
   migrations that lose or misread recorded data.
4. Tests: whether the focused tests listed in focused-tests.txt exercise the changed behavior,
   including refusals and failure paths; claims the tests do not support.
5. Honesty of evidence: status, receipts or results that could be reported without having
   happened.
6. Maintainability: unclear contracts, dead code, duplicated logic that is likely to drift.

Report only problems you can point to in the change. Severity: blocking (must not merge),
major (should be fixed before merge), minor (follow-up), info (note only).
"""
GIT_INVALID_CODES = frozenset(
    {
        "invalid_repo",
        "invalid_branch",
        "invalid_base_branch",
        "invalid_head_sha",
        "invalid_sha",
        "invalid_path",
    }
)
_FOCUSED_NAME = re.compile(r"test_[A-Za-z0-9_]+\.py")
_GLOB_CHARS = frozenset("*?[")


def focused_tests_valid(tests):
    """ADR 0108 shape rule: 1..20 unique relative POSIX paths, each a ``test_*.py`` file under a
    ``tests/`` directory segment, with no ``..``, glob characters or trailing slash. A
    directory, or the whole test tree, cannot be expressed."""
    if not isinstance(tests, list) or not 1 <= len(tests) <= MAX_FOCUSED_TESTS:
        return False
    if len(set(tests)) != len(tests):
        return False
    for path in tests:
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or path.endswith("/")
            or "\\" in path
            or any(ord(char) < 32 or char == "\x7f" for char in path)
            or _GLOB_CHARS & set(path)
        ):
            return False
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            return False
        if "tests" not in parts[:-1] or _FOCUSED_NAME.fullmatch(parts[-1]) is None:
            return False
    return True


def _prompt_digest(round_number):
    # Lazy: the control service imports the child only to read its pinned digests.
    from tessera_agent.code_review_child import (
        CODE_RECHECK_PROMPT_SHA256,
        CODE_REVIEW_PROMPT_SHA256,
    )

    return CODE_REVIEW_PROMPT_SHA256 if round_number == 1 else CODE_RECHECK_PROMPT_SHA256


def _round(connection, actor, project_id, body):
    """``(round, listed)`` for this submit, or a stable refusal. ``listed`` is the
    ``recheck.json`` content for a re-review: ``[{number, severity, category, claim}]``."""
    previous_id, recheck = body["previous_job_id"], body["recheck_findings"]
    if previous_id is None:
        if recheck:
            raise ReviewRefused("recheck_findings_invalid", 422)
        return 1, []
    previous = connection.execute(
        "SELECT * FROM review_jobs WHERE id=? AND org_id=? AND project_id=?",
        (previous_id, actor["org_id"], project_id),
    ).fetchone()
    source = None if previous is None else source_record(connection, previous_id)
    if (
        previous is None
        or previous["kind"] != "code_review"
        or previous["state"] != "completed"
        or source is None
        or source["repo"] != body["repo"]
        or source["branch"] != body["branch"]
    ):
        # A missing, foreign-project or foreign-tenant job reads the same as a mismatch.
        raise ReviewRefused("review_chain_mismatch")
    if source["round"] >= MAX_ROUNDS:
        raise ReviewRefused("review_round_limit")
    verdict = connection.execute(
        "SELECT verdict FROM code_review_jobs WHERE job_id=? AND org_id=?",
        (previous_id, actor["org_id"]),
    ).fetchone()
    if verdict is None:
        raise ReviewRefused("review_chain_mismatch")
    if verdict["verdict"] == "MERGE":
        raise ReviewRefused("review_already_merge")
    findings = {}
    for row in connection.execute(
        "SELECT finding_key,finding_json FROM review_findings "
        "WHERE job_id=? AND org_id=? AND project_id=?",
        (previous_id, actor["org_id"], project_id),
    ):
        if row["finding_key"].isascii() and row["finding_key"].isdigit():
            findings[int(row["finding_key"])] = json.loads(row["finding_json"])
    if not recheck or len(set(recheck)) != len(recheck) or not set(recheck) <= set(findings):
        raise ReviewRefused("recheck_findings_invalid", 422)
    listed = [
        {
            "number": number,
            "severity": findings[number]["severity"],
            "category": findings[number]["category"],
            "claim": findings[number]["claim"],
        }
        for number in sorted(recheck)
    ]
    return source["round"] + 1, listed


def _read_git(git, body):
    """Resolve the branch and read the changed files and focused tests. Pure (no database)."""
    from .code_review_git import GitSourceRefused

    if not focused_tests_valid(body["focused_tests"]):
        raise ReviewRefused("focused_tests_invalid", 422)
    if git is None:
        raise ReviewRefused("git_remote_not_configured")
    repo, tests = body["repo"], body["focused_tests"]
    try:
        resolution = git.resolve(repo, body["branch"], body["head_sha"], body["base_branch"])
        if git.tree_has_files(repo, resolution.head_sha, tests) != set(tests):
            raise ReviewRefused("focused_tests_invalid", 422)
        changed = [item.path for item in resolution.changed]
        head_paths = [item.path for item in resolution.changed if item.status != "D"]
        base_paths = [item.path for item in resolution.changed if item.status != "A"]
        head = git.files_at(repo, resolution.head_sha, list(dict.fromkeys(head_paths + tests)))
        base = git.files_at(repo, resolution.base_sha, base_paths)
    except GitSourceRefused as refusal:
        # A malformed name, SHA or path is the request's fault (422); the rest are 409.
        status = 422 if refusal.code in GIT_INVALID_CODES else 409
        raise ReviewRefused(refusal.code, status, refusal.rule) from None
    if set(tests) - set(head):
        raise ReviewRefused("focused_tests_invalid", 422)
    return {
        "resolution": resolution,
        "changed": changed,
        "head": head,
        "base": base,
    }


def _capture(root, files):
    ordered = tuple(sorted(files.items()))
    entries = tuple(
        InventoryEntry(path, len(content), hashlib.sha256(content).hexdigest())
        for path, content in ordered
    )
    inventory = WorkspaceInventory(root, entries, (), sum(len(c) for _, c in ordered))
    return CapturedWorkspace(inventory, ordered)


def _adopt(store, connection, actor, project_id, capture, now):
    """Adopt ``capture`` as a snapshot in the caller's transaction: the same rows, class record
    and ``workspace.snapshot_adopted`` event as ``SnapshotStore.adopt_snapshot``, with the caller
    authorized by the code review submit (not the human-only local adoption)."""
    from .data_class import record_snapshot_data_class, refuse_relabelled_files
    from .snapshots import _validate_capture

    digest = inventory_digest(capture.inventory)
    try:
        manifest = _validate_capture(capture, digest)
    except StoreError:
        # Paths and sizes are bounded by the git reader; what remains is an entry custody
        # cannot hold (for example a path with a backslash).
        raise ReviewRefused("unsupported_entry") from None
    ident = "snapshot_" + uuid.uuid4().hex
    connection.execute(
        "INSERT INTO source_snapshots VALUES (?, ?, ?, ?, ?, ?)",
        (ident, project_id, digest, json.dumps(manifest), now, actor["id"]),
    )
    connection.executemany(
        "INSERT INTO snapshot_files VALUES (?, ?, ?)",
        ((ident, path, content) for path, content in capture.files),
    )
    # No declaration: the snapshot is private_derived by default (ADR 0039).
    record = record_snapshot_data_class(connection, actor, ident, None, now=now)
    refuse_relabelled_files(connection, actor["org_id"], ident)
    store._event(
        connection,
        actor,
        project_id,
        None,
        "workspace.snapshot_adopted",
        1,
        {
            "snapshot_id": ident,
            "digest": digest,
            "file_count": len(capture.files),
            "total_bytes": capture.inventory.total_bytes,
            "provenance": "imported_unverified",
            "classification": "unclassified",
            "data_class": record,
            "origin": "code_review_submit",
        },
    )
    return {"snapshot_id": ident, "digest": digest}


def submit_code_review(store, principal, project_id, key, payload, policy, git):
    """``(201, job record)``; refusals are ``ReviewRefused`` with stable reasons (ADR 0108)."""
    body = SubmitCodeReview.model_validate(payload).model_dump(mode="json")
    path = f"/projects/{project_id}/code-reviews"
    fetched = {}

    def fetch():
        if "error" in fetched:
            raise fetched["error"]
        if "value" not in fetched:
            fetched["value"] = _read_git(git, body)
        return fetched["value"]

    if _precheck(store, principal, project_id, key, path, body):
        try:
            fetch()
        except ReviewRefused as refusal:
            # Raised again inside the command, so the refusal is audited like any other.
            fetched["error"] = refusal

    def command(connection, actor):
        store._submit_guard(connection, actor, project_id)
        round_number, listed = _round(connection, actor, project_id, body)
        source_files = fetch()
        resolution = source_files["resolution"]
        rubric = {
            RUBRIC_FILE: CODE_REVIEW_RUBRIC.encode("utf-8"),
            FOCUSED_TESTS_FILE: ("\n".join(body["focused_tests"]) + "\n").encode("utf-8"),
        }
        if round_number > 1:
            rubric[RECHECK_FILE] = (json.dumps(listed, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        captures = {
            "head": _capture("head", source_files["head"]),
            "base": _capture("base", source_files["base"]),
            "rubric": _capture("rubric", rubric),
        }
        if (
            sum(capture.inventory.total_bytes for capture in captures.values())
            > MAX_CODE_REVIEW_INPUT_BYTES
        ):
            raise ReviewRefused("change_too_large")
        now = utc_now()
        adopted = {
            name: _adopt(store, connection, actor, project_id, capture, now)
            for name, capture in captures.items()
        }
        source = {
            "repo": resolution.repo,
            "branch": resolution.branch,
            "head_sha": resolution.head_sha,
            "base_branch": resolution.base_branch,
            "base_sha": resolution.base_sha,
            "focused_tests": list(body["focused_tests"]),
            "round": round_number,
            "previous_job_id": body["previous_job_id"],
            "recheck_findings": sorted(body["recheck_findings"] or []),
        }
        job = SubmitReviewJob.model_validate(
            {
                "kind": "code_review",
                "title": f"Code review round {round_number}: "
                f"{resolution.repo}@{resolution.head_sha[:12]}",
                "inputs": [adopted["head"], adopted["base"], adopted["rubric"]],
                "prompt_digest": _prompt_digest(round_number),
                "destination_id": body["destination_id"],
                "tier": body["tier"],
                "budget_cents": body["budget_cents"],
                "egress": "model_runtime",
                "code_review": {
                    "mode": "diff",
                    "head_digest": adopted["head"]["digest"],
                    "base_digest": adopted["base"]["digest"],
                    "rubric_digest": adopted["rubric"]["digest"],
                },
            }
        ).model_dump(mode="json")
        return 201, store._submit_job(connection, actor, project_id, job, policy, source)

    return store._mutate(
        principal,
        key,
        "POST",
        path,
        canonical_digest(body),
        command,
        replay_guard=lambda c, a, r: store._submit_guard(c, a, project_id),
    )


def _precheck(store, principal, project_id, key, path, body):
    """True when git should be read before the write transaction: the caller is authorized,
    the key is new and the round rules pass. Read-only and advisory; anything else (a
    replay, a refusal, an error) is left to the authoritative command."""
    connection = _connect(store.database_path)
    try:
        connection.execute("BEGIN")
        actor = store._actor(connection, principal)
        store._submit_guard(connection, actor, project_id)
        if connection.execute(
            "SELECT 1 FROM idempotency WHERE principal_id=? AND org_id=? AND idempotency_key=? "
            "AND method='POST' AND path=?",
            (actor["id"], actor["org_id"], key, path),
        ).fetchone():
            return False
        _round(connection, actor, project_id, body)
        return True
    except Exception:
        return False
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()
