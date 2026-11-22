# Campaign compute providers

Contract: [ADR 0040](decisions/0040-multi-stage-multi-vendor-campaigns.md), which amends
[ADR 0038](decisions/0038-campaign-specs-and-platform-campaign-runner.md). A campaign
stage names a compute *provider* and a *runtime*; the runner calls only the provider
interface, so a vendor is a plugin rather than a branch in the runner. This page is the
operator side: which providers exist, how to configure them, and how the self-hosted
provider and the Salad provider are run. Which data classes each provider may receive is a
separate, deployment-owned setting: see [provider trust](provider-trust.md) and do not
duplicate it here.

## Providers

| Provider id | Kind | Runtime | Runs where | Billable |
| --- | --- | --- | --- | --- |
| `local-docker` | `worker_grant` | `docker-cpu-v1` | the control host's development worker | no (`local_zero`)¹ |
| `self-hosted` | `worker_grant` | `docker-cpu-v1` | a server you run, under its own worker principal | no (`local_zero`) |
| `salad` | `salad_route` | `salad-workload-v1` | Salad, through the operator-run executor | yes |

¹ A grant a human reconciled with a recorded cost settles `human_reconcile` instead.

`local-docker` is built in. `self-hosted` is another instance of the same code: the
unchanged development worker and grant poller on a server of your own, with no GPU. Salad
is a plugin over the Salad stage driver. Every provider declares the delegated steps
(`campaign_delegation.STEP_TABLE`) it may use, the output limits (Salad: 16 files, 16 MiB)
and its settlement basis; adding a step or a basis is a reviewed core change, not part of a
plugin.

Without a provider file only `local-docker` and the `salad` provider with no profiles
exist, so a Salad stage is refused with 409 until the operator supplies Salad profiles
(through the provider file or `--salad-campaign-profiles`).

### Provider plugins

A third-party vendor ships as a Python module (or installed package) with a
`register_from_config(section)` function that calls `register_provider(...)`. The operator
enables it by name in the provider file; nothing is loaded just because it is installed:

```json
{"schema": "tessera.campaign-providers.v1",
 "plugins": {"vendor_plugin": {"any": "plugin-owned settings"}}}
```

The name is resolved first as an entry point in the group `tessera.campaign_providers`,
then as a dotted module path. A plugin may not take an id another provider owns. Registration
validates the descriptor (id pattern, kind and target agree, positive output limits, known
delegated steps) and refuses a malformed one. Optional runner hooks (`sweep`, `on_settled`,
`experiment_ready`) form the `ProviderHooks` protocol; the runner skips any a provider lacks.
A settlement whose provider is no longer registered fails closed and is recorded rather than
skipping that provider's bookkeeping.

### Placement across candidates

Contract: [ADR 0060](decisions/0060-capacity-aware-stage-placement.md). A v2 stage may list
`alternate_providers` after its own provider, in order, so independent stages spread over every
registered provider instead of queueing for one slot:

```json
{"key": "featurize", "provider": "local-docker", "runtime": "docker-cpu-v1",
 "alternate_providers": [
   {"provider": "self-hosted", "runtime": "docker-cpu-v1"},
   {"provider": "salad", "runtime": "salad-workload-v1",
    "provider_config": {"...": "a Salad CPU profile"}, "max_exposure_cents": 20}],
 "max_exposure_cents": 30, "...": "..."}
```

Each candidate is checked at submit and approval like the stage's own provider (registered, worker
enrolled for the project, provider trust for every input, grant lifetime), so the approver
approves every destination. A candidate's `max_exposure_cents` is its cost estimate and hold; it
may not exceed the stage's, which stays the per-run cap.

When the stage needs an attempt the runner takes the first candidate that is still registered,
enrolled, trusted for the stage's inputs, fits the per-run cap, the campaign's remaining budget,
the program budget and the project limits, and has a free slot (`worker_slots` minus live grants
and attempts already placed there). If none fits it waits while a slot or a hold may still free
up, and otherwise fails exactly as a single-provider stage would (budget refusal,
`input_not_admitted`, `provider_unavailable`). It never chooses a provider the spec does not
list. The choice and every skipped candidate's reason are a `campaign.stage_placed` event, the
`placement` field of `tessera_get_campaign` and the attempt's `placement` section in the receipt.
An agent can pin or exclude candidates for the next placement with
`tessera_steer_campaign_placement`; each call replaces the previous one and can only filter the
approved list. A stage without
`alternate_providers` behaves exactly as before.

Give each instance its own worker principal (a principal binds one provider in the trust file),
and set `worker_slots` to the pollers that share it, so placement sees real capacity.

## The provider file

`tessera serve --campaign-providers FILE` takes one closed, owner-only (0600), secret-free
JSON file. It names provider instances and, under `salad_profiles`, the Salad operator
profiles (a `tessera.salad.campaign-profiles.v1` document, documented in
[campaigns](campaigns.md#salad-gpu-stages)). `--salad-campaign-profiles FILE` remains as an
alias for the profiles alone. Credentials are never in the file: a profile holds a
credential *reference*, and a worker credential is a file path. The file is read at start.

Provider ids and worker principal ids are placeholders for enrolled principals; the
deployment's real file is not kept in the repository.

## Self-hosted provider

1. Enroll a worker principal for the project that will run on the server (the same
   enrollment as any development worker; a human does it) and keep its credential in a 0600
   file on that server. `scripts/executor_enrollment.py enroll --provider self-hosted`
   (or `--provider local-docker` for the control host's own poller) creates one stable,
   renewable principal; see [provider trust](provider-trust.md#stable-executor-route-setups-that-mint-principals).
2. List the instance in the provider file under the id `self-hosted` with that worker
   principal and the runtime `docker-cpu-v1`. Optionally set `worker_slots` (1 to 8,
   default 1) to the number of pollers that share the principal: the runner issues a grant,
   and starts its lifetime, only while the worker holds fewer live grants than that (also
   available on `local_docker`; [ADR 0048](decisions/0048-grant-lifetime-and-slot-release.md)).
3. Decide, in the [provider trust](provider-trust.md) file, which data classes that
   principal may receive. The default is public data only, so a private input is refused
   with a 409 naming the provider and principal until the file says otherwise.
4. Run `tessera-worker development-poll` under a supervisor; example systemd units ship in
   [`deploy/systemd`](../deploy/systemd/README.md):

```sh
tessera-worker development-poll --config /etc/tessera/poll.json --health-file /run/tessera/poll.health
```

The poller config (`tessera.worker.development-poll.v1`) is a closed JSON file of paths and
settings, never a secret:

```json
{
  "schema": "tessera.worker.development-poll.v1",
  "control_url": "https://control.example",
  "credential_file": "/etc/tessera/worker.token",
  "workspace_root": "/var/lib/tessera/poll",
  "development_output_root": "/var/lib/tessera/out",
  "allowed_images": ["python@sha256:<digest>"],
  "poll_seconds": 2,
  "memory_bytes": 1073741824,
  "scratch_bytes": 67108864,
  "max_scratch_bytes": 2147483648,
  "container_user": "1000:1000"
}
```

`control_timeout_seconds` (optional, 1 to 20, default 15; also `--control-timeout-seconds` on
`development-poll` and `development-run`) is how long one HTTP request to control may take. A
slower answer is a transient failure that is retried (a running container is not stopped for it,
see [ADR 0054](decisions/0054-durable-observation-delivery.md)); raise it for a control plane that
answers slowly under load. The ceiling is the 30 s worker lease minus the 10 s renewal interval, so
one unanswered request cannot lapse the lease (the worker refuses a larger value, and a slower
`renew_after_seconds` lowers the ceiling accordingly). Each observation report is also capped by
the lease time left minus one renewal interval, never below 1 s.

`memory_bytes` and `scratch_bytes` are optional, operator-owned limits applied to every
container this poller runs (memory, with swap equal to it, and the `/work` tmpfs). A plan
cannot raise them. Unset keeps the defaults of 256 MiB and 16 MiB; accepted ranges are
32 MiB to 64 GiB and 1 MiB to 64 GiB, and a value outside them refuses the config (exit 2).
`max_scratch_bytes` (also `--max-scratch-bytes`) is the ceiling on the scratch a stage may
*request* with its spec's `scratch_bytes`; unset it is 64 MiB. The `/work` tmpfs is
memory-backed, so a scratch above 64 MiB must fit `memory_bytes` with 64 MiB to spare, and
a memory above 1 GiB or a ceiling above 64 MiB is checked against the host's memory (at most
75% and 50%) before the poller runs a grant. A request above the ceiling is refused at
admission, never mid-run. Declare the same ceiling to control as `max_scratch_bytes` on the
provider's entry in the provider file (`local_docker`, or a `self_hosted` instance), so
submit and approval refuse an over-large request with `scratch_exceeds_operator_ceiling`
([ADR 0059](decisions/0059-operator-scratch-ceiling.md)); keep the two equal: if control's
is higher the worker still refuses the run with its own ceiling, and if lower control refuses
what the worker would grant. A ceiling above what `memory_bytes` can back (the 256 MiB
default backs 192 MiB) is logged as a warning at startup, and a request over that is refused at
run. A default `scratch_bytes` above 64 MiB must fit `memory_bytes`
(256 MiB when unset) or the poller config is refused at load. Size them to the host: one poller
runs one container at a time.

`docker_network_pool` (optional, with `docker_network_prefix_length`, default 24; also
`--docker-network-pool` / `--docker-network-prefix-length` on `development-poll` and
`development-run`) gives the Docker networks Tessera creates their own address block.
Use it when other software has created so many Docker networks that Docker's default
address pools are exhausted and a run fails at network create with "all predefined
address pools have been fully subnetted" (only runs with an egress policy create a network;
`--network none` runs never do). Set it to a private IPv4 CIDR that nothing else uses, for
example `"docker_network_pool": "10.213.0.0/16"`. Each run's network is then created with an
explicit `--subnet` and `--gateway` from that block, skipping every subnet used by an
existing Docker network or a host route (read from `/proc/net/route` on Linux; on Docker
Desktop only the daemon's own overlap check applies). Concurrent runs are safe because
Docker refuses an overlapping subnet and the allocator then picks the next free one. The
prefix length must lie between the pool's own prefix and `/29`, giving at most 65536
networks; a full pool fails the run with a clear "no free subnet" error. Every network is
labelled `tessera.operation=<operation id>`, removed when its run ends, and the poller
removes leftover Tessera-labelled networks at startup, only those with no attached
container (in any state) and at least ten minutes old; it never deletes other software's
networks and you never need to restart the Docker daemon. Unset keeps Docker's default
pools exactly as before. In every case, a failed network create is recorded as a
`launch_failed_network_create` outcome carrying Docker's error: no container was created
or started, so nothing ran and nothing was spent.

`container_user` is an optional, operator-owned numeric `uid:gid` the stage containers run
as (`--user`); also `--container-user` on `development-poll` and `development-run`. A plan
or spec cannot set it. The poller's 0700 output workspace is owned by the poller's own uid,
and a stage can only write its declared outputs if the container runs as that uid (or the
workspace's owner). Default: on native rootful Linux, the poller's own `uid:gid`; on other
platforms (Docker Desktop maps bind-mount ownership) it stays `65532:65532` as before.
The same fallback applies when the poller's uid or gid is 0, including a non-root uid with
gid 0, because a 0 id is never chosen implicitly. Run the poller as a non-root user with a
non-zero gid. If it must run as root, its workspace is root-owned 0700 and, with all
capabilities dropped, only `"container_user": "0:0"` plus `allow_root_container_user` can
write it (a different non-root value fails at run time); a non-root uid with gid 0 needs an
explicit `container_user` set to its uid and a non-zero gid. Names are
refused, ids are bounded to 0-4294967294, and uid or gid 0 is refused unless
`"allow_root_container_user": true` (`--allow-root-container-user`) is also set. The
workspace stays 0700 and every private-custody check is unchanged; nothing is made
world-writable. The effective user is bound into the supervisor's durable fence digest beside the
memory and scratch limits, so a restart with a different user refuses the recorded operation
instead of reusing it. The journal stores the digest, not the user itself, so the user cannot
be read back from it; read it from the poller config.

**Rootless Docker, user-namespace remapping and Docker Desktop for Linux.** "Native Linux"
here means a rootful daemon without `userns-remap`. With rootless Docker or `userns-remap`,
container uid N maps to a subordinate host uid, and Docker Desktop for Linux uses a VM with
mapped file sharing, so the poller's own uid:gid does not own the workspace as seen from the
container and the default can still fail to write outputs. The poller does not detect this.
Set `container_user` to the id that the daemon's mapping makes own the bind mount (for
rootless Docker, usually `0:0` with `allow_root_container_user`, which maps to the poller's
host uid; for `userns-remap`, the mapped id from `/etc/subuid`), and verify with a one-stage
run. The opt-in Docker test skips on such daemons.

**Shared host uid.** By default the stage container now runs with the poller's host uid,
which also owns the poller's 0600 credential file and state directory. Only `/inputs`
(read-only) and `/outputs` are mounted, and a stage that changes modes inside its outputs
fails closed in the custody checks, but a container escape would land as the poller and
per-user kernel limits are shared with it. Run the poller as a dedicated service user that
owns nothing else. `Dockerfile.guard` asserts uid 65532 and is not a development-poller
image.

**Image requirement.** The container no longer runs as the image's own user (65532 in this
repository's example and workload images), so every file the stage needs from the image
(interpreter, libraries, code, data) must be world-readable (and world-executable for
programs and directories), or owned by the configured user. The images built from this
repository already `chmod a+rX`/`0555` their files. An image whose files are readable
only by uid 65532 (for example mode 0700 or 0750 owned by it) fails under another user;
rebuild it with `chmod -R a+rX` on the application tree, or set `container_user` to the
owner. The stage's `/work` tmpfs is mode 1777 and is writable by any user.

The credential file must be a private regular file (0600, owned by the service user), and
the config file must not be a symlink or group- or world-writable. The individual flags
(`--control-url`, `--credential-file`, and so on) still work for a one-off run.

## Service loop contract

`development-poll` and the Salad executor (`python -m
tessera_control.campaign_salad_executor`) share one loop, the contract in ADR 0038's
amendment:

- Runs forever by default. `--once` runs one cycle and `--max-cycles N` stops after N
  (tests and one-off runs); `max_grants` also ends it once that many grants have run.
- SIGTERM or SIGINT stops new work, finishes the current step and exits 0.
- `--health-file PATH` is replaced atomically (0600) after each loop with the pid, start
  time, last loop, loop count, consecutive errors, last error class and state. No port is
  opened.
- A running operation's control lease lasts 30 seconds and only the worker's renewal extends
  it. The worker renews as soon as it starts driving an operation (a recovered lease may have
  only seconds left), then every 10 seconds of elapsed time (and at least every 15 ticks), and
  never sleeps past the next renewal, whatever `poll_seconds` is. A renewal failure cancels
  the operation.
- Logs are bounded JSON lines with error classes only. A cycle that raises is counted and
  the loop continues.
- Exit 2 means the configuration or credential was refused: do not restart. Restart is
  otherwise idempotent (per-grant directories; a grant with a recovered runtime descriptor
  is reconciled, never re-leased).

## Salad provider

A Salad stage names an operator profile that is either a GPU profile (a `gpu_class`) or a CPU-only
profile (`cpu_rate`, no GPU); a stage that declares `salad.gpu: false` runs only on the latter. See
[campaigns](campaigns.md#salad-gpu-stages) and [ADR 0062](decisions/0062-salad-cpu-only-profiles.md).

### Checking a CPU profile against Salad (opt-in, bounded)

No offline test can show that Salad accepts a container group with no GPU class, or what a short
run costs at the profile's rates. `scripts/salad_cpu_live_check.py` checks both, and is **not**
part of default pytest or CI: it is a dry run unless you pass `--run` and an exact `--confirm`.

```
scripts/salad_cpu_live_check.py check --profiles FILE --profile NAME --contract FILE \
    --image IMAGE@sha256:DIGEST [--max-cents N]                     # plan only, no network
scripts/salad_cpu_live_check.py check ... --run --confirm spend-up-to-Nc \
    --credential-file FILE --receipt-directory DIR                  # spends, up to the ceiling
scripts/salad_cpu_live_check.py billing --receipt FILE --portal-cents X
```

- **Dry run (default).** It reads your profile file and workload contract, derives the exact
  route, and prints the shape, the hourly rate, the ceiling, the running seconds that ceiling buys
  and the `--confirm` token. It makes no network call and reads no credential.
- **Spend bound.** The ceiling is `--max-cents` (default 5) and never more than **9 cents**, a
  constant in the script, not a setting. Billing continues until the instance is actually down,
  so 30 s are reserved out of the ceiling for the stop to take effect: the running time is cut at
  the seconds the ceiling buys minus 30 (at most 300 s), and the estimate counts through the
  observed stop (through the whole 90 s settle wait if a stop is never seen, which is then
  reported as outside the bound). A rate that leaves under 30 s of running, or a workload that
  declares longer than the bound, is refused. The group is always stopped and deleted, on every
  path including an error; if that fails the receipt says so and names the group to stop by hand.
  An unexpected error does not lose the receipt: it is recorded as `aborted` (its type only, never
  its message) beside the group name and the cleanup outcome.
- **What a run does.** It makes only the vetted Salad transport's calls: read the quota, create
  one stopped group (free), read it back and compare its shape (**no GPU class**, the shape check),
  start it, poll until it ends or the bound, stop, delete. Salad publishes no CPU availability read,
  so the check cannot show capacity.
- **Inputs.** The profile must be a CPU profile; the image must be allowlisted by it; the contract
  is your reviewed `tessera.salad.workload.v1` JSON for a short CPU job (a stub that exits in
  seconds is enough). The credential is an owner-only (0600) absolute file holding the Salad
  management token; it is held in a wiped buffer and never printed or stored.
- **Billing is a manual comparison.** The receipt records the running seconds and the estimate at
  your rates (`estimated_cents`). Copy the figure Salad's own usage page shows for that group and
  run `billing --receipt FILE --portal-cents X`: it agrees if the two are within the larger of one
  cent or half the estimate (usage figures are coarse), otherwise correct the rates in the profile.
  The tool never calls a billing endpoint and never reports an estimate as an invoice.

Until a person has run this once and recorded an agreeing receipt for a provider account, a CPU
profile's group shape and billing are unverified, as ADR 0062 says.

The Salad provider wraps the Salad stage driver; its settlement bases are unchanged
(`unclaimed_zero`, `human_reconcile`, `human_charge`, `guardian_bounded_hold`; the last is
provisional and confirmed by the first human charge). See
[campaigns](campaigns.md#salad-gpu-stages) for what the runner automates and where a human
is still needed, and for the executor, which runs under the same loop contract.

A Salad stage may take stage-output or artifact inputs, composed by the core into one
snapshot, when the provider trust setting approves `salad` for `private_derived`. Then the
route's `data_policy` is `provider_trust_policy_v1` rather than `public_released_only`,
and the executor re-derives the same value from the stage. Two refusals apply even then:

- A stage with any non-public input may not declare workload fetch or redirect hosts,
  because Salad's egress is provider-wide. This is decided statically from the spec: stage
  and artifact inputs always count as non-public, and so does a `send_classes` beyond
  `public`.
- Provider trust is checked again at ticket issue and at each pull, so narrowing the setting
  stops later pulls.
