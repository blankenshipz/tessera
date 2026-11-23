# ADR 0125: The stage watcher is on by default, still observe-and-report only

Status: accepted. Amends [ADR 0087](0087-execution-plane-agents.md) and
[ADR 0111](0111-stage-watcher-decision-requests-and-remote-wiring.md), which shipped the watcher
off by default and as an operator drop-in; the limited amendment of
[ADR 0033](0033-freeze-over-built-planes.md) in ADR 0111 (two Salad hooks) is widened only from
"off by default" to "on by default" for the same two hooks.

## Context

The watcher existed but had to be asked for: `--stage-watcher` plus a kill-switch path on the
worker command line, or an `stage_watcher` block in the Salad executor's config. The poller's
JSON config file had no setting for it at all, so a worker started from a config file could not
watch, and a config that tried to name one was refused. A live Salad GPU smoke stage was watched
with a hand-written background shell loop because the platform watcher was not on. A watcher that
the operator must remember to start is not the execution-plane agent the design describes.

## Decision

1. **On by default, for every provider.** A worker or Salad executor with no stage-watcher setting
   starts the watcher: the local Docker worker path (garage and local-docker) and the Salad route
   alike. It reports state changes, stalls (a run silent past its bound) and resource-limit
   signals, as the existing receipted watch reports, joined to the campaign receipt.
2. **Same limits as before, nothing widened.** It observes and reports only: no action, no spend,
   no provider or credential access of its own. Polling is bounded by the existing per-tick budget
   and report caps. It opens no connection of its own: reports go through the worker's existing
   control channel (or the executor's in-process store, which accepts only the watch-reports
   route). Its failures never reach the stage: a failed tick or report is counted and the run
   continues.
3. **The kill switch keeps its meaning.** A marker file at a default path inside the worker's (or
   executor's) own private state directory is the operator's kill switch: while it exists the
   watcher files no decision requests and makes no model calls; observing and reporting continue,
   as ADR 0087 defined. The platform never creates the marker. If the default directory is not
   usable (missing, a symlink, not owned by the worker, or open to others) the worker still runs,
   with no watcher, and says so once on stderr (or in a bounded log line): a watcher problem
   never refuses a stage.
4. **Opt-out is explicit.** `--no-stage-watcher`, or `"stage_watcher": {"enabled": false}` in the
   poller or executor config. Explicit `--stage-watcher` and `--stage-watcher-kill-switch` values
   still win.
5. **The poller's JSON config takes a closed `stage_watcher` block** with exactly the watcher's
   own fields, strictly validated; an unknown or relative setting is refused with the same
   path-free refusal as any bad config.
6. **Readable.** The campaign receipt already carries a `stage_watch` section. A read route
   `GET /campaigns/{id}/stage-watch` and the MCP tool `tessera_campaign_stage_watch`
   (`campaign:logs`) return the recorded reports, bounded, with no worker paths.

## Consequences

- Operators who had not opted in now get reports; the cost is bounded (one cheap read per tick
  and a few small posts).
- A worker that cannot write its private state or reach control still runs stages; it reports
  nothing, and the receipt has no `stage_watch` section, as before.
- Decision requests (ADR 0111) remain advisory records; nothing here lets the watcher retry,
  resize or re-route.
