# 0211 — A checkpoint retry's input cap grows by the checkpoint's recorded bytes

## Status

Accepted. Narrows [ADR 0197](0197-salad-post-start-interruption-retry.md) for adapted Salad stages. No schema
change and no migration. Claim, admission, spend, cancel and sealing guards are unchanged.

## Context

A checkpoint-resume retry composes a new input snapshot: the stage's base inputs plus the
checkpoint the interrupted attempt left in custody. An adapted Salad stage declares its input in
the reviewed workload contract as a digest and a byte cap (`input.max_bytes`). For a stage that
pins its input, the adapter sets that cap to exactly the base inputs' bytes, so the composed
snapshot was always over the cap and admission refused the retry at route approval, after a lease
and packaging, with an over-cap reason that did not mention the checkpoint.

## Decision

On a retry whose current snapshot was composed with a checkpoint, the stage's contract is bound
to the composed digest and `input.max_bytes` becomes the reviewed cap plus the checkpoint's bytes.
Those bytes are read from the receipt recorded when the snapshot was composed (the checkpoint's own
custody manifest entry, already bounded by its declared output size), never from the caller.

The raised cap must still fit the operator profile: at most the stage adapter's `max_input_bytes`
when set, and the declared fetch bytes plus the cap within `disk_mb`. This is checked right after
the retry composes, before any lease or hold. Otherwise the stage fails closed with
`salad_checkpoint_over_cap`, naming the checkpoint output and the limit. Admission still refuses a
staged input above the (raised) cap, so nothing the operator limits would allow is exceeded.

A retry with no checkpoint (or one back on the base composition) keeps the reviewed cap and, for a
pinned contract, the reviewed digest.

## Consequences

This ADR does not make a checkpoint retry route. A checkpoint retry of a pinned-input adapted stage
still fails closed at the grant step: the approved task plan binds the base snapshot digest, so the
composed snapshot is refused with "Input snapshot digest does not match the approved plan", and
route start would also refuse the reviewed argv `input:` token for the same reason. No grant, hold or
charge is made. Revising the plan on a checkpoint retry is a separate follow-up.

What this ADR does change: the contract the retry would carry names the composed snapshot with a cap
that fits it, so the cap is no longer a second reason to fail, and a checkpoint that cannot fit the
operator profile is a clear, early failure naming the checkpoint instead of a late over-cap refusal.
The reviewed contract in the spec is unchanged.
