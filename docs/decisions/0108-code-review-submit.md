# ADR 0108: submitting a code review from a platform git branch

Status: proposed (schema v81). Extends the code review jobs of
[ADR 0092](0092-code-review-jobs.md), the review jobs of
[ADR 0084](0084-research-review-journeys.md) and the receipts of
[ADR 0083](0083-model-call-receipts.md).

## Context

ADR 0092 can run a receipted code review, but a client must first produce two content-addressed
snapshots and a rubric snapshot by hand. The review runner that works outside Tessera on a box is to
be retired once the reasoning key is in place, so a review should be requested for a branch on the
platform's own git remote, with the result written to the job receipt. A repository can exceed the
snapshot file limit, so whole-tree snapshots are not an option.

## Decision

### A. Source: operator-mapped local bare repositories

The operator maps repository names to local bare repositories on the control host in a JSON file
(`{"repos": {"name": "/abs/path.git"}}`). With no mapping, submit is refused
`git_remote_not_configured` (409). Control runs only local `git --git-dir` commands with a minimal
environment, no shell, a timeout and no stdin. It never fetches, clones or contacts a remote. Names,
branches and SHAs are validated (a branch must also pass `git check-ref-format`; a SHA is 40
lowercase hex). Git error text and paths are never put in a refusal.

Wiring: `tessera serve --code-review-git-remotes FILE` (fallback environment variable
`TESSERA_CODE_REVIEW_GIT_REMOTES`; the flag wins) loads the file through `load_git_remotes` at
startup, and `create_app(..., code_review_git_remotes=FILE)` does the same for embedders. Default
off. A missing or malformed file stops startup (`tessera serve` exits 2 with a fixed message; the
file's content is never printed).

Secret and data guard: the reader refuses the whole submit before any content reaches a snapshot
(and so a model destination). A changed path (added, modified or deleted) whose name matches
`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `*.p12`, `*.pfx`, `credentials*`,
`secrets*`, `*.sqlite*` or `*.db`, or that has a directory segment `.aws`, `.ssh`, `.gnupg`, `data`
or `datasets`, is refused `path_denied` (409). A file whose content has a PEM private-key header,
an Anthropic API key, an AWS access key id or a GitHub token shape is refused `content_denied`
(409). The response carries only the code; the matched rule's label is kept on the internal
refusal, never a path or content. The remotes file may add `deny_paths` (extra globs, refused as
`path_denied`) and `allow_paths` (globs exempting a path from the built-in name and directory
rules only, never from `deny_paths` or the content check). A glob without `/` matches any path
segment, one with `/` is anchored at the repository root; at most 64 each, case-insensitive,
validated at load. Both default to none.

### B. Head, base and changed files

The branch must point at the supplied `head_sha` (`branch_not_found`, `head_mismatch`). The base is
the merge-base of the head and the base branch (default `main`; `base_not_found`). The changed paths
come from `git diff --name-status -z --no-renames` (a rename is a delete plus an add). No change, more
than 400 files or more than 16 MiB of base and head contents is refused (`no_changes`,
`change_too_large`). A symlink or submodule entry is refused (`unsupported_entry`).

Control adopts three snapshots in the caller's project through the existing custody path: the head
(changed files at the head plus the focused test files), the base (changed files at the base) and a
generated rubric snapshot (`rubric.md` with the pinned rubric text, `focused-tests.txt` and, for a
re-review, `recheck.json`). Only changed files are snapshotted so a snapshot stays bounded.

### C. Focused tests only

Each focused test is a relative POSIX path under a `tests/` directory segment, named `test_*.py` (the
generic Python test convention), unique, without `..`, glob characters or a trailing slash, and a
regular file at the head (422 `focused_tests_invalid`). One to 20 are accepted. A directory or the
whole test tree cannot be expressed, which keeps the review's test claims bounded and checkable.

### D. Stopping rule and prompts

Round 1 has no `previous_job_id`. A re-review names a `completed` code review of the same project,
repo and branch (`review_chain_mismatch`), whose round is below 3 (`review_round_limit`) and whose
verdict is not `MERGE` (`review_already_merge`), and a non-empty, unique `recheck_findings` that are
finding numbers of that job (422 `recheck_findings_invalid`). The re-review checks only the listed
findings: every finding it returns has category `recheck:<n>` for a listed `n`, once each, and
nothing else is stored (422 `failed_output_invalid`). A resolved finding is simply not reported; if
all are resolved the verdict is `MERGE`.

The job's prompt digest is pinned per round kind: the round 1 review prompt, or the re-check prompt
for rounds 2 and 3. The runner accepts either pinned digest and the child picks the prompt by digest;
any other digest is `prompt_not_pinned`.

### E. Job, receipt and interfaces

Submit adopts the snapshots and submits a `code_review` job in `diff` mode with egress
`model_runtime`, under one idempotency key: a replay returns the same job. The `code_review_sources`
row (repo, branch, head and base SHAs, base branch, focused tests, round, previous job, rechecked
findings) is written in the job's own transaction, and cannot be updated or deleted. The job shows it
as `code_review.source`. The review receipt carries the `source`, the verdict and the findings
themselves, and its digest covers them. The job record shows `code_review.source` and
`code_review.review_receipt`.

The MCP tool `tessera_submit_code_review` (scope `review:submit`, agent or human, the authority of
ADR 0084 submit) and the `tessera-review submit` command post the same body to
`POST /projects/{p}/code-reviews`. Client-side validation mirrors the shape rules and sends nothing
on failure. The verdict stays advisory: it authorizes nothing and merges nothing.

### Atomicity

The submit is fully atomic. The three snapshots, the job row, its subject and source rows, the
events and the idempotency record commit in one transaction, so any refusal (policy, budget,
approval, size) leaves nothing behind. Git is read before the write lock is taken, and every check
(authorization, round rules, head, size) is repeated inside the transaction against the data read. A
replay of the idempotency key is answered before any git call and returns the same job.

### Authority

Direct snapshot adoption is human-only. Here an agent holding `review:submit` (or a person) causes
snapshots to be adopted through this path. That is bounded: the content comes only from
operator-configured repositories on the platform's own git remote, at a verified head and its merge
base (the changed files and the named focused tests), plus a rubric snapshot that control generates
from pinned text, the validated test list and the previous job's findings. The client supplies no
bytes. Snapshots are `private_derived` by default and are recorded with a
`workspace.snapshot_adopted` event whose origin is `code_review_submit`.

## Schema v81

`code_review_sources` (job id primary key referencing `review_jobs`, org, project, repo, branch,
head SHA, base branch, base SHA, focused tests, round 1 to 3, previous job, rechecked findings,
created time) with no-update and no-delete triggers. Migration 75 remains the code review jobs
migration of ADR 0092.

## Consequences

- A branch review is one call, with source, verdict and findings on a receipt.
- The operator must map repositories; with no mapping nothing is read.
- No live or paid model call was made to build or test this.

## Tests

No network and no paid calls: the git reader against temporary bare repositories (validation, head
mismatch, merge-base, rename handling, size limits, symlink refusal); focused test rules; the
stopping rule (round limit, `MERGE`, chain mismatch, bad recheck numbers); the re-review ingest rule;
snapshots and the source row written once, with replay returning the same job; the v81 migration and
triggers; receipt contents; the MCP tool and CLI validation.

## Open questions

1. Running the listed focused tests as a Tessera stage and attaching their receipt to the review.
2. Retiring the box runner is an operator step, after the first receipted live review.
