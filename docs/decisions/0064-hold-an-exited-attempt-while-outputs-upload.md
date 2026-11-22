# ADR 0064: hold an exited attempt while its outputs are still uploading

Status: accepted. Amends ADR 0053 (resolving attempts on a confirmed exit) and builds on
ADR 0050 (large-artifact custody, including resumable parts) and ADR 0048 (grant lifetime).

## Context

A worker reports `exited` before it collects, hashes, uploads and posts the run receipt and
output manifest. For a multi-gigabyte output (a model weights fetch, ADR 0050) that takes
minutes after the exit, with the worker's lease lapsed and, on a slow link, the grant expiring.
ADR 0053 held an `admitted` or `running` attempt only until its grant expired, not at all when
it was `unknown`, and measured its 120 s grace from the exit alone. The runner's own observation
also marked such an attempt `unknown` (`missing_receipt`, `missing_manifest`) at grant expiry.
So a stage whose upload was still running was failed as `exit_confirmed_unresolved`, its grant
released and its retry started, and the late bytes were refused: the weights campaign failed
deterministically.

## Decision

1. **A confirmed exit does not close an attempt while its outputs are arriving**, for every
   status and whether or not the grant has expired. Outputs are arriving when
   - an open (`uploading`), unexpired large artifact upload of one of the grant's leases has gained
     bytes within `UPLOAD_STALL_SECONDS` (10 minutes), or
   - an upload, small or large, or an artifact finished within `GRACE_SECONDS` (120 s), because
     the manifest and the run receipt are posted right after the last byte.
2. **Progress is observed, not stamped.** The chunk store keeps no per-write time, so the runner
   remembers when it last saw each open upload's held bytes change (`UploadProgress`). This needs
   no schema change. A resumed upload (ADR 0050, item 5b) adds bytes and so counts as progress. A
   runner that has just started has seen nothing, so an upload gets a fresh stall window: a
   restart can delay a resolution by one window and never closes an upload early.
3. **The runner's observation waits too.** Where it would mark an exited attempt `unknown`
   for a missing receipt or manifest it waits instead while outputs are arriving.
4. **The hold has a ceiling.** The grant's own lifetime always counts, as before this ADR. Beyond
   it the hold lasts at most `MAX_EXTENDED_HOLD_SECONDS` (2 hours) from when control recorded the
   exit and never past the campaign's approval window, so a trickle of bytes cannot hold a worker
   slot and a grant for the upload's own 24 hours. The ceiling is a constant, not an operator
   setting.
5. **After the uploads finish, stall or expire, or the ceiling passes, the attempt is settled as
   before**: the receipt or its absence decides, ADR 0053's outcome classes apply, and the cost
   rule is unchanged. The evidence records exactly what was open: `open_uploads`
   (`unexpired` and `expired` counts) and `hold_ended_by` (`stall` or `ceiling`, absent when
   nothing unexpired was open). When unexpired uploads were open, every outcome's failure detail
   names them and why the hold ended; the campaign receipt shows it under `exit_resolved`.

## Consequences

- A stuck upload that keeps gaining bytes holds a worker slot at most to the ceiling (2 hours past
  the exit, within the approval window); one that stops is released after 10 minutes. These are
  constants, not operator settings.
- Progress is the number of bytes control holds for the upload, counting a chunk that is only partly
  stored (a worker sending a chunk in parts, ADR 0050). Each part moves it, a worker restart
  resuming mid-chunk keeps it moving, and a chunk dropped for a bad digest changes it too. A
  single whole-chunk request is bounded by the worker's request timeout, far below the 10-minute
  window, so a slow link is never mistaken for a stall.
- Small single-BLOB uploads (at most 16 MiB) are held by the existing live-lease check and the
  completion grace, not by progress watching.
- Compatible with resumable parts: a restarted worker continues from the bytes control holds.

## Tests

`tests/test_exited_resolve_uploads.py`: an expired grant does not close an attempt whose upload is
gaining bytes and does after a stall, an `unknown` attempt is held, a restarted runner gives a fresh
window, the grace after the last byte, an expired upload stops holding, the runner neither marks
nor resolves while uploading and settles after a stall, the ceiling and the approval window, a chunk
growing part by part across a restart, exact receipt evidence, and the progress watcher
(forgetting and eviction).
