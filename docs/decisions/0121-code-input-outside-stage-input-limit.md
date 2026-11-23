# ADR 0121: a stage's code input sits outside the per-stage input limit

Status: proposed. Amends ADR 0040 (v2 stage limits) and ADR 0095 (code as a content-addressed
input). No schema change.

## Context

A v2 stage may hold at most 8 inputs. ADR 0095 made a stage's code one of those inputs: a pinned
`snapshot` input that the stage names as `code_input`. A campaign whose preparation stage already
reads 8 data inputs (for example the several datasets an outcome-style evaluation prepares) could
therefore not adopt code-as-input: adding the code input made 9 inputs and the spec was refused.
The limit exists to bound the data a stage reads and the work validation and composition do; the
code input is a single small bundle that is already gated by its own checks (pinned snapshot with
files, matching code approval, provider trust, recorded on the receipt).

## Decision

1. A v2 stage holds at most 8 **data** inputs (`MAX_STAGE_DATA_INPUTS`). When it names
   `code_input`, and that name is one of its pinned `snapshot` inputs, that one input is not a data
   input, so the stage may hold 9 inputs in all. A stage without `code_input` stays at 8.
2. It stays fail closed: a 9th input is refused unless the stage names it, or another of its inputs,
   as a snapshot `code_input`; a `code_input` that names a missing, `stage`, or `artifact` input does
   not lift the limit; 10 inputs are always refused, so there is never more than one code input.
3. The spec-wide edge cap (512 for v2) counts data inputs only, consistently with the per-stage
   rule; v1 specs have no `code_input` and are unchanged.
4. A composed input snapshot (ADR 0038, the runner's `compose_snapshot` step and
   `POST /projects/{id}/composed-snapshots`) takes up to 9 sources, so the runner can compose a
   stage with 8 data inputs and its code input into the one snapshot it mounts or stages.
5. No serialized field is added, renamed or reordered, so every spec digest valid before this
   decision is byte-identical.

## Consequences

Downstream paths were checked for an 8-input assumption. Only two existed and both changed: the
stage model's own limit and composition's source limit (plus the edge cap of point 3). The others
take any number of inputs and needed no change: normalization (`NormalStage` keeps every input and
the `code_input` name), structural validation (`code_input` naming rules), the Salad stage adapter
(sizes and digests every source, no count limit), Salad and worker delivery (one composed snapshot
per stage, so one input ticket and one `/inputs/<name>/` mount per input), the receipt's `code`
block (looks the code input up by name), submit-time code checks, the stage input index, and the
MCP retry spec builder (keeps the input list and `code_input` as written). The public composition
API accepts one more source than before; ownership, digest and class checks per source are
unchanged.

## Tests

`tests/test_code_input_outside_input_limit.py`: 8 data inputs plus the code input are accepted
through submit; 8 inputs without `code_input` stay valid; 9 without `code_input`, 9 whose
`code_input` names a missing, artifact or stage input, and 10 with a code input are refused; the
code input is not an edge; three fixed specs keep the digests computed before this change;
composition takes 9 sources and refuses 10; the Salad stage adapter composes a 9-input stage with
the expected digest and size.
