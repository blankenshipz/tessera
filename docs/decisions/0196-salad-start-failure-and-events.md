# 0196 — A start-failure loop before custody stops the group; one closed events read

## Status

Accepted. Amends ADR 0193 point 2. No schema change and no migration (the guardian's private
database gains a table and event kinds; it creates them on open).

## Context

ADR 0193 point 2 keeps an assigned machine as the allocation latch in any state, so a slow image
pull is never killed by the allocation deadline. It also says a candidate node that is assigned
and then loops is bounded only by the route deadline. In practice a container that dies at once
(for example `Instance Exited:127`) is rarely seen `running` at a 15 second poll. Each failed
start moves to another node and Salad's backoff grows toward 20 minutes, so the group held its
frame until the 3600 second route deadline while the exit code was visible only in the provider's
portal, hours later. Salad's own `pending` (image pull) and `deploying` (started, no instance
running) group states were reported as `unknown`, the same as a failed read.

## Decision

1. **Start-failure detection (amends ADR 0193 point 2).** While the adapter says that no custody
   has begun, the guardian persists the distinct machines it saw assigned to the operation (a
   truncated digest of each id, never the id). At three distinct machines it stops the group
   through the ordinary stop path with the new closed `stop_cause` `start_failure_suspected`, and
   its status carries `start_failure.machines`. Three, because one or two failing nodes are
   ordinary node trouble that Salad itself handles by moving the start and backing off, while
   three distinct nodes failing before the first call-in points at the image or its command. One
   machine that stays `downloading` or `creating` for any length of time is a slow pull, counts
   once and never stops early. An adapter that cannot say (no `custody_started`, or it raises)
   never counts. The point 2 rule that an assigned machine latches the allocation deadline is
   unchanged. `start_failure_machines` is a guardian setting (2 to 10, 0 turns it off).
   The count uses a narrower signal than `custody_started`: custody work proper (the workload
   called in, an input was served, an output was requested or written). A container caught
   `running` on one poll is therefore still counted. A container that restarts on the one machine
   stays a single machine and is held to the route deadline; the count cannot see it.
2. **Permanent only with a recorded exit; no class fallback.** Salad also moves a healthy
   instance between nodes while it allocates, pulls or creates, so three machines alone do not
   prove a failed start. Control records `failed_permanent` with failure class
   `start_failure_suspected` only when the one events read recorded an `instance_exited`. With no
   such record (the read unfinished, empty, rate limited, unavailable or showing no exit) the
   attempt is `failed_transient` as `provider_reallocated_before_custody`, so a fresh attempt may
   retry within `retry.max_reallocation_retries`. The permanent class is not an `infra_` class, so infrastructure
   retries and rerouting do not apply, and it carries neither a `prestart_failure` nor an
   `allocation_timeout`, the only evidence the class fallback reads, so the stage is not moved
   to the next class profile. The same image digest would fail the same way anywhere.
3. **One events read.** A new transport kind `read_events` makes one `query_log_entries` call
   (organization scoped, filtered to the project, the group and `instance_controller`, page size
   50, a two hour window). The service runs it at most once per operation, on a stop whose cause
   is not `output_retained`, after the stop was dispatched and off the loop thread, so the stop
   never waits on it. The result is saved on a later loop pass, so it can miss a receipt written
   at once after the stop; that only makes the failure transient (point 2), never permanent. The
   attempt is claimed durably before the request; a query that returns zero entries records
   `empty` (a wrong label name must not look like a real empty log, so it is never `read`), a
   failure records `unavailable` and a 429 records `log_quota_exhausted` (Salad's daily log query limit lasts
   until 00:00 UTC), neither is retried. Only closed tokens are stored on the receipt: event
   names from a fixed list, exit codes (0 to 255) and a count of distinct machines. No body text,
   instance or machine id, hostname or timestamp is kept. Container stdout logs are not read.
4. **Pending and deploying are observations.** The readback of a group Salad reports as
   `pending` or `deploying` carries that state (when it names the group); a failed read, a
   reallocation or any other status stays `unknown`. The live check reads them as pre-start and
   keeps its slow observe cadence. They never grant authority, never confirm a stop and never
   start accrual.

## Amendment: the container exit watch and one container-log tail

The bounded exit watch (`salad_exit_watch`) reads the same system events while a frame runs, so a
crash loop is stopped before Salad replaces the machine. Its reads back off, share a daily budget
and stop for the UTC day after a rate-limited answer, so the one post-stop read of point 3 keeps
its quota; that read is not sent again once the watch recorded the events. Exit codes are kept in
first-seen order, and the earliest non-zero exit decides whether a process exit was the workload's
own (`[1, 137]`) or a kill that came first (`[137, 1]`).

Point 3's "Container stdout logs are not read" is narrowed: after a recorded non-zero process exit,
and only then, the service makes one durably claimed `read_logs` query of the exact group's
container logs. The transport reduces it at once to a bounded, sanitized tail (last 20 lines, at
most 1 KiB, printable ASCII, secret-shaped and identifying values redacted), marked
`untrusted: true` with its digest; control re-checks that closed shape wherever it is recorded.
The tail appears only in the guardian's final status, the recorded guardian final and the receipt's
`guardian_container_exit.stderr_tail`, as an operator explanation of a failure, never as evidence
or in any authority, admission or settlement decision. A failed, rate-limited or unread query
records a closed reason instead.

A sealed run never has its container log read or stored. Control tells the guardian at reserve
whether the route runs a final holdout stage or reads a sealed partition
([ADR 0068](0068-sealed-holdout-evaluation-stages.md)), failing closed to sealed when the
campaign spec no longer parses or names the stage. The guardian keeps that flag on the
reservation (a later reserve may set it, never clear it); after a non-zero exit of a sealed run it
claims the one log read as usual but sends no query and records the closed reason
`data_class_sealed`, so the receipt's `stderr_tail` is `{"outcome": "unavailable", "reason":
"data_class_sealed"}`. The exit codes and closed event tokens are still recorded. A reserve that
omits the flag (an older trusted server) is treated as not sealed.

A non-zero exit no longer waits for three machines. The three-machine rule above still governs a
start-failure loop with no failing exit code. When the earliest non-zero exit on a single machine
is the workload's own (not 137 or 143), while its frame is armed and seen running with no retained
output, the group is stopped at once and the attempt fails permanently
(`salad_container_exited_nonzero`); see the amendment to
[ADR 0193](0193-salad-allocation-deadline-machine-assignment.md). When the earliest non-zero exit
is 137 or 143 (a kill or platform stop came first), the behaviour above is kept.
