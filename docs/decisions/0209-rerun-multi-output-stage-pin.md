# 0209 — A re-run pins several outputs of one stage as one reviewed folder

## Status

Accepted. Amends [ADR 0069](0069-rerun-a-finished-campaign.md) section 3 (the "several outputs
of one stage read by one input" refusal). Every other ADR 0069 refusal, and ADR 0074's spend
bound, are unchanged. No schema change and no migration: the new input kind is validated JSON in
the spec, and existing spec digests do not change.

## Context

A `kind: stage` input that reads several outputs of one stage mounts each of them under the input's
name, at the producing stage's declared output path:

```
/inputs/prep/cd_lock.json
/inputs/prep/surfactants.csv
/inputs/prep/labels_16e84db3.json.gz
```

and a workload that is given `--inputs /inputs` expects exactly that layout. A `failed_stages`
re-run (ADR 0069) replaces an input whose producing stage already succeeded with a `kind: artifact`
pin. One `artifact` input holds one file, mounted at `<input name>/<path>`, so three separate
pins named after the outputs would mount `/inputs/cd_lock/cd_lock.json` and so on and break the
workload. An input name is a key and cannot contain a slash. ADR 0069 therefore refused the re-run
and asked a person to write the pins by hand. No hand-written spec could preserve the layout either,
so a failed stage after a multi-output preparation stage could not be re-run at all without
running the preparation again.

## Decision

1. **New input kind `artifact_folder`.** A v2 stage input

   ```json
   {"kind": "artifact_folder", "name": "prep", "campaign_id": "<source campaign>",
    "stage": "<producing stage>",
    "files": [{"output": "cd_lock", "path": "cd_lock.json", "sha256": "<64 hex>"}, ...]}
   ```

   pins two to 128 outputs of one stage of one earlier campaign under one input name. Each file
   mounts at `<name>/<path>`, which is the layout the original `kind: stage` input had. The
   spec the person reviews names the source campaign, the producing stage, and every file's output
   name, exact relative path and sha256, so the approver sees the mount layout and the bytes
   without guessing.
2. **Paths are declared, never invented.** A file path is relative POSIX, non-empty, with no empty,
   `.` or `..` segment, no leading `/` and no backslash. Output names and paths are unique inside
   the folder, and no path is a directory of another. At submit, approval and composition, each
   path must equal both the path the source campaign's spec declares for that output and the
   output path recorded for it. Anything else is refused.
3. **Every file resolves as an `artifact` pin.** Each file goes through the same resolution as an
   `artifact` input. The producing attempt succeeded. The source campaign's terminal receipt (or
   its sealed recovery addendum) lists the output with that sha256. The manifest is in the same
   project, retained and custody-released. All files come from one manifest, and the runner
   composes it as one source under the input's name. If any one file fails, the whole input is
   refused. Nothing is pinned in part.
4. **What the re-run builder does.** In `failed_stages` mode, an input that read several outputs of
   a succeeded stage that is not re-run becomes one `artifact_folder` input, with the same name,
   each output's declared path and its one recorded digest. A single output is still an
   ordinary `kind: artifact` pin, with the same fields and digest as before. The builder's per-file
   refusals are those of ADR 0069: the producing stage must have succeeded and not be re-run, and
   the output must be custody-released and retained with exactly one recorded digest. Any one
   failing file refuses the re-run. The receipt's `rerun.reused_outputs` and the
   `campaign.rerun_submitted` event list every pinned file, with stage, output and sha256. A
   folder file also records the `input` it belongs to and its `path`. A single-output entry keeps
   its old shape.
5. **Readers.** Every reader that handles `kind: artifact` handles each file of a folder the same
   way:
   - submit and approval resolution;
   - the Salad stage adapter's input plan, which composes `<name>/<path>` per file, so the planned
     snapshot digest is the one the runner composes;
   - runner composition, input index and lineage;
   - holdout and seal checks;
   - licence-label lineage;
   - the heavy-input size estimate;
   - compose-step manifest ownership.

   A folder is always `private_derived`, so the stage's Salad data policy is the trust policy.
6. **Data policy and exceptions.** A re-run is an ordinary submission (ADR 0069 section 2). The
   reference, provider trust, data-class, licence, holdout, seal and grant-lifetime checks run on
   the new spec and on each digest it pins. The platform has no data-policy exception that is
   bound to an input digest. The only bindings are the person's approval of an exact spec digest
   and the provider trust setting digest in force at that approval. Neither carries from the source
   campaign to the re-run. Approving the source digest approves nothing here. A digest that the
   source stage did not record for that output does not resolve. If a digest-bound exception is
   added later, it must be bound to the folder file's own sha256. It must never be inherited from
   the source campaign's spec or approval.

## Consequences

- A failed stage downstream of a multi-output preparation stage can be re-run without running the
  preparation again, with the same `/inputs/<name>/...` layout, and a person still approves the
  new digest.
- Existing specs and `artifact` pins keep their serialization and digests. A spec that uses
  `artifact_folder` is refused by an older control plane, so it cannot be misread.
- Hand-written folder pins are allowed, under the same checks. A `receipt_output` reference
  still names one output; a folder reference by receipt is not part of this decision.
