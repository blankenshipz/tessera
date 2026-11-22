# 0104 — A conclusion records a researcher's decision and a stance on each evidence link

## Status

Implemented (schema 78: additive nullable columns). Extends the human-only conclusions of the
records API and keeps `scientific_validity` at `not_assessed`.

## Context

The workflow's last step is to "record whether to continue, revise or reject the idea, with links
to the supporting and opposing evidence". A conclusion could carry a statement and cited
evidence, but not the decision itself, and a cited link did not say whether it was cited for or
against the statement. A reader had to infer both from prose.

## Decision

1. **Decision.** `research_conclusions.decision` is nullable: `continue`, `revise` or `reject`.
   It may be set when a conclusion is created and while it is a draft (update). Once the
   conclusion is reviewed it is immutable with the rest of the reviewed record; a correction is
   an amendment or a new conclusion, as before.
2. **Stance.** Each evidence link (`research_conclusion_evidence` and the admitted-attempt
   variant) gets a nullable `stance`: `supports` or `opposes`. `EvidenceCitation.stance` is
   optional on conclusion create and update bodies and is inherited by that citation's artifact
   and measurement links. Responses and export show the decision and each link's stance.
3. **NULL means unstated.** A link with no stance is "unstated", not "supporting". Rows that
   existed before the migration keep NULL: they are not relabelled, because nobody recorded a
   stance for them and writing one would invent a record.
4. **Additive and nullable.** The migration (schema 78) adds the columns idempotently, with no
   backfill and no default. Absent a decision or stance, every response, export and stored row
   keeps its meaning. The agent-results `conclusions` section shows `decision`.
5. **Basis digest.** The program basis includes `decision` only when it is set, so the digest of
   every existing basis is unchanged.
6. **Authority.** Conclusions remain human-only and a reviewed conclusion remains immutable. A
   recorded decision is a researcher's record. It is not an approval, a grant of authority or a
   gate, and it is not scientific validity: `scientific_validity` stays `not_assessed`.
   Traceability is not scientific validity, and a stance marks how a person cited a link, not
   whether the link establishes anything.

## Not done

- No rule requires a decision (or any stance) before review.
- No automatic decision: Tessera never infers `continue`, `revise` or `reject` from comparison
  numbers, stances or any other record.
- Agents cannot write conclusions, decisions or stances; the human-only boundary is unchanged.
- No relabelling of legacy links, and no consistency check between a decision and the stances
  (a `reject` citing only supporting links is allowed; it is the researcher's record).

## Consequences

- The workflow's "continue, revise or reject, with supporting and opposing evidence" is
  recordable and exportable instead of living in prose, while remaining a record only.
- Readers must treat NULL stance as unknown. Consumers that previously assumed all cited
  evidence supported the conclusion were never entitled to that reading.
- Because the decision is optional, a missing decision is not a finding.

## Tests

- `tests/test_conclusion_decision.py`
- `tests/test_conclusion_decision_migration.py`
