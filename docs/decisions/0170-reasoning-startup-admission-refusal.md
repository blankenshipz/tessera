# 0170 — The reasoning supervisor refuses a bad recorded admission at start-up

## Status

Proposed. Implements point 6 of [ADR 0163](0163-reasoning-admission-delegation-record.md). No schema
change and no migration.

## Context

ADR 0163 added `startup_refusal(marker)` but nothing called it: the unit only checked that the marker
file exists, so a damaged or expired recorded marker still started the service.

## Decision

1. **The unit references the marker as a credential.** `tessera-reasoning.service` and the installer
   template add `LoadCredential=admission:<admitted path>`. The marker holds a non-secret reference, so
   nothing secret enters env, argv or logs. `ConditionPathExists` on the marker stays, so the credential
   source exists whenever the unit starts.
2. **Start-up refuses offline.** `service_startup reasoning` reads `$CREDENTIALS_DIRECTORY/admission`
   (read-only), calls `startup_refusal`, and on a code exits 78 (`EX_CONFIG`) with a one-line stderr
   message holding only the code. A missing file (an older unit) and a bare marker pass. A marker that
   cannot be read safely refuses with `admission_marker_unreadable`. The content is never printed.
3. **78 never restarts.** `RestartPreventExitStatus` already lists 78, so a refusal does not loop. 78 is
   a configuration refusal, distinct from a completed one-shot run (exit 0) and from 77.
4. **Readiness forwards the marker path.** The in-unit readiness check passes `--admitted-path`
   (the explicit argument, else the unit's `ConditionPathExists`) to `first_call_readiness`.

## What it does not do

It does not see revocation (that needs the record; readiness reports it) and does not make a bare
marker fail.
