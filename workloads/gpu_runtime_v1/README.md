# gpu_runtime_v1: slim generic GPU runtime (ADR 0036 phase 5)

A reviewed recipe, not a published image. It is a digest-pinned CUDA *runtime*
base plus `python3` and `pip` and a stdlib-only entrypoint. It contains no ML
framework, model or experiment code, so it stays small (target well under
1.5 GB) and cold-starts on Salad; everything else is fetched at run time under
a hash-pinned manifest the reviewed `workload_v1` contract already declares.

## Choosing the base (operator)

No default is baked in. Pick a CUDA **runtime** (not `devel`, not a torch image)
for linux/amd64 whose driver requirement your Salad GPU class meets, for
example an `nvidia/cuda:<cuda>-runtime-ubuntu22.04` tag. Resolve it to a digest
yourself (`docker buildx imagetools inspect <tag>`) and pass
`registry/repo@sha256:<64 hex>`:

    docker build --platform linux/amd64 \
      --build-arg GPU_RUNTIME_BASE=<repo>@sha256:<digest> \
      --build-arg UBUNTU_SNAPSHOT=<YYYYMMDDTHHMMSSZ> workloads/gpu_runtime_v1

`UBUNTU_SNAPSHOT` (owner-supplied, no default) pins apt to
`snapshot.ubuntu.com/ubuntu/<timestamp>` so the python3/pip/ca-certificates
layer is reproducible; NVIDIA's own apt lists are removed for the same reason.
The build refuses a base that is not digest-shaped, a malformed timestamp, or
a non-x86_64 base.

Runtime versus base: a CUDA *runtime* variant is needed when the workload's
wheels load system CUDA libraries (cuBLAS, NVRTC, cuDNN, e.g. CuPy). torch
wheels bundle their own CUDA libraries, so a smaller `-base` variant can work
for them; only the NVIDIA driver comes from the host either way.

**Retain the built image digest.** The route is reviewed against the image
digest produced by the build/push, not against the base; record it with the
base digest, snapshot timestamp and the commit of this recipe. The image runs
as UID 65532 with fixed entrypoint `/tessera/bin/run`. Wheels you fetch must be
compatible with the base's Python (Ubuntu 22.04: 3.10) and CUDA runtime.

## Argv (derived from the contract's `argv_template`)

    v1 OPERATION EPOCH CUSTODY_HOST WATCHDOG WORKLOAD_SECONDS MAX_OUTPUT_BYTES
       MAX_OUTPUT_FILES MANIFEST_SHA256 [input:SNAPSHOT_SHA256:MAX_BYTES]
       [redirect-hosts:HOSTS_SHA256] MANIFEST_REF...

Template: `["v1","{operation}","{epoch}","{custody_host}","<watchdog>",
"<workload_seconds>","<max_output_bytes>","<max_output_files>","<manifest_sha256>",
[<input token>], [<redirect-hosts token>], <ref tokens>]`
(at most 32 tokens of 256 characters; the command itself is `/tessera/bin/run`).

- `WATCHDOG`, `WORKLOAD_SECONDS`, `MAX_OUTPUT_BYTES`, `MAX_OUTPUT_FILES` are
  literals and **must equal** the reviewed profile (`watchdog_profile`,
  `workload_seconds`, summed output caps, output count): the runtime sends them
  as the guardian binding and the guardian compares it with its own derived
  request. `MAX_OUTPUT_BYTES` is 1..16 MiB (the sum of the declared caps) and
  `MAX_OUTPUT_FILES` 1..16 (the number of declared outputs).
- `MANIFEST_SHA256` is the sha256 of the manifest's exact bytes. It is checked
  **before** the manifest is parsed; mismatch refuses everything.
- `input:SNAPSHOT_SHA256:MAX_BYTES` (optional) declares that a staged input
  snapshot is expected. It is present **exactly** when the contract declares
  `input` and must equal `input.snapshot_digest` and `input.max_bytes`; the
  selection in turn requires `input.snapshot_digest == manifests.data`. No token
  means nothing is fetched. See "Staged input" below.
- `redirect-hosts:HOSTS_SHA256` (optional) is present **exactly** when the
  contract declares a non-empty `egress.redirect_hosts`; `HOSTS_SHA256` is the
  sha256 of that sorted list as compact JSON (`["a.example.net","b.example.org"]`).
  The manifest must then carry the same list as `redirect_hosts` (the runtime
  checks the hash, for an inline manifest before the claim). It comes after the
  `input:` token and before the refs. See "Redirects" below.
- `MANIFEST_REF` is either one or more `b64:<base64url, unpadded>` tokens whose
  payloads are concatenated (inline, at most 64 KiB; backward compatible), or,
  with an input token, exactly one `input-manifest:FETCHES_SHA256:PATH` token:
  the manifest is the file `PATH` (segments `[A-Za-z0-9][A-Za-z0-9._-]*`, at
  most 8, no `..`) inside the verified input snapshot, at most 256 KiB. Its
  sha256 must still equal `MANIFEST_SHA256` (checked before parsing), and the
  sha256 of its sorted `[url, sha256, max_bytes]` rows (canonical JSON) must
  equal `FETCHES_SHA256`, which contract validation derives from
  `egress.fetches`. Its `redirect_hosts` must hash to the `redirect-hosts:`
  token (absent token: no `redirect_hosts`), so the reviewed argv binds every
  host the runtime may contact. A `file:` form exists in the runtime only behind a
  test-only flag; the runtime entrypoint and reviewed contracts refuse it.
- `WORKLOAD_SECONDS` must be at least 60: the runtime refuses to spend the
  single begin-once claim with less than 60 s of the workload window left
  (fetching and installing count against the window).

## Manifest (`tessera.gpu-runtime.manifest.v1`)

Canonical JSON (sorted keys, compact), at most 64 KiB:

    {"schema":"tessera.gpu-runtime.manifest.v1","fetches":[
      {"url":"https://...","sha256":"<64 hex>","max_bytes":N,
       "kind":"wheel|file|entry","dest":"name"}, ...],
     "redirect_hosts":["cdn.example.net", ...]}

`redirect_hosts` is optional: present (1-16 sorted, unique, exact lowercase DNS
names; no label is all digits or starts with `0x`, so no IP-like form) exactly when the contract's `egress.redirect_hosts` is non-empty and
equal to it, and bound by the argv `redirect-hosts:` token.

At most 128 fetches (about 45 wheels plus a model revision fit with headroom;
the contract's `egress.fetches` has the same cap). Each fetch keeps its own
`max_bytes` (at most 8 GiB); the runtime refuses a manifest whose caps sum
past 1 TiB, and the route selection bounds the declared fetch plus input bytes
by its `disk_mb`. `wheel` dests are a bare `*.whl` filename; `file` dests are relative paths
(`files/<dest>`); exactly one `entry` is required and must end in `.py`.
Absolute paths, `..` and duplicates are refused.

### Items served from the staged input

With a declared input, an item may name a file of the verified input snapshot
instead of a URL:

    {"kind":"entry","source":"input","path":"main.py"}

It has exactly the keys `kind` (`entry` or `file`), `source` (`"input"`) and
`path`, and **no** `url`, `sha256` or `max_bytes`: its bytes are pinned by the
snapshot digest (`input.snapshot_digest`, which the selection binds to
`manifests.data`), not by a fetch row. So it is not an egress fetch: it is absent
from `egress.fetches`, from the `FETCHES_SHA256` of an `input-manifest:` ref and
from the capability `network` declaration (a manifest whose only item is an input
entry has an empty `egress.fetches` and an empty network). Rules, enforced by
setup and control admission for every new route (inline or carried manifest)
and again by the runtime:

- allowed only when the contract declares an `input` (the runtime refuses it
  before the claim when no `input:` token is present);
- `path` is relative, segments `[A-Za-z0-9][A-Za-z0-9._-]*`, at most 8 deep, no
  `..`, no leading `/`; an entry ends in `.py`; paths are unique;
- exactly one entry in total, URL-fetched or input-sourced;
- the item shapes, path rules and exactly-one-entry rule are new-route checks
  (setup, `check_staged_input`, admission); the model validator only excludes
  input items from the egress comparison and requires a declared input, so
  stored proposals from before this change keep parsing for stop and reconcile;
- the path must be a file of the staged snapshot: setup (`--input-dir`) and
  control admission (propose, approve, create/start) refuse a snapshot that lacks
  it, before any begin-once;
- at run time it must be a regular file of the verified read-only tree, opened
  without following any link (a symlink, directory or missing path fails with
  `input_item` before the entry runs). The entry runs **in place** from
  `<work>/input/<path>` (0444, directories 0555); it is not copied, and Python
  puts its directory first on `sys.path`, so it can import sibling modules that
  are part of the same verified snapshot.

**Image requirement.** Input-sourced items need an image built from commit
`fccd08e` (this change) or later. An older image refuses them as a malformed
manifest: with an inline manifest that happens before the begin-once claim, but
with a manifest carried in the input (`input-manifest:`) it is only read after
begin-once, so an older image spends the operation's single claim and then fails.
Record the image's source commit and do not route input items to an older image.

`file` items are not copied either; they only assert presence (the entry reads
them from `<work>/input`). Old images refuse manifests with input items (the
exact item key set), so they fail closed.

## Setup/review must bind the manifest to the contract

A contract declares the layout with `"runtime": "gpu_runtime_v1"` (optional
field; absent means no layout is assumed). Contract validation then enforces
the binding itself: `argv_template` must follow the layout above with the three
placeholders in place, the literal SECONDS/OUTPUT_BYTES/OUTPUT_FILES tokens
must equal the contract's `workload_seconds`, summed output caps and output
count, WATCHDOG must be 64 hex (it is checked against the profile's
`watchdog_profile` by the guardian binding, not by the contract), only inline
`b64:` refs are allowed, the decoded manifest must hash to `MANIFEST_SHA256`,
and its URL `(url, sha256, max_bytes)` rows must equal `egress.fetches` (as a
set; order is free; input-sourced items are no rows, see above). The manifest's rows equal the contract's fetches because the contract's fetch list drives the
capability `network` declaration the reviewers approve. Build it, never write
it by hand:

    python scripts/workload_manifest.py contract.json \
      --item wheel:foo-1.0-py3-none-any.whl --item entry:main.py --refs

A contract with `egress.redirect_hosts` gets the same list in the manifest;
`--redirect-ref` prints the `redirect-hosts:` token. Redirect hosts are
accepted only with `runtime: gpu_runtime_v1`, whose argv needs this output, so
the helper accepts a draft without `runtime`: it validates the rest of the
contract and its `egress` separately (the layout is checked once `runtime` and
the final argv are in place).

`--item entry:input:main.py` (or `file:input:PATH`) adds an input-sourced item;
it pairs with no `egress.fetches` row, so the other items still pair with the
fetches in order. `--input-ref` derives `FETCHES_SHA256` from the URL rows only.

It prints the manifest sha256 (put it in `argv_template`) and, with `--refs`,
the `b64:` tokens to append. `--out FILE` writes the canonical bytes.
Egress is provider-wide and not enforced by Tessera: the runtime only ever
requests URLs listed in the verified manifest, over HTTPS, no proxy, and follows
a redirect only to a reviewed redirect host (below).

## Redirects

Large public artifact hosts answer a pinned URL (for example a URL naming an
immutable commit) with a redirect to a CDN whose signed URL expires, so the CDN
URL cannot be pinned. A contract may declare those CDN hosts in
`egress.redirect_hosts`; they join the capability `network` (port 443) that
reviewers approve. The runtime then, per fetch:

- requests the manifest URL (its host is the declared fetch host), one hop at a
  time with redirects disabled in urllib, no proxy and a fresh request with no
  headers at every hop (no Authorization or cookie crosses a hop);
- on 301, 302, 303, 307 or 308 resolves `Location` against the current URL (so a
  relative or scheme-relative Location gets the same check) and follows it only
  if it is HTTPS, has no credentials, uses the default port (443) and its host
  is exactly one of the reviewed redirect hosts; the fragment is dropped;
- follows at most 3 redirects; a fourth, a missing, duplicated, non-ASCII or
  whitespace-bearing Location, an http downgrade, another port or an undeclared
  host fails with `redirect` before any byte is written;
- gives every hop a socket timeout of at most the remaining wall clock divided by
  the hops that may still follow, checks the deadline again when a hop
  returns, and applies the streaming `max_bytes` cap, the `Content-Length`
  check and the sha256 to the final response's bytes, exactly as without a
  redirect. The deadline is checked before and after every body read, so the
  whole body (including its final read) ends within the run's wall clock.

With no redirect hosts (the default) any redirect fails with `redirect`. The
`fetched` record names the pinned manifest URL, never the signed CDN URL.
Integrity does not depend on the CDN: the sha256 pins the bytes either way;
the redirect hosts only make the declared network honest.

**Image requirement.** Redirects need an image built at or after the commit
that added them. An older image refuses the `redirect-hosts:` argv token in
`parse_argv`, before any fetch, input pull or begin-once, for both the inline and
the input-carried manifest, so it fails closed with `argv`. Contracts without
redirect hosts produce the same argv as before and run on older images.

## Run sequence

0. The work dir is wiped at startup (only a fresh `out/` remains).
1. Parse argv; verify manifest digest; validate manifest.
2. Download each item with a streaming size cap (`max_bytes`, also checked
   against `Content-Length`) into a `.part` file, verify sha256, then rename.
   A redirect is followed only to a reviewed redirect host (see "Redirects").
   Any mismatch removes the partial file and fails; no byte is used unverified.
3. `python -I -m pip install --isolated --no-index --no-deps --find-links
   <work>/wheels --target <work>/site <only the verified wheels>` with
   `PIP_CONFIG_FILE=/dev/null` and a minimal environment; the entry gets `PYTHONPATH=<site>`.
4. IMDS JWT, then guardian **begin-once** (same protocol as the smoke's
   `HTTPSCustody`; the class is a copy adapted to take the binding from argv,
   leaving the reviewed smoke recipe untouched). Denied or uncertain means stop.
   Then fetch the guardian's declared output plan (`/v1/workload/outputs`) and
   refuse a plan whose count, summed caps or chunk size disagree with the argv
   literals, before the entry runs.
4a. **Staged input** (only with an `input:` token): pull the snapshot from the
   guardian (`POST /v1/workload/input`, same IMDS JWT and binding as
   begin-once; the ticket never leaves the guardian) and materialize it
   read-only at `<work>/input` (see below). With an `input-manifest:` ref, the
   fetch manifest is then read from the verified tree, checked as in step 1
   and its rows against `FETCHES_SHA256`, and steps 2-3 run here instead of
   before the claim. Any failure stops before the entry.
5. Resolve input-sourced items in the verified tree (regular files, no links);
   an input entry runs from `<work>/input/<path>`.
   Run `python -B <entry> <work_dir> <out_dir>` (stdin/stdout/stderr discarded,
   own process group) with a wall clock of `WORKLOAD_SECONDS` measured from
   start, covering fetching. Timeout kills the group.
6. Snapshot every declared output from `<out_dir>/<name>` into a private staging
   directory before uploading anything: each must be a regular file (no
   symlinks) within its declared cap, or the run fails with nothing uploaded.
   Undeclared files are ignored. A declared `output.json` must be a JSON object
   without its own `fetched` key; the runtime adds `fetched: [{url, sha256,
   bytes}, ...]` and re-encodes it canonically within its cap. Other files are
   uploaded byte for byte.
7. Upload each staged file in 1 MiB chunks (`/v1/workload/output-chunk`) with its
   whole-file sha256 and each chunk's sha256. A chunk is idempotent at the
   guardian, so a failed call is retried (at most 3 attempts per chunk, 8 extra
   attempts in total). Success requires every file to be reported complete and
   the last chunk to report all declared outputs complete; the guardian then
   stops the group. Upload may use the guardian's 120 s grace (the process alarm
   is `WORKLOAD_SECONDS + 100`). If the final response is lost, the retry of the
   final chunk is acknowledged (read-only) while the guardian reason is
   `output_retained`, and a crash before the guardian's output flag is repaired
   the same way. Once the group has stopped, the retry is refused and the runtime
   reports failure even though the guardian retained everything; the guardian's
   state is the truth.

**Upload timing is untested live.** Each 1 MiB chunk is a ~1.34 MiB request that
must finish inside the guardian's 10 s per-request budget together with one Salad
API read, so the node's sustained uplink must be at least about 1.5 Mbit/s. A
full 16 MiB upload is 16 such requests: about 45-60 s at 5 Mbit/s, near the
100 s runtime upload window at 2 Mbit/s, and impossible below that. Keep total
declared outputs well under 16 MiB until a live run has measured the uplink.

Success prints `{"schema":"tessera.gpu-runtime-status.v1","status":"retained",
"outputs":[{name, sha256, bytes}, ...]}`. Failures print only
`{"schema":"tessera.gpu-runtime-status.v1","status":"failed","reason":<closed code>}`;
no exception text or provider detail.

## Provider-side cache

A `file` or `wheel` URL item may carry an optional boolean `"cache": true` (an `entry` may not; an
absent key means off). The cache is only useful where the provider mounts persistent storage into
the container: a Salad container is otherwise ephemeral, so without it every flagged item reports
`cache: "unavailable"` and is fetched as usual.

- **Where.** The directory comes only from the runtime's own environment: `TESSERA_FETCH_CACHE_DIR`
  (set by the image or by the reviewed contract `env`) and optionally `TESSERA_FETCH_CACHE_MAX_BYTES`
  (decimal; default 0 means never store, lookups still work). The directory must be absolute, a real
  directory with no symlink in its path, owned by the runtime's uid, mode 0700, and neither inside nor
  containing the work directory. Anything else reads as `unavailable`.
- **Key and verification.** An entry is `<dir>/<sha256>`: the pinned digest is the key, never a name from
  the manifest. Every reuse re-hashes the whole entry while copying it (copies, never links; a regular
  single-link file only, within the item's `max_bytes`). A mismatch or wrong size deletes the entry and
  the item is fetched normally, so nothing unverified is ever used. A verified fresh fetch is stored
  atomically (0600) when it fits the quota, evicting the oldest entries first; a storing failure never
  fails the run.
- **Never an output.** The cache directory is outside the work tree, so cached bytes cannot be staged
  or uploaded; the control plane never receives them.
- **Row.** Each retained `fetched` row gains `cache`: `hit`, `miss`, `unavailable` or `off`. A hit's
  `bytes` is the size read and `seconds` the local copy time.
- **Older images** refuse the extra `cache` key (the exact key set), before the claim for an inline
  manifest, so a reviewed contract with a cache flag cannot run on an image that does not know it.

## Staged input

The expected digest and byte cap come only from the reviewed argv. The runtime
requests the manifest, refuses a reply whose `digest` differs from the argv,
re-derives the digest from the manifest's canonical JSON, requires the chunk
index to be exactly the manifest's 4 MiB plan and the total within `MAX_BYTES`
(the default operator artifact bound, 8 GiB; the reviewed cap and the guardian apply the
operator's actual bound), then pulls each chunk and checks its index fields, size and sha256
(canonical base64 only) before writing it, and each file's size and sha256 and
the total afterwards. These checks mirror `tessera_worker.chunked_inputs`
(stdlib only, one file in the image); `tests/test_salad_input_path.py` runs both
verifiers on the same tampered snapshots and requires the same verdicts.

Paths follow the worker's rules (relative, no empty/`.`/`..` segments, no
backslash or NUL, bounded length and depth, no case/Unicode aliases, no file
that is also a directory). Files are created with `O_EXCL|O_NOFOLLOW` through
directory descriptors under a fresh `<work>/input`, then made 0444 and their
directories 0555. A transport failure is retried at most 3 times per range and 8
extra times overall (the guardian allows 3 and 12); a verification mismatch is
never retried. Any failure removes the partial tree. The tree is removed again
at exit and at the next start. Each stored 4 MiB chunk is requested as ranges of
at most 1 MiB (`{"request":"chunk","seq","offset","length"}`, reply schema
`tessera.input-range.v1`); every range reply's fields and canonical base64 are
checked, and the reassembled chunk must match its index size and sha256 before any
byte is written. Each reply (a range or the manifest) is at most 1,402,197 bytes, so
at the 1.5 Mbit/s floor it fits the guardian's 20 s chunk request alarm with its
control fetch and auth (see docs/salad-safety-service.md); an input of N GiB is about
1,024 x N range requests (3,072 for 3 GiB), each with one Salad API read at the guardian.
The ticket lifetime and the guardian's per-ticket serve budget bound what is practical.
This is untested live.

## Cold-start smoke (`smoke/`)

A generic, stdlib-only smoke delivered entirely through the staged input: no URL
fetch, no wheel, empty `egress.fetches` and capability network. `smoke/entry.py`
reads `data/sample.bin` from `<work>/input` and hashes it (compared with the
digest in `expected.json` next to it), probes the GPU through ctypes against the
host driver's `libcuda.so.1` (`cuInit`, `cuDeviceGetCount`, `cuDeviceGet`,
`cuDeviceGetName`, `cuDeviceTotalMem_v2`; a missing driver or CUDA error is
recorded as a closed code, not raised), and writes the two declared outputs
`result.json` (input digest, GPU facts, timings) and `probe.txt` (a short
summary), each at most 64 KiB. `smoke/stage.py` writes the staged-input directory
(0700: `entry.py`, `data/sample.bin`, `expected.json`) and the contract (0600:
`runtime: gpu_runtime_v1`, `workload_seconds: 300`, inline manifest with the one
item `{"kind":"entry","source":"input","path":"entry.py"}`, `input` equal to the
directory's digest and size). It validates the contract and checks the directory
against it exactly as setup does, offline.

Operator commands (from the repository root on the trusted host, paths absolute
and new; the parent directory must already be owner-only 0700):

    .venv/bin/python workloads/gpu_runtime_v1/smoke/stage.py \
      --input-dir /PRIVATE/gpu-smoke/input \
      --contract-out /PRIVATE/gpu-smoke/contract.json \
      --custody-host CUSTODY.TAILNET.ts.net

    .venv/bin/python scripts/salad_live_setup.py \
      --state-dir /PRIVATE/state --owner-header /PRIVATE/state/owner.header \
      --output-dir /PRIVATE/salad-live-gpu-smoke-1 \
      --image ghcr.io/OWNER/REPO@sha256:REBUILT_GPU_RUNTIME_V1_DIGEST \
      --salad-org ORG --salad-project PROJECT --gpu-class-id GPU_CLASS_UUID --country US \
      --custody-dns CUSTODY.TAILNET.ts.net --custody-admin-host ADMIN_HOST \
      --custody-ca-file /PRIVATE/ca.pem --custody-cert-sha256 CERT_SHA256 \
      --custody-token-file /PRIVATE/admin-token \
      --credential-directory /PRIVATE/credentials --credential-version N \
      --guardian-socket /PRIVATE/guardian.sock --guardian-uid UID \
      --source-commit COMMIT_40_HEX \
      --workload-contract /PRIVATE/gpu-smoke/contract.json \
      --input-dir /PRIVATE/gpu-smoke/input \
      --data-class public --license CC0-1.0 \
      --source "tessera:workloads/gpu_runtime_v1/smoke/stage.py" \
      --max-seconds 3600 --max-polls 360

    .venv/bin/python -m tessera_control.salad_live_check --run \
      --server-config /PRIVATE/salad-live-gpu-smoke-1/salad-live-config.json \
      --receipt-directory /PRIVATE/gpu-smoke-receipt-1

Since ADR 0039 a third-party GPU route only admits staged input a human declared
public with license and source evidence; the smoke's input is generated synthetic
data, declared here as CC0-1.0 with its generator as the source.
`--custody-host` must equal `--custody-dns`. The argv WATCHDOG is
`--watchdog-profile` of `stage.py` (default 64 `b`, setup's default); pass the
same value to both if you change it. `--max-seconds` is the route window
(unbilled provisioning plus the 300 s workload plus upload); `--max-polls` times
the driver's 10 s poll cap should cover it. `stage.py` prints the
`snapshot_digest`, `manifest_sha256` and `data_sha256`; the retained
`result.json` must report that `data_sha256` with `matches_expected: true`.
The image must be rebuilt from a commit that has input items and multi-file
outputs: the published image predates both and refuses this manifest (it fails
closed with `manifest`, before the begin-once claim). No live run of this smoke has happened yet.

## Trust note on `fetched`

`fetched` is what this trusted runtime reports about what it downloaded and
verified. It is **not an attestation**: the entry runs as the same UID and
could, for example, upload its own output before the runtime does. The
guardian's caps (single claim, declared names, per-file and total size limits)
still apply; treat
`fetched` as an honest record from reviewed code, not as proof against a
malicious entry.

## Status

Recipe implemented and offline-tested (`tests/test_gpu_runtime_v1.py`; the
upload code is also run against the real guardian service in
`tests/test_salad_output_custody.py`). Multi-file output (ADR 0036 phase 3)
needs a rebuilt image: an image built before it accepts only one `output.json`
and uses the original single-object endpoint, which the guardian still serves
for that contract shape. Staged input (ADR 0036 phase 2 on the runtime side)
and the manifest carried in the input also need a rebuilt image; they are
offline-tested against fake custody and the real guardian handler only.
Reviewed redirects are offline-tested against a fake single-hop transport
(`tests/test_gpu_runtime_redirects.py`) and need a rebuilt image.
No live cold-start has been run.

## CPU runtime variant and baked wheels

`Dockerfile.cpu` (ADR 0076) builds this runtime on a non-CUDA base for a Salad CPU profile. The base
is an owner-supplied `name@sha256:` image that already has python3 (3.10 or newer) and pip; there is
no default, and no package manager is run. The runtime, `stage_argv.py` and the fixed entrypoint
are the same as the GPU image's, and the local worker overrides the entrypoint, so one image runs
on both.

Dependencies for a stage with private inputs (which may not declare fetches) are baked in: pass
`--build-arg WHEELS_LOCK=requirements.lock`, a complete lock (every transitive dependency, as
`pip install --no-deps` is used) in which every requirement carries `--hash=sha256:...`; pip runs
with `--require-hashes --only-binary=:all:`. Network access is at image build time only, to the
index you choose. The pushed image's digest, listed in the profile's `allowed_image_digests`, is
what pins the wheels, so record it with the base digest, the lock file's sha256 and this recipe's
commit. Wheels supplied as hash-verified items of the input snapshot are not implemented.

### Image entry

A manifest may hold one item `{"kind":"entry","source":"image","path":"stage_argv.py","argv":[...]}`
as its only item. It is resolved inside the image (`/tessera/lib/entries/`), is not a fetch, and
needs no declared input. The runtime runs `python -B stage_argv.py WORK OUT -- ARGV...`; the launcher
rewrites arguments at `/inputs` and `/outputs` to `WORK/input` and `OUT`, runs the argv in `WORK`
with no stdin, and exits with its code. An image older than this change refuses the item (exact key
set). The stage adapter builds such a manifest; a hand-written contract may too.
