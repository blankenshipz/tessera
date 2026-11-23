# Enablement runbook: features a deploy ships but an operator must switch on

Run these after a deploy. Each section says what the feature does, the exact steps to turn it on,
and a check that proves it is in use (not merely deployed). Every path is a placeholder: use your
own state directory, config directory and control URL. Nothing here needs a secret typed on a
command line; credentials are owner-only files named by path.

Shell setup used below (set these once; do not print the header file):

```
CONTROL=http://127.0.0.1:8765          # your control URL
HEADER=/path/to/state/owner.header     # the owner header file (never paste its contents)
PROJECT=your-project-id
```

An authenticated read is `curl -s -H "$(cat "$HEADER")" "$CONTROL/..."`.

## 1. Stuck-campaign detector (ADR 0091)

It records a finding when a campaign has stopped making progress. It reads the database only and
never cancels, resumes or retries anything.

**On by default.** `tessera serve` starts the scanner on its own timer (every 300 seconds;
`--campaign-stuck-scan-seconds N` changes it, `0` turns it off). It runs even with
`--no-campaign-runner`. Make sure no unit passes `--campaign-stuck-scan-seconds 0`.

**Check it is in use:**

```
curl -s -X POST -H "$(cat "$HEADER")" -H "Idempotency-Key: scan-$(date +%s)" \
  "$CONTROL/projects/$PROJECT/campaign-stuck/scan"
curl -s -H "$(cat "$HEADER")" "$CONTROL/projects/$PROJECT/campaign-stuck?status=all&limit=5"
```

The first call scans now; the second lists findings (an empty list is correct when nothing is
stuck). A failing scanner records a `campaign.stuck_scan_failed` event. An agent can read the same
with the MCP tool `tessera_list_stuck_campaigns`; `tessera_heavy_local_audit` lists the heavy
stages running on a local provider now, with reasons.

## 2. Credential-expiry health

Reports how long each Salad management credential has left, and blocks a start whose credential is
expired, revoked or pinned to another version. It reads metadata only, never the secret.

**Turn it on:** start control with the credential directory (absolute, owner-only 0700):

```
tessera serve ... --salad-credential-directory /path/to/salad-credentials
```

or set `salad_credential_directory` in the providers file (not both). Restart control.

**Check it is in use:**

```
curl -s -H "$(cat "$HEADER")" "$CONTROL/credential-health"
```

Every `items[].state` should be `ok` or `warning`, and no `reason` should say "no credential
directory is configured" (that means `unknown`: the directory was not given). The same data is under
`credentials` in `GET /operator/health`, and the MCP tool is `tessera_credential_health`.
It covers Salad management credentials only; worker and executor credentials renew on their own
timer (`deploy/executor`).

## 3. Execution-plane stage watcher (ADR 0111)

Watches a running stage for silence and posts observation reports and decision requests. It only
observes and reports: it never stops, retries, resizes or approves.

**On by default.** The worker watches every local Docker stage with no flag, with the marker
`<workspace_root>/stage-watch.kill` (under the worker's private workspace root). The shipped
`deploy/systemd/tessera-development-poll.service` still passes
`--stage-watcher --stage-watcher-kill-switch /var/lib/tessera/stage-watch.kill`, and explicit values
win. For the Salad executor, set the `stage_watcher` block in its JSON config (`enabled`, an
absolute `kill_switch_path`).

The watcher is engaged (no requests) while the marker file exists; the worker never creates it.
`touch` the marker to engage it at once; remove the file to release it. A marker directory that is
not private leaves the worker running with no watcher and one stderr line.

**Opt out (explicit only):** `--no-stage-watcher` on the command, or
`"stage_watcher": {"enabled": false}` in the poller JSON config, then restart:

```
systemctl restart tessera-development-poll.service
```

**Check it is in use:**

```
journalctl -u tessera-development-poll.service | grep -c 'stage watcher:'
tessera campaign receipt --owner-header "$HEADER" --control-url "$CONTROL" --campaign-id CAMPAIGN_ID
```

The first counts stderr lines for an unusable or opted-out setup (`0` is healthy). After a stage has run, the campaign
receipt has a `stage_watch` section only if reports were recorded; no section means no reports
yet (for example a stage too short to go silent), not a fault.

## 4. Campaign-submit runner (ADR 0093)

Turns a record a person already approved into a campaign, on a schedule, so no session has to. It
runs no model and approves nothing.

**Gates, all required (each is a human or operator act):**

1. Control runs with `tessera serve --agent-submissions --agent-submission-kill-switch /etc/tessera/submit-runner.kill`.
2. The agent that submits is enrolled with the separate `campaign:submit_cleared` scope
   (`--allow-campaign-submit-cleared` on `scripts/enroll_local_mcp.py`; it is not part of `--allow-campaigns`).
3. A person records an approved submission (a spec digest they reviewed, with a cap and expiry).
4. The host is admitted: `touch /etc/tessera/submit-runner.admitted`.

**Turn it on.** The unit is wanted by `tessera.target`, so after step 4:

```
systemctl daemon-reload
systemctl enable tessera.target
systemctl start tessera-submit-runner.service
```

It sets `TESSERA_PROJECT_ID`, `TESSERA_AGENT_DESTINATION` and the agent token file by reference in
the unit; edit those placeholders once for your host. `touch /etc/tessera/submit-runner.kill`
pauses it at once (running campaigns are untouched).

**Check it is in use:**

```
systemctl status tessera-submit-runner.service
tail -n 5 /var/lib/tessera/submit-runner/journal.jsonl
curl -s -H "$(cat "$HEADER")" "$CONTROL/projects/$PROJECT/submission-approvals"
```

The journal shows `runner_started` and `tick` lines (and `kill_switch_engaged` while paused); the
approvals list shows records `pending` before and `consumed` after it submitted. A campaign it
created has a `submission` section on its receipt.

## 5. Heavy-stage placement guard (ADR 0107)

Warns, or (if you choose) waits, when a heavy stage would run on the local provider although a
Salad profile is available. It is on by default in `warn` mode and has no spec field.

**Make it apply:** register a Salad campaign profile for the project through the validated path
(`tessera providers add --file PROVIDERS_FILE --section salad-profile --entry-file ENTRY.json`, then
restart control), and make sure heavy stages list a remote candidate. To wait instead of falling
back, set `placement_preferences.heavy_local_fallback` to `"refuse"` in the providers file. Do not
do that for a stage that has no remote candidate: it would wait indefinitely.

**Check it is in use:** a heavy stage placed on `local` while a profile exists shows a
`heavy_guard` object on `GET $CONTROL/campaigns/CAMPAIGN_ID` (or `tessera_get_campaign`) and
`placement.heavy_guard` on its receipt; the event is `campaign.heavy_stage_local_fallback` and the
stuck finding codes are `heavy_stage_on_local` and `heavy_stage_refused_local`. No `heavy_guard`
means the stage was light, a remote candidate was chosen, or no Salad profile applies.

## 6. Code as a content-addressed input (ADR 0095)

A stage's experiment code travels as a pinned snapshot input instead of being baked into an image.

**Use it:** pin the code as a project snapshot, add it to the stage's inputs as a `snapshot`
input, and set `code_input: "<that input's name>"` on the stage (a v2 spec). Use a generic,
digest-pinned runtime image with no experiment code. If the spec omits `code_input`, nothing
changes and existing digests stay the same. Provider trust must admit the input's data class
(`private_derived`, or `private_code_approved` after a person approved the files).

**Check it is in use:**

```
tessera campaign receipt --owner-header "$HEADER" --control-url "$CONTROL" --campaign-id CAMPAIGN_ID
```

The stage has a `code` block (`input`, `snapshot_digest`, `code_digest`, `approved`, `image_digest`)
and each attempt a `code_transfer` block. `granted: true` means the grant event exists; it does
not by itself show the worker fetched and ran the code.

## Order after a deploy

1. Restart control with the flags in sections 2 and 4 (and 1 if you had disabled the scanner).
2. `systemctl daemon-reload`, restart the poller (section 3), then admit and start the runner (4).
3. Register the Salad profile if you have not (5).
4. Run each check above and keep the output as your record that the feature is in use.
