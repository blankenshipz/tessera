# ADR 0131: a campaign result compares named baseline and intervention arms

Status: Implemented (no schema change). Builds on ADR 0115 (plain-words campaign report) and ADR 0038 (campaign runner).
Read-only. No schema change.

## Context

The research ledger already compares a baseline and an intervention measurement
(`experiment_comparison.py`), with an uncertainty interval where several attempts exist, and gates
pair variants on a split lock. A campaign receipt records every stage's attempts, inputs and
measurements, but it could not name which stages are the arms, and nothing compared them with
receipts behind the numbers. A reader had to pair stage results by hand and could not tell whether
the two arms were scored on the same evaluation data.

## Decision

1. `GET /campaigns/{campaign_id}/comparison` reads a campaign's immutable terminal receipt
   (`GET /campaigns/{id}/receipt`, ADR 0038 and the receipt section of the campaign docs). It is
   read-only and returns 404 until the campaign is terminal. Query parameters:
   - `baseline`: one stage key;
   - `intervention`: one or more stage keys (repeatable);
   - `metric`: one or more measurement names (repeatable);
   - `evaluation_input`: one or more input names (repeatable; at least one is required).
2. The arms are stages of one campaign receipt. Each arm must have exactly one succeeded attempt
   (the same rule the campaign gates use for measurement conditions). Failed, cancelled or extra
   attempts are not results and are not chosen between, so there is no uncertainty interval: the
   comparison is arithmetic on the recorded numbers.
3. Every named evaluation input must be identical across the arms. Its identity is the io item's
   `sha256`, or, for a snapshot input without one, the snapshot identity the io item records; an
   input with no recorded identity is refused. Arms that used different evaluation data are never
   compared.
4. Each metric must be recorded exactly once per arm attempt and must be a finite number (not a
   boolean). The delta is intervention minus baseline.
5. Refusals are 409 with `detail` `comparison_refused:<code>: <message>`, one stable reason code each (callers in Python read `.code`). Missing required query parameters, and lists over the bounds (8 interventions, 16 metrics, 8 evaluation inputs), are rejected by request validation (422) before the comparison runs. Refuse, never drop:
   `arms_not_distinct` (the baseline is also an intervention, or an intervention is repeated),
   `too_many_arms` (more than 8 interventions), `too_many_metrics` (more than 16), `no_intervention`,
   `no_metric`, `stage_missing`, `arm_attempt_not_unique`, `evaluation_inputs_unnamed`,
   `evaluation_input_missing`, `evaluation_inputs_differ`, `split_lock_differs`,
   `spec_unavailable`, `spec_digest_mismatch`, `metric_not_recorded`, `metric_not_numeric`. A repeated metric name is
   refused as `too_many_metrics`.
6. **The check does not trust the caller's list.** The route also reads the approved spec whose
   digest the receipt carries (the receipt must name the stored spec, else `spec_unavailable`) and
   adds what each arm declares: every arm must have the same `gate_evidence.split_lock_id` (one arm
   with a lock and another without counts as different: `split_lock_differs`), and the held-out
   input an arm declares (`evaluation.holdout.input`) is compared across all arms like a named one,
   so two arms cannot be made to look comparable by naming only an input that matches. The
   stored spec is verified on read: the receipt's digest must equal the stored `spec_digest`
   column, and the stored `spec_json` is parsed and re-hashed exactly as at submission and
   approval, which must give that digest. A spec that no longer hashes to it is refused as
   `spec_digest_mismatch` and never read; one that cannot be parsed cannot be verified and is
   refused as `spec_unavailable`. What each arm declares comes from the verified parsed spec, not
   from the raw text. This guards the comparison only. The database does not stop an edit (the
   schema has no trigger on `campaigns`), and other readers of `spec_json` do not yet verify it
   on read; the platform itself writes the spec columns once at submission and no code path
   updates them afterwards.
7. The result carries the campaign id and receipt digest, a plain-words statement of the method,
   each arm's stage key, attempt, evidence kind and digest and run receipt digest, the named
   evaluation inputs with their identical identity and the arms that used them, and per metric the
   baseline value and each intervention's value and delta. It carries `scientific_validity:
   "not_assessed"` and **no** decision, verdict or recommendation.

## Consequences

The comparison states what the receipts record and nothing more: it does not say an intervention
worked, and a difference is not evidence of an effect. Scientific judgement stays with a person,
through the research ledger's conclusions and result assessments.

Not covered:

- arms from different campaigns (compare the campaigns' receipts or use the research ledger);
- more than one attempt per arm (use the experiment comparison, which has the uncertainty interval);
- pairing arms by seed or by replicate (use paired replicate groups).

## Tests

`tests/test_campaign_comparison.py` (the pure rules: every refusal code, a succeeded and a failed
attempt on the same stage, an evaluation input whose identity differs, a metric recorded twice, a
non-numeric metric, the declared split lock and held-out input, and the route: 200, 404 without a
receipt, 409 for differing inputs, differing split locks, a split lock on one arm only, an unnamed
held-out input that differs, and a receipt that names a spec that is not the stored one) and
`tests/test_campaign_comparison_real_receipt.py` (the same comparison on a receipt written by the
real campaign receipt builder from a campaign the harness ran to a terminal state, and the
non-terminal and other-tenant refusals).
