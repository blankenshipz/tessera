# ADR 0091: a control-plane detector for stuck campaigns

Status: accepted. No change to any existing campaign, grant, lease or settlement rule. ADR 0047's
public-internet invariant is untouched: the detector reads database state only and contacts no
provider, worker or network destination. Builds on ADR 0038 (campaign runner), 0046 and 0053
(reconciling attempts), 0048 (grant lifetime and slot release), 0055 (steering tools) and 0085
(the standing agent loop).

## Context

Whether a campaign is stuck was decided by whoever drove the work: a person or a driver routine
read each campaign's status, noticed a `blocked` or paused one, noticed an attempt whose outcome
was unknown, noticed that nothing had changed for a long time, and nudged the right person. That
check lived outside the platform, so it left no receipt, was lost when the driver was, and could
not be read by the platform's own agent loop.

## Decision

1. **The detector takes no action.** It never cancels, resumes, resolves, retries, reconciles or
   edits a campaign, stage, attempt, grant, lease or budget. It records *findings* and *receipts*.
   Any action stays with a person, or with what existing policy and the agent loop's own bounded
   rules already allow. The driver's stuckness check moves into the platform; the driver's
   authority does not.
2. **Findings.** A scan reads the control database and produces one finding per condition:

   | code | condition | suggested next action |
   |---|---|---|
   | `campaign_blocked` | campaign status `blocked` | `inspect_stop_reason_then_resume_or_cancel` |
   | `delegation_lapsed` | status `paused_delegation_lapsed` | `renew_delegation_by_reapproval` |
   | `attempt_outcome_unknown` | an attempt `unknown`, or a stage `blocked_unknown` | `resolve_unknown_attempt` |
   | `stale_execution_lease` | an execution lease of an unreconciled grant whose expiry plus the grace has passed, for a live campaign attempt | `reconcile_or_cancel_the_grant` |
   | `runner_lease_stale` | the runner lease expired beyond the grace while a campaign is submitted, running, stopping or cancel-requested | `check_the_campaign_runner_service` |
   | `no_progress` | a non-terminal campaign, not already blocked or paused, whose own row and every stage and attempt row are unchanged for longer than `unchanged_seconds` | `check_provider_capacity_or_cancel` |

   A finding has a stable id (a digest of the campaign, the code and the subject: attempt, lease
   or stage), its code and suggested action, `first_seen_at` and `last_seen_at`, and evidence:
   ids, statuses and ages in seconds only, never output content, prompts or credentials.
3. **Receipts.** Every change is an event carrying a `receipt_sha256` (sha256 of the canonical
   evidence): `campaign.stuck_detected`, `campaign.stuck_acknowledged`, `campaign.stuck_resolved`
   and `campaign.stuck_settings_set`. The evidence of an occurrence is frozen at detection: the
   row, the detection event and the acknowledged and resolved events all carry the same evidence and
   the same `receipt_sha256`, so any of them can be checked against the others. Ages in the
   evidence are as of detection; `last_seen_at` shows that a scan still sees the condition. A
   reappearance after resolution is a new occurrence with new evidence and a new digest. A scan
   that raises for a project is recorded as `campaign.stuck_scan_failed` (project id, exception
   class only, consecutive failures) at the first failure and every tenth after, so a failing
   project is not mistaken for a healthy one with no findings.
4. **States.** `open`, `acknowledged`, `resolved`. A scan opens a finding the first time a
   condition holds and resolves it when it no longer holds. A condition that returns after being
   resolved reopens the same finding and increments its `occurrences`. Acknowledging says a
   person or an authorised agent has seen it; it changes nothing else and does not hide the
   finding from a list.
5. **Settings.** Per project: `unchanged_seconds` (default 3600) and `lease_grace_seconds`
   (default 300), bounded (60 s to 30 days). Only a human sets them.
6. **When it runs.** The service scans every project periodically
   (`serve --campaign-stuck-scan-seconds`, default 300; 0 disables) and on demand. A scan is
   idempotent: scanning twice with no change records nothing new. Requesting a scan needs only
   `campaign:read`, deliberately: a scan records findings and events and nothing else, so its
   effect is bounded and a narrower scope would only make the detector harder to read.
7. **Surfaces.** Routes under `/projects/{project_id}/campaign-stuck`: `POST scan`, `GET` (list),
   `POST {finding_id}/acknowledge`, `PUT settings`. List and scan need a human or an agent with
   `campaign:read`; acknowledge needs a human or an agent with `campaign:cancel`; settings are
   human only. `tessera_mcp` adds `tessera_list_stuck_campaigns` and `tessera_acknowledge_stuck`.
8. **A work source for the agent loop.** `tessera_agent.stuck_work_source.StuckWorkSource` offers
   the open findings of a project as candidate work for the standing loop (ADR 0085), with the id
   `<finding id>:<occurrences>` so a reappearance is new work. It only reads: the loop's own
   policy decides whether to act, under the same tier, identity, budget and kill-switch rules as
   any other item, and acknowledged findings are not offered. It is available and tested (alone and
   against the real routes) but not yet wired into a running loop: the standing loop has no entry
   point yet, and wiring is part of that work.

## Consequences

- A stuck campaign is visible to people, MCP clients and the agent loop from one place, with a
  reason code, a suggested next step and a receipt, whoever (or whatever) was driving.
- Nothing becomes automatic: a finding grants no authority and spends nothing.
- A scan writes events and finding rows, so a long-stuck campaign produces one detection and one
  resolution per occurrence, not one per scan.

## Limits

- Nothing was run against a live deployment; behaviour is tested with synthetic state.
- The thresholds are heuristics. An unchanged campaign may be waiting legitimately (a long stage,
  a person's review), and a slow stage may be healthy.
- A scan reads database state only. Progress inside a provider (a container computing, a remote
  job running) is invisible until it is reported to the control plane.
- Stale-lease detection depends on workers renewing leases; a worker that cannot reach the
  control plane looks the same as a dead one.
- A finding says what to look at, not why: the stop reason and the provider are read separately.

## Tests

`tests/test_campaign_stuck.py` (each code, thresholds, reopen on reappearance, resolve, receipts,
no side effects on campaigns), `tests/test_campaign_stuck_api.py` (routes, scopes, tenant
isolation, human-only settings, idempotency), `tests/test_mcp_campaign_stuck.py` (the two tools),
`tests/test_stuck_work_source.py` and `tests/test_stuck_loop_integration.py` (the loop's work source, alone and against the real routes). Focused tests only; no live calls.

Amended by [ADR 0107](0107-heavy-stage-placement-guard.md), which adds the codes `heavy_stage_on_local` and `heavy_stage_refused_local` (a schema migration widens the findings code check).
