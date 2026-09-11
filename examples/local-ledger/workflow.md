# A synthetic public workflow

**Question:** Does changing the label order alter a toy classifier's reported
accuracy?

**Baseline:** Record a fixed, invented input description and a baseline label
order in a local ledger.

**Intervention:** Register one alternative label order before recording any
outcome.

**Synthetic observation:** A human may record that the demonstration is
inconclusive. No command, dataset, model, provider receipt, artifact, or metric
is supplied by this example.

**Decision:** Preserve the baseline, intervention, and inconclusive observation
as separate historical records. Do not infer execution, provider success, cost
settlement, artifact acceptance, or scientific validity from the narrative.

Use this as a terminology aid only. A real workflow needs the acceptance gates
in [implementation progress](../../docs/implementation-progress.md), including
reviewable evidence and explicit human decisions.
