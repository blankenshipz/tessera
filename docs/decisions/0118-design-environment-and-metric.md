# 0118 — A design records its environment digest and headline metric

## Status

Implemented (schema 83: two additive nullable columns). Builds on the design revisions
described in [records-api.md](../records-api.md).

## Context

README step 1 promises that the researcher identifies the script, the input snapshot, the
environment, the metric and the success criterion. A design revision had typed digests for
the script and the input snapshot, and a free-text success criterion, but nowhere to say
which environment the work ran in or which single metric the criterion is judged on. Both
ended up, if at all, in free text that a reader could not find or compare.

## Decision

1. **Environment digest.** `DesignVariant.environment_digest` is an optional sha256 digest of
   the environment (an image, a lockfile or a requirements file). The platform never
   interprets it.
2. **Headline metric.** `CreateExperiment.metric` is the optional name of the single headline
   metric the success criterion is judged on. It is also accepted on a design revision.
3. **Immutable per revision.** Both are stored with the design revision in nullable columns
   and never edited in place. They appear in design responses, the revision history and
   `GET /experiments/{id}/export`.
4. **Optional and additive.** Omitted stays null. Legacy designs are never relabelled, and
   existing digests and requests are unchanged.
5. **A change is a plan change.** A revision that changes either value is recorded like any
   other plan change, with its `change_reason`. A revision records exactly what it states:
   nothing is carried forward from the previous revision. Dropping a value the current
   revision recorded (the metric, or either variant's environment digest) is a plan change
   too, so it is refused without a `change_reason`; adding a value, or restating an unchanged
   one, needs none.
6. **Recorded, not verified.** The platform does not check that a digest matches an actual
   environment, or that the metric appears in the recorded measurements. A later check could
   compare `metric` with recorded measurement names; that is not done.
7. **Schema 83.** The migration is additive: it adds two nullable columns and changes no
   existing row. Like every schema bump it clears the backup-health success markers so the
   next backup is taken on the new schema.

## Consequences

- A design can state which environment and which headline metric it stands on, so a reader
  of the export can find them without parsing free text.
- A digest or metric name is a researcher's claim. It is not evidence that the environment
  was used or that the metric was measured, and `scientific_validity` is unaffected.
- Designs made before this change simply show null for both.

## Not done

- The design and variant tables are not immutable at the database level: unlike the
  assessment table they have no triggers, so "never edited in place" is a code convention,
  as it already is for the existing columns.

- No desktop form fields for either value.
- No enforcement that a design has them.
- No environment capture by the platform.
- No comparison tooling beyond what exists.

## Tests

`tests/test_design_environment.py` (create, revise, reads, history, export, change_reason,
validation) and `tests/test_design_environment_migration.py` (schema 83 migration and
legacy rows staying null).
