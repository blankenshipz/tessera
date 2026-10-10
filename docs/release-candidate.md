# Source candidate packaging (not accepted release)

The RC builder is offline, deterministic and allowlisted. It does not deploy,
publish, commit, read active credentials or grant execution authority. Each
candidate needs a frozen inventory and candidate-bound checks; earlier builds
do not validate later source/documentation changes.

Final source capture waits for all writers, the runtime owner's approved public
observer bundle inputs and the parent's explicit freeze. Until then only labelled
development smoke artifacts may be built; they are not the pinned final candidate.
Set `BUILD_OUTPUT_DIR` to an operator-owned directory outside the source inventory.

```sh
.venv/bin/python dev/build_release_candidate.py build \
  --root "$PWD" --output "$BUILD_OUTPUT_DIR/rc1" \
  --version 0.1.0rc1 --source-date-epoch 1767225600
```

An RC version and SOURCE_DATE_EPOCH are mandatory. Input file hashes, exact input
revision, version, epoch, observer baseline/patch/license pins and artifact hashes
are recorded in manifest.json. The original project version is not changed: only
the allowlisted staging copy receives the candidate version. Source archive gzip/
tar metadata and wheel ZIP timestamps/order/modes are normalized. Build twice with
the same inputs, epoch and pinned local setuptools version to compare artifact
hashes. No output directory is overwritten. The source archive is the complete
selected input handoff, not a setuptools sdist; wheels contain code, entrypoints,
portable skill, operator docs and observer handoff files.

Allowlist: Python source/tests/scripts/dev, Markdown docs/skills, plugin source,
example Go/Markdown and public deploy assets; explicit root package/build/contracts.
The observer distribution has two distinct closed bundles:

- v2 file-pinned bundle at `release/opencode-observer/`: exactly `README.md`,
  `baseline.json`, `LICENSE`, `observer.patch`, and `baseline.tar.gz`.
- runtime-v8 closure at `release/opencode-observer/runtime-v8/`: exactly
  `baseline.json`, `baseline.tar.gz`, `observer.patch`, `LICENSE`, `BASELINE.sha256`,
  `TARGET.sha256`, `origins.json`, `README.md`, `reconstruct.py`, `link_runtime.py`,
  `verify_runtime.mjs`, `verify_python.py`, `runtime-deps/package.json`, and
  `runtime-deps/bun.lock`.

The runtime-v8 directory is a path-specific closed distribution, not a recursive
release-root allowlist; retain its pinned input/target/license and provenance
requirements. The v2 archive is used only for v2 file-pinned mode.
Generated egg-info, runtime/auth/env/credentials/receipts/worktrees/cache/log files
are excluded. Inputs are ordinary files, not symlinked custody; hashes/path checks
do not prove arbitrary source or patch content is secret-free. Review the owner's
public patch and manifest before capture; never supply private guard source.

For an initial smoke only, `--development-snapshot` explicitly allows a missing
observer bundle, marked `development_snapshot:true` and candidate-only. Final mode
always validates the top-level observer inputs `baseline.json`, `LICENSE`, and
`observer.patch`; a v2 file-pinned baseline also requires `baseline.tar.gz`. In
addition, final versions other than exactly `0.1.0rc1` require the closed runtime-v8
14-file bundle and its pin/provenance contract. These requirements are additive,
not alternative bundle selections: a final `0.1.0rc2` build requires both. See the
bundle READMEs for staging interfaces. No worktree auto-copy.
An actual nongit source snapshot uses explicit v2 file-pinned provenance plus the
small deterministic baseline.tar.gz and original file/license SHA maps, never a
fabricated upstream commit. Such pins do not independently verify upstream identity.

```sh
.venv/bin/python dev/check_release_candidate.py /path/to/candidate.whl \
  --dependencies /path/to/existing/locked/environment/lib/python3.11/site-packages
```

The smoke creates a new temporary environment, references only third-party local
dependencies without editable .pth/Tessera code, installs the wheel with no-deps/
no-index, checks actual import origin, five entrypoint help contracts, skill/docs
and MCP resources. It never starts a service or research session. It is not an
independent dependency solve/offline installer bundle: record the exact local
dependency versions separately and test clean installation with authorized locked
dependency acquisition at the final packaging gate if required.

## Acceptance boundary

Successful packaging produces a **source candidate**, never an accepted release.
Verify input exclusions, bundle reconstruction/provenance, deterministic rebuild,
installed entrypoints/docs/resources and applicable source/protocol gates against
the final frozen inputs. Installed reasoning/admission, native dialogs and
independent-host/laptop-off execution need separate real proof. Keep receipts
revision-bound outside shipped artifacts; see the [release checklist](release-checklist.md).
