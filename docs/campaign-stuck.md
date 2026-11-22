# Stuck campaign detector

Contract: [ADR 0091](decisions/0091-campaign-stuck-detector.md). The control plane notices
campaigns that need attention and records why. It never acts on them: no cancel, resume, resolve
or retry. A person, or what existing policy and the standing agent loop already allow, decides.

## What is flagged

| code | meaning | suggested next action |
|---|---|---|
| `campaign_blocked` | the campaign is `blocked` | `inspect_stop_reason_then_resume_or_cancel` |
| `delegation_lapsed` | paused because its delegation lapsed | `renew_delegation_by_reapproval` |
| `attempt_outcome_unknown` | an attempt (or its stage) has an unknown outcome | `resolve_unknown_attempt` |
| `stale_execution_lease` | an execution lease past its expiry plus the grace, for a live attempt | `reconcile_or_cancel_the_grant` |
| `runner_lease_stale` | no live campaign runner while campaigns are active | `check_the_campaign_runner_service` |
| `no_progress` | nothing in the campaign changed for longer than `unchanged_seconds` | `check_provider_capacity_or_cancel` |
| `heavy_stage_on_local` | a live attempt (reserved, granted, admitted or running) has a heavy-stage warning event for the same attempt (ADR 0107) | `check_salad_credential_or_profile` |
| `heavy_stage_refused_local` | a stage has waited on a `heavy_local_refused` refusal longer than `lease_grace_seconds` (ADR 0107); clears when placed | `restore_remote_capacity_or_allow_local` |

Evidence for the two heavy-stage codes: `heavy_stage_on_local` carries `attempt_id`, `attempt`,
`attempt_status`, `provider_id`, `reasons`, `signals` and `age_seconds`; `heavy_stage_refused_local`
carries `stage_key`, `attempt`, `reasons`, `waiting_seconds` and `grace_seconds` (the project's
`lease_grace_seconds`, default 300). A schema migration widens the findings code check to accept
both codes.

A finding carries a stable id, the code, the suggested action, when it was first and last seen,
how many times it has occurred, and evidence (ids, statuses, ages in seconds). The evidence is
frozen when an occurrence is detected, so its ages are as of detection; `last_seen_at` shows that
a scan still sees it, and a recurrence is a new occurrence with new evidence. It never carries
outputs, prompts or credentials.

## States

`open` (a condition holds), `acknowledged` (someone has seen it; it is still listed), `resolved`
(the condition no longer holds). If a resolved condition returns, the same finding reopens and its
occurrence count goes up.

## Receipts

Each change is a recorded event with a `receipt_sha256` of the canonical evidence:
`campaign.stuck_detected`, `campaign.stuck_acknowledged`, `campaign.stuck_resolved`,
`campaign.stuck_settings_set`.

## Settings

Per project, set by a human only: `unchanged_seconds` (default 3600) and `lease_grace_seconds`
(default 300), each from 60 seconds to 30 days. They are heuristics: tune them to how long your
stages and reviews really take.

## Running it

The service scans periodically: `tessera serve --campaign-stuck-scan-seconds N` (default 300;
`0` turns it off). A scan can also be requested at any time, by a human or an agent with
`campaign:read`: that is deliberate, because a scan records findings and events and nothing else.
A scan reads the database only; it does not contact providers or workers. If a project's scan
raises, a `campaign.stuck_scan_failed` event (project id, exception class, consecutive failures) is
recorded at the first failure and every tenth after.

Routes, under `/projects/{project_id}/campaign-stuck`:

| route | who | what |
|---|---|---|
| `POST scan` | a human, or an agent with `campaign:read` | scan now; idempotent |
| `GET` | a human, or an agent with `campaign:read` | list findings (filter by state) |
| `POST {finding_id}/acknowledge` | a human, or an agent with `campaign:cancel` | mark seen, with a short note |
| `PUT settings` | a human | set the two thresholds |

`tessera_mcp` tools: `tessera_list_stuck_campaigns` (read-only) and `tessera_acknowledge_stuck`,
registered only when the credential's live scopes allow them.

## The agent loop

`tessera_agent.stuck_work_source.StuckWorkSource` offers a project's open findings to the standing
agent loop as candidate work, with the id `<finding id>:<occurrences>`. It only reads. Whether the
loop does anything is decided by its own policy (tier, identity, budget, kill switch); acknowledged
findings are not offered. It is available and tested but not yet wired into a running loop (the
standing loop has no entry point yet).

## Limits

Nothing has been run against a live deployment. A scan cannot see progress inside a provider, and a
worker that cannot reach the control plane looks like a dead one. A finding says what to look at,
not why.
