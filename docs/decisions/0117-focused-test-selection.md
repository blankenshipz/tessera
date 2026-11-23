# 0117 — Focused test selection with an impact map and a smoke set

## Status

Proposed. Extends the changed-test selector in `dev/focused_tests.py` and the deploy upgrade of
[ADR 0099](0099-deploy-upgrade.md). No schema change.

## Context

Between full runs a branch is gated by a hand-picked set: the branch's own tests, the tests of
the shared modules it touches, and a fixed smoke set chosen by hand each time. The full suite is
too heavy for the shared hosts (a full run takes tens of minutes and its shards slow each other
down), so it is a once-per-branch pre-merge run and cannot be the working loop.

The existing selector follows file names and import text one level out, which is the right idea
but fails at the hubs. A change to the store, the API, the migrations, the receipts or the
campaign runner is imported by most of the source tree, so "the tests of the changed module and
its dependents" fans out to half the suite, or, when the hub is reached only through a thin
wrapper, selects too little. Neither result is a gate someone can trust or afford.

## Decision

1. **An impact map from imports, plus a small checked-in override file.** The map is computed
   on demand from file names and import text (no code under test is imported): a changed
   module selects the tests named for it, the tests that import it, and the tests of the source
   modules that directly import it (one level of dependents). Where that is wrong, a checked-in
   file, `dev/focused_tests_overrides.toml`, maps modules to tests. An entry has a mode, `add`
   (extra tests on top of the computed ones) or `replace` (only the named tests), and an
   optional cap. A hub module is handled by a `replace` entry that names the tests that actually
   exercise it; `hub_cap` bounds what an unlisted hub can pull in.
2. **Ranges.** A positional `A..B` selects the files changed by `git diff A...B` (changes on
   B since the merge base), so a branch is gated against its base rather than against the
   working tree. With no range the selection is from the working tree and index as before.
3. **Caps, priority-based truncation, never silently the full suite.** `[limits]` in the
   override file sets `max_files` (selected test files, default 60), `max_jobs` (default 4) and
   `hub_cap` (test files one changed module may pull in, default 40; a `[[module]]` entry's own
   `cap` overrides it). A selection over a cap is truncated by priority (override entries
   first, then the changed test itself, tests named for a module, importers, then dependents),
   and the output says what was cut and why. A change that maps to no test is reported as
   unmapped, and when nothing at all can be selected the command stops with status 2 and says
   to run the full suite explicitly; it never widens on its own. The full suite runs only when
   `--full` is passed, and `--full` requires `--run`.
4. **A fixed smoke set.** `[smoke] files` in the override file names a small, fast, fixed set
   that is appended to every non-empty selection (`--no-smoke` omits it, `--smoke-only` runs only
   it). It replaces the hand-picked smoke set and is reviewed like code.
5. **A concurrency cap.** Selected tests run as at most `--jobs N` pytest processes (default 1),
   each shard with its own basetemp and log. `N` is clamped to `max_jobs`, so a shared host is
   not saturated by a focused run.
6. **Loud validation of the override file.** An unknown key, a missing `match`, a pattern or test
   path that matches nothing, a mode other than `add` or `replace`, a `replace` entry without
   `tests`, or a cap that is not a positive integer is an error that names the entry and stops
   the run with status 2. A stale override must fail rather than quietly select nothing.
7. **The command.** `tessera dev focused-tests [RANGE] [--jobs N] [--list|--run]
   [--smoke-only] [--no-smoke] [--max-files N] [--full] [--explain] [--root DIR] [--base REV]`.
   The default action lists the selection with a reason for each test (`--explain` shows the
   reasons per file); `--run` executes it under the cap.
   `dev/focused_tests.py` stays as a thin wrapper over the same code.
8. **An upgrade preflight step.** `tessera deploy-upgrade` accepts optional plan fields
   `from_revision`, `focused_tests` (boolean, disabled by default), `focused_tests_jobs` (1 to 4,
   default 2) and `repo_root`. When enabled it adds a preflight finding `tests.focused`:
   - a dry run lists the selection (`from_revision..source_revision`) and
     runs nothing;
   - under `--execute` the selection is run during preflight, before any service stops, before
     the backup and before the migration, so a failure costs no downtime;
   - a failing run, a selection that cannot be mapped (changed files and no test for any of
     them), a range git cannot resolve, or a checkout that is not at `source_revision` blocks the
     upgrade; it never falls back to the full suite;
   - when no repository checkout exists on the host (packaged installs), the step is skipped
     with a clear note rather than passed;
   - the receipt carries a `focused_tests` key only when the step is enabled, so existing
     receipts do not change.

## Consequences

- The working gate is small, explainable and bounded: every test in it has a reason, and a hub
  change selects the tests that matter instead of half the suite.
- The override file is a maintained artifact. A hub missing from it still works (it falls back
  to the computed map and `hub_cap`), but is only as precise as the import text.
- A host cannot be saturated by a focused run, and nothing runs the full suite by accident.
- An operator who enables the upgrade step gets a test gate that cannot cost downtime, at the
  price of a longer preflight and of needing a checkout on the host.
- Limits: selection is static (names and import text), so dynamic imports and data-driven
  coupling are only caught by overrides or the full pre-merge run, which stays required once per
  branch.

## Alternatives considered

- **Coverage-based selection.** Precise, but needs a coverage database per commit, which is
  slow to build on the shared hosts and stale after any refactor; it also hides coupling that
  coverage does not record.
- **A cached map file.** Faster, but a checked-in generated map goes stale and conflicts across
  branches, and an untracked cache can silently select from an old tree. Computing the map is
  cheap enough; only the small hand-written override file is checked in.
- **Running the full suite.** Correct and too heavy for the working loop; it stays the
  once-per-branch pre-merge run (`make test-parallel`).
- **pytest-testmon or pytest-xdist.** Both add a dependency, and testmon keeps a per-checkout
  database that is not shared or reviewable. The repository already runs disjoint shards
  without xdist, and the concurrency cap is simpler to reason about.

## Open questions

- Per-test durations are needed to tune the smoke set: which tests are fast and still cover the
  hubs. Until then the set is chosen by hand and revisited with measured data.
- How to keep the override file current: a check that flags a hub module without an entry, or an
  entry whose tests no longer import it, could run in CI.
