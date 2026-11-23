# ADR 0124: MCP tools for the operator loop (render, wave receipts, gate decision)

Status: accepted. Addendum to ADR 0055 (MCP steering tools), ADR 0065 (operator-enabled MCP
approval tool) and ADR 0045 (gate decisions).

## Context

An agent driving the wave-and-gate operator loop (submit a wave of campaigns built from one
spec template, read each finished wave's receipts, then a person records a gate go) had to leave
MCP three times: to render a spec from a template with new values, to read the receipts and gate
decisions of a chain of campaigns one call at a time, and to record the gate go. The first two
need no new authority; the third is a person's act.

## Decision

1. `tessera_render_campaign_spec` and `tessera_submit_rendered_campaign` (both `campaign:submit`)
   take a template (a campaign spec as a JSON object or JSON text) and a map of values.
   Placeholders are `${NAME}` (upper-case letters, digits, underscore). Substitution is
   structural: values are strings placed into string leaves only, never into keys, so a value
   cannot add a field or change a type. Every placeholder needs a value and every value must be
   used; otherwise the call is refused before any request is sent. The rendered spec is parsed
   by the same model `tessera_submit_campaign` uses and is then validated (render) or submitted
   (submit) by the control plane, which applies every check it applies to a hand-written spec.
   Submission grants nothing: a person still approves the exact spec digest returned.
2. `tessera_campaign_wave_receipts` (`campaign:read`) takes 1-20 distinct campaign ids and
   returns, for each, the status, the terminal receipt and the recorded gate decisions with the
   effective one. It is read-only. An id that cannot be read reports its own error and the
   others still return. A campaign that has not finished has a null receipt and a reason;
   nothing is filled in, estimated or judged. A campaign cancelled before any approval never
   gets a receipt, so its entry has a null receipt with the reason `no_receipt_recorded` (a 404
   on the receipt read of a never-approved cancelled campaign; any other receipt error is
   reported as an error), and gate decisions are read independently of the receipt, so neither
   failure hides the other. Gate decisions are requested for every campaign that could be read,
   finished or not, with no finished check in the tool: an unfinished campaign's entry still
   carries `gate_decisions` exactly as the control API returns it (the control API records
   decisions only for finished campaigns, so none exist yet), and a failed read is reported in
   `gate_decisions_error` without hiding the rest of the entry.
3. `tessera_record_gate_decision` is registered only on the operator-enabled approve-only server
   (`--approval-tool`), beside `tessera_approve_campaign`. It is not enrollable, is not in
   `CAMPAIGN_TOOL_SCOPES` and no scope grants it. It is a thin client of
   `POST /campaigns/{id}/gate-decision` and needs a person's own credential. The control API
   decides: it refuses a credential that is not a person's and a campaign that has not finished,
   and a later decision supersedes an earlier one without deleting it. ADR 0045 and the go-class
   rules are unchanged.
4. Not done: no agent path approves a spec or records a go or no-go, and nothing here creates a
   standing approval or an unlock condition. Rendering and wave receipts add no write that an
   agent could not already make through submit and read.

## Consequences

- An agent can run the loop from template to receipts without leaving MCP; the approval and the
  gate go remain a person's, on a separate server with that person's credential.
- Whoever can run the approve-only server with a person's credential can now record a gate
  decision as well as approve, exactly as with the CLI, so protecting that credential file is
  the control.
- Rendering is deliberately strict: a spec that needs a value in a key or a structural change
  is written out in full instead.
- Wave receipts cost one read of the campaign, its receipt read when it has finished, and a
  gate-decisions read for every id, finished or not; the 20-id bound limits that.
- ADR 0055's statement that no tool approves or records a go holds for every agent credential
  and every default server.
