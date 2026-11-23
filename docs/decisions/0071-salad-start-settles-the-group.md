# 0071 — A fresh Salad group is settled before it is started

## Status

Implemented (no schema change). Narrows [ADR 0062](0062-salad-cpu-only-profiles.md)'s live
check and the start admission of [ADR 0036](0036-generic-salad-gpu-workloads.md); every spend,
guardian and data rule is unchanged, and no new network surface or destination is added
([ADR 0047](0047-agent-routed-data-destinations.md) holds).

## Context

A live check of a CPU-only profile created its group (accepted, so Salad takes a group with no GPU
class) and then refused its own start every time: `start` re-reads the group and refuses unless it
reads `stopped`, with no change in flight and every instance count zero, and a group that has only
just been created does not yet read that way. The same admission sits under the campaign
executor's start. Two further things hid the cause: the refusal recorded no observation, and a
CPU-only group may report its instance counts differently from a GPU group (fields absent), which
the strict parse treated as malformed.

## Decision

1. **Settle before start, with reads only.** The transport's `start` reads the group again a few
   times (`START_SETTLE_SECONDS`, 10 s, one second apart) while it reads `pending` or `stopped`
   with a change still in flight and nothing running, and admits the start only on a settled read
   (stopped, no change in flight, no instance). A live (allocating, creating or running) instance,
   or any other state, is refused at once. The one start POST is never repeated, the claim rules
   are unchanged, and the wait is short so the executor's heartbeat and the guardian's silence
   bound are not stretched.
2. **Create accepts a transitional group.** A `201` that reads `pending`, or `stopped` with a
   change in flight, and shows nothing running is reported `accepted_pending` (the group's
   identity is verified as before); a group with a live instance or any other state is still
   refused. The caller settles it by reading.
3. **Callers poll within their bounds.** The CPU live check reads the created group until it is
   settled, for at most 60 s (a stopped group is not billed, so this is outside the spend ceiling
   and the settle reserve), and refuses to start a group that never settles, still stopping and
   deleting it. The campaign executor's pre-start poll waits for the guardian's settled reading
   (`termination_confirmed`, true only for a stopped group with no change and no instance) for up
   to 45 s after the group first reads stopped, then hands the start to the transport, whose own
   read decides. A session that reports no such signal is not waited on.
4. **A refusal says why.** An `admission_denied` start carries, in its observation and on the live
   check's receipt, the observed state, the change-in-flight flag, the instance counts and the
   count key names Salad sent (count-shaped names only, at most 16, 40 characters each): no body
   and no secret. They sit under a `refusal` key of the observation (so nothing downstream can read a refusal as a group readback) and the receipt flattens them with the same `state` key the read steps use. The live check's receipt also
   records the settle poll (count, last state, whether it settled) and, on its `start` step,
   whether the group reported all four counts.
5. **Counts a CPU-only group omits.** Absent, null or empty `instance_status_counts`, and a missing
   known key, read as zero instances; a known key that is not a non-negative integer, or a
   non-object, is still malformed (fail closed), and an unknown `*_count` key with a positive
   integer counts as a live instance. The observation records whether all four counts were
   reported (`instance_counts_complete`); **a stop is never confirmed from counts Salad did not
   send**: every place that confirms a stop (the guardian's terminal reading in the trusted server,
   and the safety service's reconcile and its own observation) needs the complete set, so a CPU
   group that omits its counts is started but is not reported as a confirmed stop until a live run
   shows what Salad sends for one.
6. **The credential by reference.** `scripts/salad_cpu_live_check.py` reads the management
   credential either from a token-only 0600 file (as before) or from the operator's credential
   directory by reference and version (`--credential-dir`, `--credential-ref`, default
   `credential-ref:salad-management`, `--credential-version`), through the same private backend the
   rest of the Salad stack uses (a 0700 directory of 0600 records; a missing, revoked, expired or
   other-version record is refused without saying which). Exactly one source is accepted; the
   secret stays in a wiped buffer and never reaches the receipt.

## Consequences

- A CPU group created accepted-pending is started once it settles, from the live check and the
  executor, with no change to what may start (the same stopped, no-writers admission) or to cost
  bounds.
- A group Salad never settles is not started, and the receipt names the state, the change flag and
  the count keys instead of only "admission denied".
- For such a group the executor's pre-start poll never sees the guardian's settled reading and
  waits its full 45 s before every start (bounded, heartbeat kept); that is the honest cost of not
  confirming from counts that were not sent. The live-check receipt records the wait
  (`prestart_settle.waited_seconds`), so its cost is visible; revisit the bound once the first
  live CPU run shows what Salad sends for such a group.
- Both places that read the counts (the loop's own observation and an operator reconcile) refuse an
  incomplete read. Both guards are pinned by tests: the observation guard by the one added with
  this change (run with the allocation unchanged so nothing else masks it), the reconcile guard by
  an earlier one. The settle block on the receipt is a closed set of plain values.
- Not covered: whether Salad reports CPU group counts the way the fake here does has to be
  confirmed by a live run; the tolerant parse fails closed on every real instance count it can
  see.
