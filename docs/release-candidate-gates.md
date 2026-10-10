# Exact-source candidate gate runner

`scripts/run_release_candidate_gates.py` runs only the three
CONTRIBUTING standard local gates, with the current interpreter:

| ID | Fixed command suffix | Proof |
| --- | --- | --- |
| ruff-lint | `-m ruff check .` | Local source lint only |
| ruff-format | `-m ruff format --check .` | Local source formatting only |
| python-tests | `-m pytest -q` | Local executable suite; provider/device effects retain their test-specific synthetic/opt-in boundaries |

It does not install dependencies, run Docker/providers, inspect credentials or
invoke arbitrary manifest commands. **Do not execute full gates while source
writers are active.** The parent must freeze the candidate and explicitly admit
the run with `--parent-frozen`. That flag is a self-reported admission, not proof
that every writer stopped; exact source hashes and scope checks still apply.

## Packaging owner handoff

The actual packaging tools are `dev/build_release_candidate.py` and
`dev/check_release_candidate.py`, documented in
[release-candidate.md](release-candidate.md). The release owner provides the reviewed
tracked + intentional-new gate inventory and pinned artifact references using
this closed JSON schema:

```json
{
  "schema": 1,
  "source_paths": ["CONTRIBUTING.md", "pyproject.toml", "src/example.py"],
  "source_sha256": "64-lowercase-hex-canonical-source-digest",
  "gates": ["ruff-lint", "ruff-format", "python-tests"],
  "artifacts": [{"subject": "candidate-wheel", "sha256": "64-lowercase-hex"}]
}
```

The sample paths/digests are schematic, not a runnable real candidate. Use
`manifest(root, source_inventory(root), artifact_references)` to construct the
actual manifest; inspect/approve any intentional-new paths before doing so.
Source revision hashes the sorted map of relative path to file SHA256 using
canonical JSON (`sort_keys=True`, compact separators). This map includes filenames
as well as bytes; it is not the SHA256 of a tar archive. Do not relabel a builder's
different archive hash as this revision. The builder's `input_revision` hashes a
list of `{path,size,sha256}` entries, whereas this runner hashes a map of path to
SHA256; these are distinct revision identities even for identical file selections.
The runner's gate inventory is broader than the builder's extension allowlist;
do not silently supply a narrower package list as complete Ruff/pytest source.
Artifact hashes are safe references only:
the report says `artifact_proof:references_only`, never that a wheel/install passed.

Source roots are `src`, `tests`, `scripts`, `docs`, `desktop`, `skills`, `examples`,
`.agents`, `dev`, `deploy`, `plugins`, `.github`, plus explicitly named root source/
contract files. Git inventory uses tracked and nonignored new filenames, and the
manifest must cover every allowed inventoried source path. Runtime/state/auth/.env,
build/dist/caches, credential headers/keys/PEM/SQLite and symlinks are excluded or
refused before bytes are hashed. Files are bounded to 64 MiB each / 512 MiB total,
10,000 paths and a 1 MiB input manifest. Source outside these roots needs an explicit
runner contract update, not silently missing coverage. Ignored runtime/artifact
directories are never made source by an artifact reference.

The `release/` exception consists of two exact, closed observer bundles:

- v2 file-pinned layout `release/opencode-observer/`: `README.md`, `baseline.json`,
  `LICENSE`, `observer.patch`, and `baseline.tar.gz`.
- runtime-v8 layout `release/opencode-observer/runtime-v8/`: `baseline.json`,
  `baseline.tar.gz`, `observer.patch`, `LICENSE`, `BASELINE.sha256`, `TARGET.sha256`,
  `origins.json`, `README.md`, `reconstruct.py`, `link_runtime.py`,
  `verify_runtime.mjs`, `verify_python.py`, `runtime-deps/package.json`, and
  `runtime-deps/bun.lock`.

The runner admits only these explicit paths under `release/`; a present bundle's
listed paths are included even when Git ignores them. No arbitrary release/runtime,
private guard, nested vendor or unlicensed extra source is admitted. Additions
require coordinated allowlist updates, not a broad release root. This gate hashes
the baseline archives as opaque bytes; it does not extract them or prove contents,
license coverage or patch applicability. The package owner's separately pinned
checks in `dev/observer_snapshot.py` perform those bundle checks.
Both `dev/build_release_candidate.py` and `dev/observer_snapshot.py` are ordinary
default gate inventory inputs, not runtime artifacts.

After the parent freezes every writer, set `EVIDENCE_DIR` to an existing private
directory outside the frozen source inventory. This constructor command emits the
gate manifest outside the source inventory (artifact references may be added only
using the closed subject/SHA256 schema above):

```sh
.venv/bin/python - <<'PY' > "$EVIDENCE_DIR/rc-final-gate-manifest.json"
import importlib.util
import json
from pathlib import Path
root = Path.cwd()
spec = importlib.util.spec_from_file_location("rc_gates", root / "scripts/run_release_candidate_gates.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
print(json.dumps(runner.manifest(root, runner.source_inventory(root)), sort_keys=True))
PY
```

Do not substitute builder `input_revision` or archive SHA256 for this constructor's
`source_sha256`. The final execution then uses the above manifest with
`--parent-frozen`; constructing it alone does not run Ruff/pytest.

```sh
# Shape/scope/revision inspection only: no full commands without the freeze flag.
.venv/bin/python scripts/run_release_candidate_gates.py --root . --manifest /owned/candidate-gates.json

# Parent-only fresh admission after all candidate writers freeze:
.venv/bin/python scripts/run_release_candidate_gates.py --root . \
  --manifest /owned/candidate-gates.json --parent-frozen
```

The interpreter must already contain the repository's applicable dependencies.
Standard commands are each bounded to one hour and run sequentially. A timeout
kills only that command's own newly created process group; no global cleanup or
service signalling is performed. A still-open output pipe is a failed receipt,
not an assumed clean exit. Native exit codes and sanitized finite categories are
recorded; missing executables/modules, nonzero exits and missing/zero-test summaries
cannot pass. Skipped/deselected/xfailed/xpassed coverage is explicitly incomplete,
not full pytest acceptance.

## Evidence semantics and privacy

Before/after source SHA256 equality and scope equality are required. Changed,
removed or newly added included source invalidates passing receipts. An omitted
allowed source path blocks admission. Commands do not continue to qualify their
old passing evidence against a new revision. Reports hold only fixed gate IDs,
native exits, finite categories, counts, safe source/artifact digest references
and explicit modes/statuses. Raw stdout/stderr, traceback bodies, prompt/auth
text and arbitrary error messages are not persisted or emitted. Transient output
is parsed with a bounded line buffer and discarded; only recognized pytest final
summary counts survive.

Injected executors are isolated-test seams: their rows are `synthetic` and an
all-positive synthetic run is `synthetic_only`, never a native local pass. Native
local gate success exits 0; an unrun, synthetic, incomplete or invalidated local
gate exits 2. Even exit 0 means **local source gates only**, not release acceptance.

The matrix always retains `installed-mcp-entrypoint`, `installed-a2a-entrypoints`,
`native-picker`, `offhost-execution` and `remote-reasoning` as `live/not_run`.
`full_acceptance` remains `not_run`; a source-only run cannot set these passed.
Installed clean-package/secret-exclusion/observer compatibility checks remain the
packaging/protocol owners' separately revision-bound receipts. Parent convergence
joins those receipts and all real journey/admission/device/offhost evidence; this
runner neither closes product acceptance nor transforms historical evidence into
current candidate acceptance.
