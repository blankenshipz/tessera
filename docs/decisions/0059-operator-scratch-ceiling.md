# 0059 — Operator scratch ceiling and per-stage scratch requests

## Status

Implemented. No schema change. Amends the operator container limits of
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) and the provider file of
[ADR 0040](0040-multi-stage-multi-vendor-campaigns.md).

## Context

A local CPU stage runs in a container whose scratch directory `/work` is a tmpfs. The
operator's `scratch_bytes` set its size for every container, and both it and the largest
value the operator could configure were capped at 64 MiB. With large artifacts live, a
stage that fetches a model of about a gigabyte filled the scratch and failed with `ENOSPC`
in the middle of the run, with no way for the operator or the spec to make room.

The tmpfs is memory-backed: its pages are charged to the container's memory limit. So a
bigger scratch is only real if the container's memory can hold it.

## Decision

### 1. The operator's ceiling

The operator owns two numbers, and a plan owns neither:

- `scratch_bytes`: the scratch every container gets by default. Unchanged: 16 MiB unless
  set.
- `max_scratch_bytes` (poller config, `--max-scratch-bytes`): the largest scratch a plan may
  *request*. The default ceiling is the historical 64 MiB, so a fresh installation changes
  nothing. The allowed maximum rises from 64 MiB to 64 GiB, and `memory_bytes` may rise to
  64 GiB too.

Settings above the historical bounds (memory over 1 GiB, a ceiling over 64 MiB) are checked
against the host before the poller runs anything: memory may claim at most 75% of host
memory and the ceiling at most 50%, read from `/proc/meminfo`. Where host memory cannot be
read the check is skipped, and default settings are never checked. A default scratch above
the historical ceiling raises the ceiling to cover it.

### 2. A stage may request more, within the ceiling

A stage spec (v1 and v2) takes an optional `scratch_bytes` (1 MiB to 64 GiB) for
`docker-local-cpu-v1` and worker-grant providers. It becomes `max_scratch_bytes` in the
approved plan, so it is part of the digest a person approves; absent, the plan and every
existing digest are unchanged. The worker grants it only inside its own ceiling: the
container gets exactly the requested scratch, and a request the ceiling or the memory
limit cannot back is refused when the operation is built, before any container exists
(never as `ENOSPC` or an out-of-memory kill mid-run). A scratch above 64 MiB must also fit
the memory limit with 64 MiB to spare, in the operator's setting and in the container's
operation.

### 3. Admission fails clearly

Control cannot read a worker's config, so the operator also declares the ceiling in the
provider file: `max_scratch_bytes` on `local_docker` and each `self_hosted` entry (default
64 MiB, the same range). Submit and approval refuse a stage whose `scratch_bytes` is above
its provider's declared ceiling with 409 and `scratch_exceeds_operator_ceiling`, naming the
request, the ceiling and the settings to change, and refuse a request to a provider that
does not grant scratch (Salad and plugins do not). The declaration is the operator's to keep
equal to the poller's; if they disagree the worker's own check still refuses the run at
admission with its reason.

### 4. The receipt

The worker's run receipt records `scratch_bytes`, the scratch the container ran with, and
each attempt entry of the campaign receipt carries `scratch: {requested_bytes,
granted_bytes}`: what the spec asked and what the worker reports it granted (`null` when
nothing was asked or the receipt does not record it; never inferred).

## Consequences

- A stage that needs a large scratch can state it, a person approves it with the digest, and
  the operator's ceiling bounds it.
- Memory-backed scratch costs host memory while it is used; the host check and the memory
  coupling are what keep a large setting from starving the host. Disk-backed scratch for
  larger sizes is future work.
- The two declarations of the ceiling (provider file, poller config) can drift; the worker's
  check is authoritative and its refusal is clear.

## Tests

`tests/test_scratch_ceiling.py`: conservative defaults, a raised ceiling and a request within
it, the memory coupling, host-capacity bounds and their skips, validation of the settings,
the container's tmpfs size, the runtime's grant and refusal, the poller config and file, the
spec shape and digest, admission refusal with its reason, the provider file ceiling, a
provider that does not grant scratch, a Salad stage refused, and both receipt records.
