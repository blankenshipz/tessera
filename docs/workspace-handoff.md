# Confirmed workspace handoff

Schema v17 records a bounded local workflow: a human requests a stop for one
current project/task/grant/lease and selected, already captured baseline snapshot;
the request cancels the grant and advances its authority epoch. The record contains
only a human label and captured snapshot metadata. It accepts neither a filesystem
path nor workspace bytes, and exposes no generic desktop IPC or privileged write.

A human may confirm takeover only after the latest matching authenticated worker
observation is `exited` with `termination_confirmed`, its bound worker/lease/
epoch and a stable provider identity. Unknown, stale, mismatched, unconfirmed, or
continuing writer authority blocks confirmation. Confirmation fences later worker
observations and leaves the human as the recorded workspace owner.

For handback, the owner supplies a separately captured immutable result snapshot and
the baseline/result digests. The control plane verifies snapshot/project/digest
matches, records added/deleted/changed paths plus capture exclusions, permits an
empty diff, and appends a fresh context epoch. Capturing is performed through the
existing descriptor-safe workspace capture path, which rejects mutation while reading.
Snapshots are never overwritten.

Handback does not issue a grant, revive the cancelled grant/lease, dispatch a
provider operation, release data, accept a result, or make a scientific claim. Any
new agent work still needs the existing approval and execution-admission flow.
Arbitrary shared-writer mediation, full desktop workspace adoption, and provider
quiescence beyond the attributed worker observation remain unimplemented.
